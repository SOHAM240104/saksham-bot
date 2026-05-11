import asyncio
import json
import logging
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from sqlalchemy import desc

from app.config.base import SessionLocal
from app.models.chat.chat import Conversation, Message, Thread
from app.models.senior import Senior
from app.models.subscriptions import Subscription, SubscriptionPlan
# from app.models.techsaathi import TechSaathi
from app.models.user import User
from wati.services.conversation import (
    _build_history_messages,
    classify_wati_turn_intent,
    conversation_control,
    generate_wati_reply,
)
from wati.settings import settings

from .parse import (
    _extract_button_reply_id,
    _extract_name,
    _handoff_confirm_decision,
    wati_payload_indicates_human_operator,
)
# Free-tier thread limit (disabled )
# from .feature import FreeTierThreadLimitReached, free_thread_limit
from .wati_webhook import (
    read_wati_timeline_state,
    send_interactive_buttons_message,
    send_interactive_platform_list_message,
    send_message,
    send_template_message,
)

logger = logging.getLogger("wati.services.handling")
COPY_LLM = ChatOpenAI(model="gpt-4.1-mini", temperature=0.6)


# ===== : DB-DRIVEN HANDOFF ASSIGNEE PICKER - START =====
# def _pick_handoff_assignee_email() -> str:
#     db = SessionLocal()
#     try:
#         chosen = (
#             db.query(TechSaathi)
#             .join(User, User.id == TechSaathi.user_id)
#             .filter(
#                 TechSaathi.is_deleted.is_(False),
#                 TechSaathi.is_active.is_(True),
#                 User.is_deleted.is_(False),
#                 TechSaathi.is_active.is_(True),
#                 User.email.isnot(None),
#                 User.email != "",
#             )
#             .order_by(TechSaathi.assigned_count.asc(), TechSaathi.created.asc())
#             .first()
#         )
#
#         if not chosen or not chosen.user:
#             return ""
#
#         email = (chosen.user.email or "").strip()
#         if not email:
#             return ""
#
#         chosen.assigned_count = int(chosen.assigned_count or 0) + 1
#         db.commit()
#         return email
#
#     except Exception:
#         db.rollback()
#         logger.exception("Failed to pick handoff assignee email")
#         return ""
#     finally:
#         db.close()
#

