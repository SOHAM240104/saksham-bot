import asyncio
import contextlib
import json
import logging
from datetime import UTC, date, datetime, timedelta
import httpx
from sqlalchemy import desc

from app.config.base import SessionLocal
from app.models.chat.chat import Conversation, Message, Thread
from app.models.senior import Senior
from app.models.subscriptions import Subscription, SubscriptionPlan
# from app.models.techsaathi import TechSaathi
from app.models.user import User
from wati.services.conversation import (
    _assistant_in_troubleshooting_arc,
    _build_history_messages,
    _is_on_tech_path,
    _is_platform_only_user_message,
    _is_unresolved_negative_only,
    _name_for_thread,
    _pending_user_issue_from_history,
    _platform_label,
    _platform_slug_from_senior,
    _prior_thread_snapshot,
    _unsupported_refinement_declined_reply,
    _unsupported_refinement_offer_reply,
    classify_wati_turn_intent,
    conversation_control,
    generate_wati_reply,
    refinement_platform_for_turn,
    resolve_scam_context_from_turn,
    save_senior_platform_from_turn,
    thread_has_platform_issue_prompt,
    _platform_slug_from_turn,
    _senior_for_thread,
    extract_os_context_llm,
    resolve_issue_continuity,
)
from wati.services.supported_refinements import check_platform_refinement_from_extraction
from wati.services.unresolved_flow import (
    resolve_unresolved_phase,
    should_auto_handoff_by_unresolved_depth,
)
from wati.services.dynamic_copy import dynamic_copy as _dynamic_copy
from wati.settings import settings

from .parse import (
    _extract_button_reply_id,
    _extract_name,
    _is_handoff_confirmation_message,
    _yes_no_confirm_decision,
    wati_payload_indicates_human_operator,
)
# Free-tier thread limit (disabled )
# from .feature import FreeTierThreadLimitReached, free_thread_limit
from .wati_webhook import (
    keep_typing_indicator,
    send_interactive_buttons_message,
    send_interactive_platform_list_message,
    send_message,
    send_template_message,
    send_typing_indicator,
)

logger = logging.getLogger("wati.services.handling")


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

def _default_button_message(action: str) -> str:
    #users always see readable caption text with button/list messages when the main LLM doesn’t supply one.
    if action == "mode_buttons":
        return "How can I help you today?"
    if action == "platform_buttons":
        return "Which phone are you using?"
    if action == "feedback_buttons":
        return _dynamic_copy("feedback_checkin")
    if action == "scam_os_buttons":
        return "To guide you better, is your phone an iPhone/iPad or Android?"
    return "Please choose an option."


def _classify_wati_turn_intent_in_thread(
    thread_id: int,
    message_id: int,
    incoming_message: str,
    payload: dict,
    button_reply_id: str | None,
    history_for_llm: list[dict] | None,
) -> dict:
    db = SessionLocal()
    try:
        result = asyncio.run(
            classify_wati_turn_intent(
                db,
                thread_id=thread_id,
                current_message_id=message_id,
                current_message=incoming_message,
                payload=payload,
                button_reply_id=button_reply_id,
                history=history_for_llm,
            )
        )
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()



def _extract_os_context_in_thread(
    incoming_message: str,
    history_for_llm: list[dict] | None,
    stored_platform: str,
    button_reply_id: str | None,
) -> dict:
    return asyncio.run(
        extract_os_context_llm(
            incoming_message,
            history_for_llm,
            stored_platform=stored_platform or "",
            button_reply_id=button_reply_id or "",
        )
    )


def _generate_wati_reply_in_thread(
    thread_id: int,
    message_id: int,
    incoming_message: str,
    turn_meta: dict,
    history_for_llm: list[dict] | None,
) -> dict:
    db = SessionLocal()
    try:
        result = asyncio.run(
            generate_wati_reply(
                db,
                thread_id,
                message_id,
                incoming_message,
                turn_meta=turn_meta,
                history=history_for_llm,
            )
        )
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