def _load_dynamic_copy_prompt() -> str:
    fallback = (
        "You are generating outbound WhatsApp support copy.\n"
        "Return exactly ONE sentence, plain text only.\n"
        "No emojis. No bullets. No markdown.\n"
        "Tone: warm, calm, human.\n"
        "Keep it concise."
    )
    try:
        path = Path(__file__).resolve().parents[2] / "llm" / "systemprompt.txt"
        text = path.read_text(encoding="utf-8")
    except Exception:
        return fallback
    m = re.search(
        r"<dynamic_copy_prompt>\s*(.*?)\s*</dynamic_copy_prompt>",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return fallback
    extracted = (m.group(1) or "").strip()
    return extracted or fallback


def _dynamic_copy(kind: str, context: dict | None = None) -> str:
    prompt = _load_dynamic_copy_prompt()
    context = context or {}
    goals = {
        "handoff_wait": "Tell user we are connecting to a human support agent and ask them to wait.",
        "handoff_confirm": "Ask user if they want to connect to a human agent now; ask to reply Yes or No.",
        "resolved_ack": "Acknowledge that issue is resolved in a warm way.",
        "resolved_next": "Ask what else the assistant can help with in a friendly way.",
        "feedback_checkin": "Write a natural one-line follow-up check-in after troubleshooting steps. Avoid 'Did this help?'.",
        "welcome_back_context": (
            "Given prior thread history, write one short warm welcome-back line that "
            "references previous help naturally and ends with: How can I help you today?"
        ),
        "thread_summary": (
            "Given prior thread history, write one short, plain-language summary of what issue "
            "was discussed and where things ended."
        ),
        "thread_topic": (
            "Given prior thread history, extract only the main issue topic in 3-10 words, "
            "without resolution status or greeting."
        ),
        "welcome_back_blended": (
            "Write one short, natural welcome-back line that references the prior issue topic from context. "
            "Sound human and specific to that issue, not template-like. Keep tone positive and warm. "
            "Do not mention unresolved/failed/not fixed. End by asking how you can help now."
        ),
    }
    fallback = {
        "handoff_wait": "I'm connecting you to a human support agent now. Please wait a moment.",
        "handoff_confirm": "Would you like me to connect you to a human agent now? Please reply Yes or No.",
        "resolved_ack": "Happy to know this helped.",
        "resolved_next": "What else can I help you with today?",
        "feedback_checkin": "Please try this once and tell me what you see now.",
        "welcome_back_context": "Hi, I am your Tech Saathi from Saksham. How can I help you today?",
        "thread_summary": "Earlier we worked on your previous issue and I can continue from there.",
        "thread_topic": "your previous phone issue",
        "welcome_back_blended": "Welcome back. Hope your previous issue is okay now - how can I help today?",
    }
    try:
        user_payload = {"goal": goals.get(kind, ""), "context": context}
        r = COPY_LLM.invoke(
            [
                SystemMessage(content=prompt),
                HumanMessage(content=json.dumps(user_payload, ensure_ascii=False)),
            ]
        )
        txt = (getattr(r, "content", "") or "").strip()
        if txt:
            return txt
    except Exception:
        logger.exception("Dynamic copy generation failed kind=%s", kind)
    return fallback.get(kind, "How can I help you?")


def _default_button_message(action: str) -> str:
    if action == "mode_buttons":
        return "How can I help you today?"
    if action == "platform_buttons":
        return "Which phone are you using?"
    if action == "feedback_buttons":
        return _dynamic_copy("feedback_checkin")
    return "Please choose an option."


def _get_or_create_user(db, phone: str, first_name: str | None) -> User:
    digits = "".join(c for c in str(phone or "") if c.isdigit())
    p_91 = digits
    p_plus = f"+{digits}" if digits else ""
    user = db.query(User).filter(User.phone_number.in_([p_plus, p_91])).first()
    if user:
        return user

    user = User(
        phone_number=p_plus or phone,
        first_name=first_name,
        user_type="senior",
  
    
    )
    db.add(user)
    db.flush()
    return user


def _get_or_create_senior(db, user: User, first_name: str | None) -> Senior:
    senior = db.query(Senior).filter(Senior.user_id == user.id).first()
    if senior:
        return senior

    initial = ((first_name or user.first_name or "U").strip()[:1] or "U").upper()

    senior = Senior(
        user_id=user.id,
        initial=initial,
        dob=date(1970, 1, 1),
        whatsapp_msg=True,
    )
    db.add(senior)
    db.flush()
    return senior


def _subscription_sync(senior_id: int) -> None:
    db = SessionLocal()
    try:
        free_plan = (
            db.query(SubscriptionPlan)
            .filter(SubscriptionPlan.plan_type == "free")
            .first()
        )
        if not free_plan:
            return
        existing_subscription = (
            db.query(Subscription)
            .filter(Subscription.user_id == senior_id)
            .first()
        )
        if existing_subscription:
            return
        db.add(
            Subscription(
                user_id=senior_id,
                plan_id=free_plan.id,
                status="active",
            )
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Failed creating default subscription for senior_id=%s", senior_id)
    finally:
        db.close()


def _subscription(senior_id: int) -> None:
    asyncio.create_task(asyncio.to_thread(_subscription_sync, senior_id))


# =========================================================
# CONVERSATION LOGIC 
# =========================================================
def _get_or_create_conversation(db, senior: Senior, wati_conversation_id: str):
    if wati_conversation_id:
        conv = (
            db.query(Conversation)
            .filter(
                Conversation.wati_conversation_id == wati_conversation_id,
                Conversation.is_deleted.is_(False),
            )
            .first()
        )
        if conv:
            return conv

    latest = (
        db.query(Conversation)
        .filter(
            Conversation.senior_id == senior.id,
            Conversation.is_deleted.is_(False),
        )
        .order_by(desc(Conversation.created))
        .first()
    )

    if not latest or wati_conversation_id:
        conv = Conversation(
            senior_id=senior.id,
            wati_conversation_id=wati_conversation_id,
        )
        db.add(conv)
        db.flush()
        return conv

    return latest


# =========================================================
# THREAD LOGIC 
# =========================================================
def _get_or_create_thread(db, conversation: Conversation, now: datetime):
    last_thread = (
        db.query(Thread)
        .filter(Thread.conversation_id == conversation.id)
        .order_by(desc(Thread.created))
        .first()
    )

    last_message = (
        db.query(Message)
        .filter(
            Message.conversation_id == conversation.id,
            Message.is_deleted.is_(False),
        )
        .order_by(desc(Message.created))
        .first()
    )

    idle_hours = 1.0
    idle_cutoff = now - timedelta(hours=idle_hours)
    if (
        not last_thread
        or not last_message
        or last_message.created < idle_cutoff
    ):
        # free_thread_limit(db, conversation.id)
        thread = Thread(
            conversation_id=conversation.id,
            role="chatbot",
            status="assigned",
        )
        db.add(thread)
        db.flush()
        return thread, True

    return last_thread, False


# =========================================================
# PERSIST INCOMING MESSAGE
# =========================================================
def _persist_template_value(payload: dict, routed_message: str) -> str:  #need working on this, need to extract the llm message and then  insert into db
    """Prefer visible copy (body / selection text), not raw WhatsApp type like interactive."""
    body = str(payload.get("text") or "").strip()
    if body:
        return body[:4000]
    sel = (routed_message or "").strip()
    if sel:
        return sel[:4000]
    return (str(payload.get("type") or "text").strip() or "text")[:4000]


def _persist_incoming(phone: str, message: str, payload: dict):
    db = SessionLocal()
    now = datetime.now(UTC)

    try:
        first_name = _extract_name(payload)

        
        wati_conversation_id = str(payload.get("conversationId") or "").strip()
        wati_message_id = str(
            payload.get("whatsappMessageId") or payload.get("id") or ""
        ).strip()

        template_value = _persist_template_value(payload, message)

        user = _get_or_create_user(db, phone, first_name)
        senior = _get_or_create_senior(db, user, first_name)

        conversation = _get_or_create_conversation(
            db, senior, wati_conversation_id
        )

        thread, created_new_thread = _get_or_create_thread(db, conversation, now)

        msg = Message(
            conversation_id=conversation.id,
            thread_id=thread.id,
            user_message=message or "",
            template=template_value,
            message_source="wati",
            wati_message_id=wati_message_id,
        )

        db.add(msg)
        db.flush()

        message_id = msg.id

        db.commit()
        _subscription(senior.id)

        logger.info(
            "Saved message id=%s phone=%s conv=%s wati_conv=%s",
            message_id,
            phone,
            conversation.id,
            wati_conversation_id,
        )

        return message_id, thread.id, conversation.id, created_new_thread

    except Exception:
        db.rollback()
        logger.exception("DB persist failed")
        raise

    finally:
        db.close()


# =========================================================
# UPDATE BOT RESPONSE
# =========================================================

def _update_bot_response_sync(
    message_id: int,
    bot_response: str | None = None,
    template: str | None = None,
    message_source: str | None = None,
    confidence_score: float | None = None,
) -> None:
    db = SessionLocal()
    try:
        msg = db.query(Message).filter(Message.id == message_id).first()
        if msg:
            if bot_response is not None:
                msg.bot_response = bot_response
            if template is not None:
                msg.template = template
            if message_source is not None:
                msg.message_source = message_source
            if confidence_score is not None:
                msg.confidence_score = confidence_score
            db.commit()
        else:
            logger.warning("Message not found id=%s", message_id)
    except Exception:
        db.rollback()
        logger.exception("Failed updating bot_response")
    finally:
        db.close()


def _update_bot_response(
    message_id: int,
    bot_response: str | None = None,
    template: str | None = None,
    message_source: str | None = None,
    confidence_score: float | None = None,
) -> None:
    asyncio.create_task(
        asyncio.to_thread(
            _update_bot_response_sync,
            message_id,
            bot_response,
            template,
            message_source,
            confidence_score,
        )
    )


def resolve_thread_and_create_new_chatbot_thread(db, thread_id: int) -> int | None:
    """Resolve chatbot thread and start a fresh chatbot thread. Returns new thread id."""
    thread = db.query(Thread).filter(Thread.id == thread_id).first()
    if not thread:
        return None
    if thread.role != "chatbot":
        return None
    thread.status = "resolved"
    # free_thread_limit(db, thread.conversation_id)
    next_thread = Thread(
        conversation_id=thread.conversation_id,
        role="chatbot",
        status="assigned",
    )
    db.add(next_thread)
    db.flush()
    return next_thread.id


def resolve_assigned_chatbot_thread_and_create_techsaathi_thread(
    db, thread_id: int
) -> int | None:
    """
    Bot → human escalation: close the active bot thread and open a new human queue thread.
    Returns the new thread id, or None if the current thread is not an assigned chatbot thread.
    """
    thread = db.query(Thread).filter(Thread.id == thread_id).first()
    if not thread or thread.role != "chatbot" or thread.status != "assigned":
        return None
    thread.status = "resolved"
    # free_thread_limit(db, thread.conversation_id)
    human_thread = Thread(
        conversation_id=thread.conversation_id,
        role="techsaathi",
        status="assigned",
    )
    db.add(human_thread)
    db.flush()
    return human_thread.id


def resolve_techsaathi_thread_and_create_new_chatbot_thread(db, thread_id: int) -> int | None:
    """Resolve techsaathi thread and start a fresh chatbot thread."""
    thread = db.query(Thread).filter(Thread.id == thread_id).first()
    if not thread or thread.role != "techsaathi":
        return None
    thread.status = "resolved"
    # free_thread_limit(db, thread.conversation_id)
    next_thread = Thread(
        conversation_id=thread.conversation_id,
        role="chatbot",
        status="assigned",
    )
    db.add(next_thread)
    db.flush()
    return next_thread.id


def _reassign_message_thread(db, message_id: int, new_thread_id: int) -> None:
    msg = db.query(Message).filter(Message.id == message_id).first()
    if msg:
        msg.thread_id = new_thread_id


def append_human_operator_text_to_latest_message(
    phone: str, text: str, payload: dict
) -> None:
    body = (text or "").strip()
    if not body or not phone:
        return
    db = SessionLocal()
    try:
        digits = "".join(c for c in str(phone or "") if c.isdigit())
        p_91 = digits
        p_plus = f"+{digits}" if digits else ""
        user = db.query(User).filter(User.phone_number.in_([p_plus, p_91])).first()
        senior = user.senior_profile if user else None
        if not senior:
            return

        wati_conv = str(payload.get("conversationId") or "").strip()
        conv = None
        if wati_conv:
            conv = (
                db.query(Conversation)
                .filter(
                    Conversation.senior_id == senior.id,
                    Conversation.wati_conversation_id == wati_conv,
                    Conversation.is_deleted.is_(False),
                )
                .first()
            )
        if not conv:
            conv = (
                db.query(Conversation)
                .filter(
                    Conversation.senior_id == senior.id,
                    Conversation.is_deleted.is_(False),
                )
                .order_by(desc(Conversation.created))
                .first()
            )
        if not conv:
            return

        msg = (
            db.query(Message)
            .filter(
                Message.conversation_id == conv.id,
                Message.is_deleted.is_(False),
            )
            .order_by(desc(Message.created))
            .first()
        )
        if not msg:
            return

        existing = (msg.tech_saathi_response or "").strip()
        msg.tech_saathi_response = f"{existing}\n\n{body}" if existing else body
        thread = db.query(Thread).filter(Thread.id == msg.thread_id).first()
        if thread:
            if thread.role == "techsaathi":
                thread.status = "assigned"
            elif thread.role == "chatbot" and thread.status == "assigned":
                hid = resolve_assigned_chatbot_thread_and_create_techsaathi_thread(
                    db, thread.id
                )
                if hid:
                    msg.thread_id = hid
            elif thread.role == "chatbot" and thread.status == "resolved":
                # free_thread_limit(db, thread.conversation_id)
                human_thread = Thread(
                    conversation_id=thread.conversation_id,
                    role="techsaathi",
                    status="assigned",
                )
                db.add(human_thread)
                db.flush()
                msg.thread_id = human_thread.id
        db.commit()
        logger.info(
            "append_human_operator_text msg_id=%s thread_id=%s",
            msg.id,
            msg.thread_id,
        )
    except Exception:
        db.rollback()
        logger.exception(
            "append_human_operator_text_to_latest_message failed phone=%s", phone
        )
    finally:
        db.close()


async def _send_new_chatbot_thread_mode_prompt(
    phone: str, conversation_id: int, thread_id: int, message_id: int
) -> bool:
    """Send mode buttons when a new chatbot thread starts."""
    final_welcome_text = _dynamic_copy("welcome_back_context")
    topic_text = ""
    db = SessionLocal()
    try:
        recent_cutoff = datetime.now(UTC) - timedelta(seconds=60)
        recent_mode_msg = (
            db.query(Message)
            .filter(
                Message.thread_id == thread_id,
                Message.id != message_id,
                Message.is_deleted.is_(False),
                Message.message_source == "new_thread_reentry",
                Message.created >= recent_cutoff,
            )
            .order_by(desc(Message.created))
            .first()
        )
        if recent_mode_msg:
            logger.info(
                "Skipping duplicate new-thread prompt thread_id=%s message_id=%s",
                thread_id,
                message_id,
            )
            db.commit()
            return True

        prev_thread = (
            db.query(Thread)
            .filter(
                Thread.conversation_id == conversation_id,
                Thread.id != thread_id,
            )
            .order_by(desc(Thread.created))
            .first()
        )
        if prev_thread:
            prev_rows = (
                db.query(Message)
                .filter(
                    Message.thread_id == prev_thread.id,
                    Message.is_deleted.is_(False),
                )
                .order_by(desc(Message.created))
                .limit(5)
                .all()
            )
            prev_rows = list(reversed(prev_rows))
            previous_turns = []
            for row in prev_rows:
                user_text = (row.user_message or "").strip()
                bot_text = (row.bot_response or "").strip()
                if user_text:
                    previous_turns.append({"role": "user", "content": user_text})
                if bot_text:
                    previous_turns.append({"role": "assistant", "content": bot_text})
            topic_text = _dynamic_copy(
                "thread_topic",
                context={"previous_turns": previous_turns},
            )
            if topic_text:
                normalized = " ".join(topic_text.split())
                normalized = re.sub(
                    r"\b(resolved|fixed|solved|closed|completed|done)\b",
                    "",
                    normalized,
                    flags=re.IGNORECASE,
                ).strip(" .,-")
                if len(normalized) > 90:
                    normalized = normalized[:87].rstrip() + "..."
                if normalized:
                    final_welcome_text = _dynamic_copy(
                        "welcome_back_blended",
                        context={
                            "issue_topic": normalized,
                            "previous_turns": previous_turns,
                        },
                    )
                    # Safety guard: force positive tone even if model outputs negative phrasing.
                    final_welcome_text = re.sub(
                        r"\b(not resolved|wasn['’]?t resolved|unresolved|not fixed|didn['’]?t work|failed)\b",
                        "all good",
                        final_welcome_text,
                        flags=re.IGNORECASE,
                    )
                    if "how can i help" not in final_welcome_text.lower():
                        final_welcome_text = f"{final_welcome_text.rstrip(' .')} How can I help you today?"
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Failed preparing new-thread personalized welcome")
    finally:
        db.close()

    sent_mode = await send_interactive_buttons_message(
        phone,
        final_welcome_text,
        "mode_buttons",
    )
    _update_bot_response(
        message_id,
        bot_response=final_welcome_text,
        message_source="new_thread_reentry",
        confidence_score=None,
    )
    if not sent_mode:
        logger.error("Failed sending new-thread mode buttons to %s", phone)
    return sent_mode


# =========================================================
# MAIN ENTRYPOINT
# =========================================================
async def process_incoming_message(
    phone: str, message: str, payload: dict | None = None
):
    if not phone:
        logger.warning("Skipping message: no phone")
        return

    try:
        payload = payload or {}
        incoming_message = message
        button_reply_id = _extract_button_reply_id(payload)

        override = None

        # Block inactive seniors before persisting, so no new conversation/thread/message is created.
        digits = "".join(c for c in str(phone or "") if c.isdigit())
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.phone_number.in_([f"+{digits}", digits])).first()
            senior = db.query(Senior).filter(Senior.user_id == user.id).first() if user else None
            if senior and not senior.is_active:
                return
        finally:
            db.close()

        # 1. Save incoming message
        message_id, thread_id, conversation_id, _created_new_thread = _persist_incoming(
            phone, incoming_message, payload
        )

        intent_result = {}
        classified_intent = ""
        issue_followup_depth = 1
        same_issue_as_previous = False
        is_unresolved_followup = False
        history_for_llm: list[dict] | None = None
        awaiting_handoff_confirm = False
        confirm_reply = ""
        declined_handoff = False
        ask_handoff_confirmation = False

        db = SessionLocal()
        try:
            history_for_llm = _build_history_messages(
                db, thread_id, message_id, incoming_message
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

        should_run_classifier = bool(incoming_message.strip()) or bool(button_reply_id)
        if should_run_classifier:
            db = SessionLocal()
            try:
                intent_result = await classify_wati_turn_intent(
                    db,
                    thread_id=thread_id,
                    current_message_id=message_id,
                    current_message=incoming_message,
                    payload=payload,
                    button_reply_id=button_reply_id,
                    history=history_for_llm,
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

            classified_intent = (intent_result.get("intent") or "").strip().upper()
            same_issue_as_previous = bool(intent_result.get("same_issue_as_previous"))
            is_unresolved_followup = bool(intent_result.get("is_unresolved_followup"))
            raw_depth = intent_result.get("issue_followup_depth")
            issue_followup_depth = raw_depth if isinstance(raw_depth, int) else 1
            if issue_followup_depth < 1:
                issue_followup_depth = 1
            logger.info(
                "LLM_INTENT intent=%s phone=%s same_issue=%s unresolved_followup=%s depth=%s",
                classified_intent,
                phone,
                same_issue_as_previous,
                is_unresolved_followup,
                issue_followup_depth,
            )
        # Deterministic signal for unresolved button taps.
        # Button input is an explicit unresolved follow-up on current issue.
        if button_reply_id == "not_resolved":
            is_unresolved_followup = True
            same_issue_as_previous = True
            issue_followup_depth = max(issue_followup_depth, 2)
        db = SessionLocal()
        try:
            last_msg = (
                db.query(Message)
                .filter(
                    Message.thread_id == thread_id,
                    Message.is_deleted.is_(False),
                    Message.id != message_id,
                )
                .order_by(desc(Message.created))
                .first()
            )
            awaiting_handoff_confirm = bool(
                last_msg and (last_msg.message_source or "").strip() == "handoff_confirmation"
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        if awaiting_handoff_confirm:
            confirm_reply = _handoff_confirm_decision(incoming_message)
            declined_handoff = confirm_reply == "NO"
        is_unresolved = button_reply_id == "not_resolved" or is_unresolved_followup

        if classified_intent == "RESOLVED":
            control_result = conversation_control.invoke({"action": "resolved"})
            override = {
                "kind": "control",
                "action": (control_result.get("action") or "").strip(),
                "message_source": "intent_classifier",
                "confidence_score": None,
            }

        # --- Human ticket resolved (WATI UI) ---
        ticket_raw = str(payload.get("ticketStatus") or payload.get("ticket_status") or "").strip().upper()
        if not ticket_raw and isinstance(payload.get("ticket"), dict):
            tk = payload["ticket"]
            ticket_raw = str(tk.get("status") or tk.get("ticketStatus") or "").strip().upper()
        if ticket_raw == "SOLVED":
            db = SessionLocal()
            try:
                thread_row = db.query(Thread).filter(Thread.id == thread_id).first()
                if thread_row:
                    logger.info(
                        "human_resolution_detected thread_id=%s phone=%s",
                        thread_id,
                        phone,
                    )
                    resolve_thread_and_create_new_chatbot_thread(db, thread_id)
                    db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()
            return

        # --- Human queue (getMessages) or bot → human escalation ---
        handoff_needed = False
        handoff_count = 0
        switched_from_human_to_chatbot_this_turn = False
        db = SessionLocal()
        try:
            thread_row = db.query(Thread).filter(Thread.id == thread_id).first()

            if thread_row and thread_row.role == "techsaathi":
                ext_state = await read_wati_timeline_state(str(phone or "").strip())

                logger.error(
                    "DEBUG_TIMELINE thread=%s ext_state=%s phone=%s",
                    thread_id,
                    ext_state,
                    phone,
                )

                if ext_state == "resolved":
                    old_thread_id = thread_id
                    new_thread_id = resolve_techsaathi_thread_and_create_new_chatbot_thread(
                        db, thread_id
                    )
                    if new_thread_id:
                        _reassign_message_thread(db, message_id, new_thread_id)
                        thread_id = new_thread_id
                        thread_row = db.query(Thread).filter(Thread.id == thread_id).first()
                        switched_from_human_to_chatbot_this_turn = True

                    db.commit()

                    logger.info(
                        "TECHSAATHI_RESOLVED_SWITCH_TO_BOT old_thread=%s new_thread=%s phone=%s",
                        old_thread_id,
                        new_thread_id,
                        phone,
                    )
                else:
                    thread_row.status = "assigned"
                    db.commit()

                    logger.info(
                        "HUMAN_ACTIVE thread_id=%s phone=%s",
                        thread_id,
                        phone,
                    )

                    _update_bot_response(
                        message_id,
                        bot_response="",
                        message_source="human_active",
                    )

                    return

            if (
                wati_payload_indicates_human_operator(payload)
                and thread_row
                and thread_row.role == "chatbot"
                and thread_row.status == "assigned"
            ):
                hid = resolve_assigned_chatbot_thread_and_create_techsaathi_thread(
                    db, thread_id
                )
                if hid:
                    _reassign_message_thread(db, message_id, hid)
                    thread_id = hid
                db.commit()
                logger.info(
                    "escalate_human_operator_wati thread_id=%s phone=%s",
                    thread_id,
                    phone,
                )
                _update_bot_response(
                    message_id,
                    bot_response="",
                    message_source="human_wati",
                )
                return

            if thread_row and thread_row.role == "chatbot" and thread_row.status == "assigned":
                classifier_requested_handoff = (
                    classified_intent == "REQUEST_HUMAN" and not declined_handoff
                )
                if (
                    awaiting_handoff_confirm
                    and confirm_reply == "YES"
                    and not switched_from_human_to_chatbot_this_turn
                ):
                    hid = resolve_assigned_chatbot_thread_and_create_techsaathi_thread(
                        db, thread_id
                    )
                    if hid:
                        _reassign_message_thread(db, message_id, hid)
                        thread_id = hid
                    handoff_needed = True
                    handoff_count = 1
                elif (
                    classifier_requested_handoff
                    and not switched_from_human_to_chatbot_this_turn
                ):
                    ask_handoff_confirmation = True
                elif (
                    same_issue_as_previous
                    and is_unresolved
                    and issue_followup_depth >= 3
                ):
                    hid = resolve_assigned_chatbot_thread_and_create_techsaathi_thread(
                        db, thread_id
                    )
                    if hid:
                        _reassign_message_thread(db, message_id, hid)
                        thread_id = hid
                    handoff_needed = True
                    handoff_count = issue_followup_depth
                    logger.info(
                        "handoff_by_issue_followup_depth thread_id=%s phone=%s issue_followup_depth=%s",
                        thread_id,
                        phone,
                        issue_followup_depth,
                    )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

        if awaiting_handoff_confirm and confirm_reply == "UNCLEAR":
            confirm_text = _dynamic_copy("handoff_confirm")
            await send_message(phone, confirm_text)
            _update_bot_response(
                message_id,
                bot_response=confirm_text,
                message_source="handoff_confirmation",
                confidence_score=None,
            )
            return

        if handoff_needed:
            teams = ["support"]
            base = settings.WATI_API_ENDPOINT.rstrip("/")
            headers = {
                "Authorization": f"Bearer {settings.WATI_AUTH_TOKEN}",
                "Content-Type": "application/json",
            }
            assignee_email = (
                getattr(settings, "WATI_SAKSHAM_ASSIGNEE_EMAIL", None)
                or getattr(settings, "WATI_HANDOFF_ASSIGNEE_EMAIL", None)
                or ""
            ).strip()
            # ===== switch assignee source to DB  =====
            # assignee_email = _pick_handoff_assignee_email() or (
            #     getattr(settings, "WATI_SAKSHAM_ASSIGNEE_EMAIL", None)
            #     or getattr(settings, "WATI_HANDOFF_ASSIGNEE_EMAIL", None)
            #     or ""
            # ).strip()

            channel_number = (getattr(settings, "WATI_CHANNEL_NUMBER", None) or "").strip()

            tenant = (getattr(settings, "WATI_TENANT_ID", None) or "").strip()
            assign_team_url = f"{base}/api/v1/assignTeam"
            if tenant and not base.rstrip("/").endswith(tenant):
                assign_team_url = f"{base}/{tenant}/api/v1/assignTeam"
            assign_operator_url = f"{base}/api/v1/assignOperator"
            if tenant and not base.rstrip("/").endswith(tenant):
                assign_operator_url = f"{base}/{tenant}/api/v1/assignOperator"

            try:
                async with httpx.AsyncClient(timeout=20.0) as client:
                    if teams:
                        r = await client.post(
                            assign_team_url,
                            headers=headers,
                            params={"whatsappNumber": phone},
                            json={"teams": teams},
                        )
                        logger.info(
                            "WATI assignTeam status=%s text=%s url=%s",
                            r.status_code,
                            r.text,
                            assign_team_url,
                        )
                    if assignee_email and phone:
                        r = await client.post(
                            assign_operator_url,
                            headers=headers,
                            params={"whatsappNumber": phone, "email": assignee_email},
                        )
                        logger.info(
                            "WATI assignOperator status=%s text=%s url=%s",
                            r.status_code,
                            r.text,
                            assign_operator_url,
                        )
                    if channel_number and phone:
                        r = await client.post(
                            f"{base}/api/v1/updateChatStatus",
                            headers=headers,
                            json={
                                "ticketStatus": "OPEN",
                                "whatsappNumber": phone,
                                "channelPhoneNumber": channel_number,
                            },
                        )
                        logger.info(
                            "WATI updateChatStatus OPEN status=%s text=%s",
                            r.status_code,
                            r.text,
                        )
            except Exception:
                logger.exception("WATI handoff request failed")

            wait_text = _dynamic_copy("handoff_wait")
            await send_message(phone, wait_text)
            logger.info(
                "handoff_triggered thread_id=%s phone=%s signal_count=%s",
                thread_id,
                phone,
                handoff_count,
            )
            _update_bot_response(
                message_id,
                bot_response=wait_text,
                message_source="handoff",
            )
            return

        if ask_handoff_confirmation or (
            classified_intent == "REQUEST_HUMAN"
            and not awaiting_handoff_confirm
            and not declined_handoff
        ):
            confirm_text = _dynamic_copy("handoff_confirm")
            await send_message(phone, confirm_text)
            _update_bot_response(
                message_id,
                bot_response=confirm_text,
                message_source="handoff_confirmation",
                confidence_score=None,
            )
            return

        # 2. Send re-entry prompt only when user sends first message in a fresh chatbot thread.
        db = SessionLocal()
        try:
            thread_row = db.query(Thread).filter(Thread.id == thread_id).first()
            prior_msg_in_same_thread = (
                db.query(Message)
                .filter(
                    Message.thread_id == thread_id,
                    Message.is_deleted.is_(False),
                    Message.id != message_id,
                )
                .first()
            )
            prior_thread_exists = (
                db.query(Thread)
                .filter(
                    Thread.conversation_id == conversation_id,
                    Thread.id != thread_id,
                )
                .first()
            )
            should_send_reentry = bool(
                thread_row
                and thread_row.role == "chatbot"
                and not prior_msg_in_same_thread
                and prior_thread_exists
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

        if should_send_reentry:
            await _send_new_chatbot_thread_mode_prompt(
                phone=phone,
                conversation_id=conversation_id,
                thread_id=thread_id,
                message_id=message_id,
            )
            return

        # 3. Generate response from history + tool calling
        db = SessionLocal()
        try:
            turn_meta={
                "button_reply_id": button_reply_id or "",
                "unresolved_signal_current_turn": is_unresolved,
                "unresolved_rounds": issue_followup_depth,
            }
            llm_result = override or await generate_wati_reply(
                db,
                thread_id,
                message_id,
                incoming_message,
                turn_meta=turn_meta,
                history=history_for_llm,
            )

            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

        # 4. Send mapped WATI response
        sent = False
        bot_response = ""
        response_source = (llm_result.get("message_source") or "").strip()
        response_confidence = llm_result.get("confidence_score")
        if not isinstance(response_confidence, (int, float)):
            response_confidence = None
        if llm_result.get("kind") == "control":
            control_action = (llm_result.get("action") or "").strip().lower()
            if control_action == "resolved":
                db = SessionLocal()
                try:
                    new_thread_id = resolve_thread_and_create_new_chatbot_thread(
                        db, thread_id
                    )
                    db.commit()
                except Exception:
                    db.rollback()
                    raise
                finally:
                    db.close()

                ack_text = _dynamic_copy("resolved_ack")
                sent = await send_message(phone, ack_text)
                bot_response = ack_text
                if not response_source:
                    response_source = "conversation_control"
            else:
                sent = True
                bot_response = ""
        elif llm_result.get("kind") == "action":
            action = llm_result.get("action")
            action_message = (llm_result.get("message") or "").strip() or _default_button_message(action)
            bot_response = action_message
            if action == "platform_buttons":
                sent = await send_interactive_platform_list_message(phone, action_message)
            else:
                sent = await send_interactive_buttons_message(phone, action_message, action)
        else:
            bot_response = (llm_result.get("message") or "").strip()
            sent = await send_message(phone, bot_response)

        # 5. Save bot response
        _update_bot_response(
            message_id,
            bot_response=bot_response,
            message_source=response_source if response_source else "llm",
            confidence_score=response_confidence,
        )

        if not sent:
            logger.error("Failed sending message to %s", phone)

    # except FreeTierThreadLimitReached:
    #     upgrade_text = (
    #         "You have reached your free plan limit. "
    #         "Please upgrade to continue."
    #     )
    #     await send_message(phone, upgrade_text)
    #     try:
    #         _update_bot_response(
    #             message_id,
    #             bot_response=upgrade_text,
    #             message_source="free_thread_limit",
    #             confidence_score=None,
    #         )
    #     except Exception:
    #         pass
    #     return
    except Exception:
        logger.exception("Processing failed for phone=%s", phone)