# This function is used to stop the typing indicator.
async def _stop_typing_indicator(
    typing_stop: asyncio.Event,
    typing_task: asyncio.Task,
) -> None:
    typing_stop.set()
    typing_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await typing_task


# This function is used to get or create a user.
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

# This function is used to get or create a senior.
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

# This function is used to create a subscription for a senior.
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

# This function is used to write in the db the incoming message.
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
# This function is used to update the bot response in the db.
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
                prev = (msg.message_source or "").strip()
                if prev.startswith("scam_os:") and not message_source.startswith("scam_os:"):
                    message_source = f"{prev},{message_source}"
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
        button_reply_id = _extract_button_reply_id(payload) #Quick-reply buttons arrive as structured payload, not plain text.
        if not button_reply_id and _is_unresolved_negative_only(incoming_message, ""):
            button_reply_id = "not_resolved"

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
        # This is the default values for the intent result.
        intent_result = {}
        classified_intent = ""
        issue_followup_depth = 1
        same_issue_as_previous = False
        is_unresolved_followup = False
        history_for_llm: list[dict] | None = None
        awaiting_handoff_confirm = False
        awaiting_platform_confirm = False
        awaiting_unsupported_refinement_confirm = False
        confirm_reply = ""
        platform_confirm_reply = ""
        unsupported_refinement_reply = ""
        use_platform_only_retrieval = False
        last_bot_response = ""
        last_bot_message_source = ""
        platform_saved_slug = ""
        declined_handoff = False
        ask_handoff_confirmation = False

        db = SessionLocal()
        try:
            history_for_llm = _build_history_messages(       #LLM needs prior turns; built once per inbound message.

                db, thread_id, message_id, incoming_message
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

        db = SessionLocal()
        try:
            user = db.query(User).filter(User.phone_number.in_([f"+{digits}", digits])).first()
            senior = db.query(Senior).filter(Senior.user_id == user.id).first() if user else None
            if senior and _is_platform_only_user_message(incoming_message, button_reply_id or ""):
                picked_slug = save_senior_platform_from_turn(
                    db, senior, incoming_message, button_reply_id or ""
                )
                if not picked_slug:
                    picked_slug = _platform_slug_from_turn(
                        incoming_message, button_reply_id or ""
                    )
                platform_saved_slug = picked_slug
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

        from .techsaathi_poll import handle_techsaathi_incoming_message

        # --- TechSaathi gate (poll module) ---
        # Before classifier/LLM: if user is with a human, either stay silent or
        # switch back to bot when WATI shows the agent closed the chat.
        techsaathi_turn = await handle_techsaathi_incoming_message(
            thread_id, message_id, phone
        )
        if techsaathi_turn.kind == "human_active":
            logger.info("HUMAN_ACTIVE thread_id=%s phone=%s", thread_id, phone)
            _update_bot_response(
                message_id,
                bot_response="",
                message_source="human_active",
            )
            return
        if techsaathi_turn.kind == "resolved":
            # Poll path already sent welcome buttons; record that on this message row.
            _update_bot_response(
                message_id,
                bot_response=techsaathi_turn.welcome_text,
                message_source=techsaathi_turn.welcome_source,
            )
            return

        on_techsaathi_thread = False
        active_techsaathi_thread = False
        db = SessionLocal()
        try:
            thread_row = db.query(Thread).filter(Thread.id == thread_id).first()
            on_techsaathi_thread = bool(thread_row and thread_row.role == "techsaathi")
            active_techsaathi_thread = bool(
                thread_row
                and thread_row.role == "techsaathi"
                and thread_row.status == "assigned"
            )
        finally:
            db.close()

        # Safety net: never run LLM while human is still assigned.
        if active_techsaathi_thread:
            logger.warning(
                "TECHSAATHI_ACTIVE_SAFETY_RETURN thread_id=%s phone=%s",
                thread_id,
                phone,
            )
            _update_bot_response(
                message_id,
                bot_response="",
                message_source="human_active",
            )
            return

        typing_stop = asyncio.Event()
        typing_task = asyncio.create_task(keep_typing_indicator(phone, typing_stop))
        await send_typing_indicator(phone)
        try:
            intent_result: dict = {}
            os_extraction: dict = {}
            should_run_classifier = (
                (bool(incoming_message.strip()) or bool(button_reply_id))
                and not on_techsaathi_thread  # no intent LLM while on human thread
            )
            if should_run_classifier:   #Run classifier only if there is text or button id, and not on_techsaathi_thread
                intent_result = await asyncio.to_thread(
                    _classify_wati_turn_intent_in_thread,
                    thread_id,
                    message_id,
                    incoming_message,
                    payload,
                    button_reply_id,
                    history_for_llm,
                )
                # Intent result is a dictionary with the intent, active_branch, same_issue_as_previous, is_unresolved_followup, issue_followup_depth.

                classified_intent = (intent_result.get("intent") or "").strip().upper()
                logger.info(
                    "LLM_INTENT intent=%s active_branch=%s phone=%s",
                    classified_intent,
                    intent_result.get("active_branch"),
                    phone,
                )

            support_mode, scam_context = resolve_scam_context_from_turn(
                history_for_llm or [],
                incoming_message,
                button_reply_id or "",
                intent_result=intent_result,
            )
            if (
                button_reply_id in {"ios", "android"}
                and scam_context
                and (support_mode or "").strip().lower() != "tech"
            ):
                _update_bot_response(
                    message_id,
                    message_source=f"scam_os:{button_reply_id}",
                )
            logger.info(
                "ROUTE_CONTEXT phone=%s button=%s support_mode=%s active_branch=%s scam_context=%s msg_preview=%s",
                phone,
                (button_reply_id or "")[:16],
                support_mode,
                (intent_result.get("active_branch") or ""),
                scam_context,
                (incoming_message or "")[:80],
            )

            if scam_context:
                same_issue_as_previous = False
                issue_followup_depth = 1
                is_unresolved_followup = False
            elif _is_on_tech_path(
                support_mode,
                incoming_message,
                button_reply_id or "",
                active_branch=(intent_result.get("active_branch") or "").strip().lower(),
            ):
                continuity = resolve_issue_continuity(
                    history=history_for_llm or [],
                    current_message=incoming_message,
                    button_reply_id=button_reply_id or "",
                    on_tech_path=True,
                )
                same_issue_as_previous = bool(continuity.get("same_issue_as_previous"))
                issue_followup_depth = int(continuity.get("issue_followup_depth") or 1)
                is_unresolved_followup = bool(continuity.get("is_unresolved_followup"))
                if issue_followup_depth < 1:
                    issue_followup_depth = 1
                logger.info(
                    "ISSUE_CONTINUITY phone=%s same_issue=%s depth=%s sig=%s unresolved=%s",
                    phone,
                    same_issue_as_previous,
                    issue_followup_depth,
                    continuity.get("issue_signature"),
                    is_unresolved_followup,
                )
            else:
                same_issue_as_previous = False
                issue_followup_depth = 1
                is_unresolved_followup = False
            db = SessionLocal()
            #Load previous message in thread; if last bot message was handoff confirmation → awaiting_handoff_confirm = True.
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
                    last_msg
                    and (
                        (last_msg.message_source or "").strip() == "handoff_confirmation"
                        or _is_handoff_confirmation_message(last_msg.bot_response or "")
                    )
                )
                awaiting_platform_confirm = bool(
                    last_msg
                    and (last_msg.message_source or "").strip() == "platform_confirmation"
                )
                awaiting_unsupported_refinement_confirm = bool(
                    last_msg
                    and "unsupported_refinement_confirmation"
                    in (last_msg.message_source or "")
                )
                last_bot_response = (last_msg.bot_response or "").strip() if last_msg else ""
                last_bot_message_source = (last_msg.message_source or "").strip() if last_msg else ""
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()
            if awaiting_handoff_confirm:
                confirm_reply = _yes_no_confirm_decision(incoming_message)
                declined_handoff = confirm_reply == "NO"
            if awaiting_platform_confirm:
                platform_confirm_reply = _yes_no_confirm_decision(incoming_message)
            if awaiting_unsupported_refinement_confirm:
                unsupported_refinement_reply = _yes_no_confirm_decision(incoming_message)
                if unsupported_refinement_reply == "YES":
                    use_platform_only_retrieval = True
                    issue_followup_depth = max(issue_followup_depth, 3)
                    is_unresolved = False
                elif unsupported_refinement_reply == "NO":
                    is_unresolved = False
            is_unresolved = button_reply_id == "not_resolved" or is_unresolved_followup
            if scam_context:
                is_unresolved = False
                issue_followup_depth = 1

            on_tech_path_for_phase = _is_on_tech_path(
                support_mode,
                incoming_message,
                button_reply_id or "",
                active_branch=(intent_result.get("active_branch") or "").strip().lower(),
            )
            unresolved_phase = resolve_unresolved_phase(
                on_tech_path=on_tech_path_for_phase,
                scam_mode=bool(scam_context),
                same_issue_as_previous=same_issue_as_previous,
                button_reply_id=button_reply_id or "",
                current_message=incoming_message,
                last_bot_message_source=last_bot_message_source,
                last_bot_response=last_bot_response,
            )
            logger.info(
                "UNRESOLVED_PHASE phase=%s last_bot_source=%s phone=%s depth=%s",
                unresolved_phase,
                (last_bot_message_source or "")[:40],
                phone,
                issue_followup_depth,
            )

            if classified_intent == "RESOLVED":
                # On scam path, only the main LLM closes via conversation_control (RULE 0).
                allow_resolve = not scam_context
                if allow_resolve:
                    control_result = conversation_control.invoke({"action": "resolved"})
                    override = {
                        "kind": "control",
                        "action": (control_result.get("action") or "").strip(),
                        "message_source": "intent_classifier",
                        "confidence_score": None,
                    }

            # --- WATI ticketStatus=SOLVED webhook (agent closed in WATI UI) ---
            ticket_raw = str(payload.get("ticketStatus") or payload.get("ticket_status") or "").strip().upper()
            if not ticket_raw and isinstance(payload.get("ticket"), dict):
                tk = payload["ticket"]
                ticket_raw = str(tk.get("status") or tk.get("ticketStatus") or "").strip().upper()
            if ticket_raw == "SOLVED":
                from .techsaathi_poll import (
                    resolve_techsaathi_thread_and_create_new_chatbot_thread,
                    send_post_techsaathi_return_welcome,
                )

                db = SessionLocal()
                try:
                    thread_row = db.query(Thread).filter(Thread.id == thread_id).first()
                    if thread_row:
                        logger.info(
                            "human_resolution_detected thread_id=%s phone=%s role=%s",
                            thread_id,
                            phone,
                            thread_row.role,
                        )
                        # Techsaathi handoff: same thread switch + welcome as the poll path.
                        if (
                            thread_row.role == "techsaathi"
                            and thread_row.status == "assigned"
                        ):
                            new_id = resolve_techsaathi_thread_and_create_new_chatbot_thread(
                                db, thread_id
                            )
                            if new_id:
                                await send_post_techsaathi_return_welcome(
                                    db,
                                    conversation_id=thread_row.conversation_id,
                                    new_thread_id=new_id,
                                    phone=phone,
                                )
                        else:
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
                    elif should_auto_handoff_by_unresolved_depth(
                        scam_mode=bool(scam_context),
                        on_tech_path=on_tech_path_for_phase,
                        same_issue_as_previous=same_issue_as_previous,
                        button_reply_id=button_reply_id or "",
                        current_message=incoming_message,
                        issue_followup_depth=issue_followup_depth,
                        unresolved_phase=unresolved_phase,
                        last_bot_message_source=last_bot_message_source,
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

            if awaiting_unsupported_refinement_confirm:
                db = SessionLocal()
                try:
                    user = db.query(User).filter(User.phone_number.in_([f"+{digits}", digits])).first()
                    senior = db.query(Senior).filter(Senior.user_id == user.id).first() if user else None
                    stored_slug = _platform_slug_from_senior(db, senior)
                    customer_name = _name_for_thread(db, thread_id)
                finally:
                    db.close()
                if unsupported_refinement_reply == "UNCLEAR":
                    confirm_text = last_bot_response or (
                        "Would you like me to try general steps without that version? "
                        "Please reply Yes or No."
                    )
                    await send_message(phone, confirm_text)
                    _update_bot_response(
                        message_id,
                        bot_response=confirm_text,
                        message_source="unsupported_refinement_confirmation,dynamic_copy",
                        confidence_score=None,
                    )
                    return
                if unsupported_refinement_reply == "NO":
                    declined = _unsupported_refinement_declined_reply(
                        customer_name=customer_name,
                        platform=stored_slug,
                    )
                    decline_text = (declined.get("message") or "").strip()
                    await send_message(phone, decline_text)
                    _update_bot_response(
                        message_id,
                        bot_response=decline_text,
                        message_source="unsupported_refinement_declined,dynamic_copy",
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
                and not switched_from_human_to_chatbot_this_turn
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

            if platform_saved_slug:
                on_tech_path_now = _is_on_tech_path(
                    support_mode,
                    incoming_message,
                    button_reply_id or "",
                    active_branch=(intent_result.get("active_branch") or "").strip().lower(),
                )
                pending_issue = _pending_user_issue_from_history(
                    history_for_llm or [],
                    incoming_message,
                    button_reply_id or "",
                )
                if on_tech_path_now and not scam_context and not pending_issue:
                    db = SessionLocal()
                    try:
                        customer_name = _name_for_thread(db, thread_id)
                    finally:
                        db.close()
                    issue_text = _dynamic_copy(
                        "platform_issue_ask",
                        context={
                            "platform_label": _platform_label(platform_saved_slug),
                            "customer_name": customer_name,
                        },
                    )
                    await send_message(phone, issue_text)
                    _update_bot_response(
                        message_id,
                        bot_response=issue_text,
                        message_source="platform_issue_prompt,dynamic_copy",
                        confidence_score=None,
                    )
                    return

            if awaiting_platform_confirm:
                db = SessionLocal()
                try:
                    user = db.query(User).filter(User.phone_number.in_([f"+{digits}", digits])).first()
                    senior = db.query(Senior).filter(Senior.user_id == user.id).first() if user else None
                    stored_slug = _platform_slug_from_senior(db, senior)
                    customer_name = _name_for_thread(db, thread_id)
                finally:
                    db.close()
                copy_ctx = {
                    "platform_label": _platform_label(stored_slug),
                    "customer_name": customer_name,
                }
                if platform_confirm_reply == "UNCLEAR":
                    confirm_text = _dynamic_copy("platform_still_using", context=copy_ctx)
                    await send_message(phone, confirm_text)
                    _update_bot_response(
                        message_id,
                        bot_response=confirm_text,
                        message_source="platform_confirmation",
                        confidence_score=None,
                    )
                    return
                if platform_confirm_reply == "NO":
                    list_msg = _default_button_message("platform_buttons")
                    await send_interactive_platform_list_message(phone, list_msg)
                    _update_bot_response(
                        message_id,
                        bot_response=list_msg,
                        message_source="platform_list",
                        confidence_score=None,
                    )
                    return
                if platform_confirm_reply == "YES":
                    issue_text = _dynamic_copy("platform_issue_ask", context=copy_ctx)
                    await send_message(phone, issue_text)
                    _update_bot_response(
                        message_id,
                        bot_response=issue_text,
                        message_source="platform_issue_prompt,dynamic_copy",
                        confidence_score=None,
                    )
                    return

            if (
                not scam_context
                and not awaiting_handoff_confirm
                and not awaiting_platform_confirm
                and not awaiting_unsupported_refinement_confirm
                and not use_platform_only_retrieval
            ):
                on_tech_path_now = _is_on_tech_path(
                    support_mode,
                    incoming_message,
                    button_reply_id or "",
                    active_branch=(intent_result.get("active_branch") or "").strip().lower(),
                )
                db = SessionLocal()
                try:
                    refinement_platform = refinement_platform_for_turn(
                        db, thread_id, history_for_llm or []
                    )
                    customer_name = _name_for_thread(db, thread_id)
                    senior_row = _senior_for_thread(db, thread_id)
                    stored_platform = _platform_slug_from_senior(db, senior_row) or ""
                finally:
                    db.close()
                os_extraction = {}
                if (
                    on_tech_path_now
                    and refinement_platform
                    and refinement_platform != "samsung"
                ):
                    os_extraction = await asyncio.to_thread(
                        _extract_os_context_in_thread,
                        incoming_message,
                        history_for_llm,
                        stored_platform,
                        button_reply_id,
                    )
                if on_tech_path_now and refinement_platform:
                    refinement_check = check_platform_refinement_from_extraction(
                        os_extraction,
                        refinement_platform,
                        text=incoming_message,
                    )
                    if refinement_check.get("status") == "unsupported":
                        offer = _unsupported_refinement_offer_reply(
                            customer_name=customer_name,
                            platform=refinement_platform,
                            unsupported_label=refinement_check.get("label") or "",
                            refinement_type=refinement_check.get("refinement_type") or "",
                        )
                        offer_text = (offer.get("message") or "").strip()
                        await send_message(phone, offer_text)
                        _update_bot_response(
                            message_id,
                            bot_response=offer_text,
                            message_source="unsupported_refinement_confirmation,dynamic_copy",
                            confidence_score=None,
                        )
                        return

            if (
                not scam_context
                and not awaiting_handoff_confirm
                and not awaiting_platform_confirm
                and not awaiting_unsupported_refinement_confirm
            ):
                on_tech_path_now = _is_on_tech_path(
                    support_mode,
                    incoming_message,
                    button_reply_id or "",
                    active_branch=(intent_result.get("active_branch") or "").strip().lower(),
                )
                pending_issue = _pending_user_issue_from_history(
                    history_for_llm or [],
                    incoming_message,
                    button_reply_id or "",
                )
                platform_pick_this_turn = _is_platform_only_user_message(
                    incoming_message, button_reply_id or ""
                )
                db = SessionLocal()
                try:
                    user = db.query(User).filter(User.phone_number.in_([f"+{digits}", digits])).first()
                    senior = db.query(Senior).filter(Senior.user_id == user.id).first() if user else None
                    stored_slug = _platform_slug_from_senior(db, senior)
                    already_past_confirm = thread_has_platform_issue_prompt(
                        db, thread_id, exclude_message_id=message_id
                    )
                finally:
                    db.close()
                if (
                    on_tech_path_now
                    and senior
                    and senior.device_id
                    and stored_slug
                    and not pending_issue
                    and not platform_pick_this_turn
                    and not already_past_confirm
                ):
                    db = SessionLocal()
                    try:
                        customer_name = _name_for_thread(db, thread_id)
                    finally:
                        db.close()
                    confirm_text = _dynamic_copy(
                        "platform_still_using",
                        context={
                            "platform_label": _platform_label(stored_slug),
                            "customer_name": customer_name,
                        },
                    )
                    await send_message(phone, confirm_text)
                    _update_bot_response(
                        message_id,
                        bot_response=confirm_text,
                        message_source="platform_confirmation",
                        confidence_score=None,
                    )
                    return

            # 2. First message on a new chatbot thread after an older thread existed (e.g. after resolved).
            fresh_chatbot_thread = False
            post_resolve_welcome_sent = False
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
                fresh_chatbot_thread = bool(
                    thread_row
                    and thread_row.role == "chatbot"
                    and not prior_msg_in_same_thread
                    and prior_thread_exists
                )
                if (
                    is_unresolved
                    or _assistant_in_troubleshooting_arc(
                        last_bot_message_source, last_bot_response
                    )
                ):
                    fresh_chatbot_thread = False
                if fresh_chatbot_thread:
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
                        last_on_prev = (
                            db.query(Message)
                            .filter(
                                Message.thread_id == prev_thread.id,
                                Message.is_deleted.is_(False),
                            )
                            .order_by(desc(Message.created))
                            .first()
                        )
                        if last_on_prev and "post_resolve_welcome" in (  #If previous thread’s last message had post_resolve_welcome in source → post_resolve_welcome_sent=True.
                            last_on_prev.message_source or ""
                        ):
                            post_resolve_welcome_sent = True
                if not post_resolve_welcome_sent:
                    # Poll may have sent welcome on this thread already — skip duplicate.
                    prior_bot_welcome = (
                        db.query(Message)
                        .filter(
                            Message.thread_id == thread_id,
                            Message.is_deleted.is_(False),
                            Message.id != message_id,
                            Message.bot_response.isnot(None),
                        )
                        .order_by(desc(Message.created))
                        .first()
                    )
                    if prior_bot_welcome and "post_resolve_welcome" in (
                        prior_bot_welcome.message_source or ""
                    ):
                        post_resolve_welcome_sent = True
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

            # 3. Generate response from history + tool calling
            turn_meta = {
                "button_reply_id": button_reply_id or "",
                "unresolved_signal_current_turn": is_unresolved,
                "unresolved_rounds": issue_followup_depth,
                "unresolved_phase": unresolved_phase,
                "use_platform_only_retrieval": use_platform_only_retrieval,
                "fresh_chatbot_thread": fresh_chatbot_thread,
                "post_resolve_welcome_sent": post_resolve_welcome_sent,
                "active_tech_troubleshooting_arc": _assistant_in_troubleshooting_arc(
                    last_bot_message_source, last_bot_response
                ),
                "active_branch": (intent_result.get("active_branch") or "").strip().lower(),
                "classifier_intent": (intent_result.get("intent") or "").strip().upper(),
                "os_extraction": os_extraction,
            }
            llm_result = override or await asyncio.to_thread(
                _generate_wati_reply_in_thread,
                thread_id,
                message_id,
                incoming_message,
                turn_meta,
                history_for_llm,
            )
        finally:
            await _stop_typing_indicator(typing_stop, typing_task)

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
                new_thread_id = None
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
                response_source = "conversation_control"

                welcome_text = _default_button_message("mode_buttons")
                if new_thread_id:
                    db = SessionLocal()
                    try:
                        snap = _prior_thread_snapshot(db, new_thread_id)
                        welcome_text = (
                            (snap.get("prior_welcome_blend") or "").strip()
                            or welcome_text
                        )
                    finally:
                        db.close()

                sent_buttons = await send_interactive_buttons_message(
                    phone, welcome_text, "mode_buttons"
                )
                sent = sent or sent_buttons
                bot_response = f"{ack_text}\n\n---\n\n{welcome_text}"
                response_source = "conversation_control,post_resolve_welcome,mode_buttons"
            else:
                sent = True
                bot_response = ""
        elif llm_result.get("kind") == "multi_text":
            parts = llm_result.get("messages") or []
            bot_chunks: list[str] = []
            for part in parts:
                if not isinstance(part, dict):
                    continue
                text = (part.get("message") or "").strip()
                if not text:
                    continue
                part_sent = await send_message(phone, text)
                sent = sent or part_sent
                bot_chunks.append(text)
                part_source = (part.get("message_source") or "").strip()
                if part_source and not response_source:
                    response_source = part_source
            bot_response = "\n\n---\n\n".join(bot_chunks)
        elif llm_result.get("kind") == "action":
            action = llm_result.get("action")
            if action == "scam_os_buttons" and (
                (support_mode or "").strip().lower() == "tech" or not scam_context
            ):
                logger.warning(
                    "Blocked scam_os_buttons phone=%s support_mode=%s scam_context=%s",
                    phone,
                    support_mode,
                    scam_context,
                )
                action_message = (
                    (llm_result.get("message") or "").strip()
                    or _default_button_message("platform_buttons")
                )
                bot_response = action_message
                sent = await send_interactive_platform_list_message(phone, action_message)
            else:
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