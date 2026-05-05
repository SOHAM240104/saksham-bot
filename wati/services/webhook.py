import asyncio
import json
import logging
from datetime import UTC, date, datetime, timedelta
from urllib.parse import quote

import httpx
from sqlalchemy import desc

from app.config.base import SessionLocal
from app.models.chat.chat import Conversation, Message, Thread
from app.models.senior import Senior
from app.models.user import User
from wati.services.conversation import (
    _looks_like_troubleshooting_steps,
    classify_wati_turn_intent,
    conversation_control,
    generate_wati_reply,
)
from wati.settings import settings

logger = logging.getLogger("wati.services.webhook")


def wati_payload_indicates_human_operator(payload: dict | None) -> bool:
    p = payload or {}
    if (p.get("operatorName") or "").strip():
        return True
    e = (p.get("operatorEmail") or "").strip().lower()
    return bool(e) and "api-token-user" not in e


def wati_response_indicates_success(response: httpx.Response) -> bool:
    if response.status_code >= 400:
        return False
    try:
        data = response.json()
    except Exception:
        return True
    if isinstance(data, dict) and data.get("result") is False:
        return False
    if isinstance(data, dict) and data.get("ok") is False:
        return False
    return True


def _wati_v1_api_base() -> str:
    base = settings.WATI_API_ENDPOINT.rstrip("/")
    tenant = (getattr(settings, "WATI_TENANT_ID", None) or "").strip()
    if tenant and not base.rstrip("/").endswith(tenant):
        return f"{base}/{tenant}"
    return base


def _timeline_items_from_response(data: dict) -> list:
    """v1 getMessages uses messages.items; ext v3 used message_list."""
    if not isinstance(data, dict):
        return []
    msgs = data.get("messages")
    if isinstance(msgs, dict):
        items = msgs.get("items")
        if isinstance(items, list):
            return items
    raw = data.get("message_list")
    return raw if isinstance(raw, list) else []


def _item_event_type(item: dict) -> str:
    return str(item.get("eventType") or item.get("event_type") or "").lower()


def _item_event_description(item: dict) -> str:
    return str(item.get("eventDescription") or item.get("event_description") or "")


def _item_detailed_description_text(item: dict) -> str:
    """WATI embeds extra copy under detailedEventDescription (e.g. agentName)."""
    det = item.get("detailedEventDescription") or item.get("detailed_event_description")
    if not isinstance(det, dict):
        return ""
    parts: list[str] = []
    for key in ("agentName", "status", "triggerSourceName", "flowName"):
        v = det.get(key)
        if isinstance(v, str) and v.strip():
            parts.append(v.strip())
    return " ".join(parts)


def _item_combined_description(item: dict) -> str:
    return " ".join(
        p for p in (_item_event_description(item), _item_detailed_description_text(item)) if p
    ).strip()


def parse_wati_timeline_for_thread(items: list) -> str:
    if not isinstance(items, list):
        return "assigned"

    ts_human_assigned = ""
    ts_closed = ""
    ts_bot_reopened = ""

    for it in items:
        if not isinstance(it, dict):
            continue

        if _item_event_type(it) != "ticket":
            continue

        desc = _item_combined_description(it).lower()
        ts = str(it.get("created") or "")

        # human took control
        if "assigned to" in desc and "bot" not in desc:
            if ts > ts_human_assigned:
                ts_human_assigned = ts

        # human closed
        if "chat has been closed" in desc or "closed by agent" in desc:
            if ts > ts_closed:
                ts_closed = ts

        #  bot took back control
        if "ticket status" in desc and "open" in desc and "bot" in desc:
            if ts > ts_bot_reopened:
                ts_bot_reopened = ts

        if "chat has been initialized" in desc:
            if ts > ts_bot_reopened:
                ts_bot_reopened = ts

    latest_end = max(ts_closed, ts_bot_reopened)

    if latest_end and latest_end >= ts_human_assigned:
        return "resolved"

    return "assigned"


async def fetch_wati_get_messages(phone: str) -> dict | None:
    digits = "".join(c for c in str(phone or "") if c.isdigit())
    if not digits:
        return None
    url = f"{_wati_v1_api_base()}/api/v1/getMessages/{quote(digits, safe='')}"
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(
                url,
                headers={
                    "Authorization": f"Bearer {settings.WATI_AUTH_TOKEN}",
                    "Accept": "application/json",
                },
                params={"pageNumber": 1, "pageSize": 200},
            )
            logger.info("WATI getMessages status=%s url=%s", r.status_code, url[:140])
            if r.status_code >= 400:
                logger.warning("WATI getMessages body=%s", r.text[:500])
                return None
            return r.json()
    except Exception:
        logger.exception("WATI getMessages failed")
        return None


async def read_wati_timeline_state(phone: str) -> str | None:
    """resolved | assigned for techsaathi thread.status sync; None if disabled or request failed."""
    if not getattr(settings, "WATI_EXT_MESSAGES_ENABLED", True):
        return None
    data = await fetch_wati_get_messages(phone)
    if not isinstance(data, dict):
        return None
    res = data.get("result")
    if res is not None and str(res).lower() not in ("success", "true", "1"):
        if res is False:
            logger.warning("WATI getMessages unexpected result=%s", res)
            return None
    items = _timeline_items_from_response(data)
    return parse_wati_timeline_for_thread(items)


# =========================================================
# SEND MESSAGE TO WATI (NO STATIC FALLBACKS)
# =========================================================
async def send_message(phone: str, message: str) -> bool:
    if not phone:
        logger.warning("Skipping send_message: phone missing")
        return False

    text = (message or "").strip()
    if not text:
        logger.warning("Empty message, not sending")
        return False

    url = f"{settings.WATI_API_ENDPOINT}/api/v1/sendSessionMessage/{phone}"
    headers = {
        "Authorization": f"Bearer {settings.WATI_AUTH_TOKEN}",
        "Content-Type": "application/json",
    }

    for attempt in range(1, 4):
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post(
                    url,
                    headers=headers,
                    params={"messageText": text},
                )
                logger.info(
                    "WATI send response status=%s text=%s",
                    response.status_code,
                    response.text,
                )
                return wati_response_indicates_success(response)

        except httpx.TimeoutException:
            logger.warning("Timeout attempt %s for %s", attempt, phone)
            await asyncio.sleep(attempt)

        except Exception as exc:
            logger.exception("Send failed attempt %s: %s", attempt, exc)
            await asyncio.sleep(attempt)

    return False


async def send_interactive_buttons_message(phone: str, message: str, action: str) -> bool:
    if not phone:
        logger.warning("Skipping interactive message: phone missing")
        return False

    text = (message or "").strip()
    if not text:
        logger.warning("Skipping interactive message: empty body")
        return False

    if action == "mode_buttons":
        buttons = [
            {"id": "tech", "title": "Tech Help"},
            {"id": "scam", "title": "Scam Help"},
        ]
    elif action == "feedback_buttons":
        buttons = [
            {"id": "resolved", "title": "Resolved "},
            {"id": "not_resolved", "title": "Still Stuck "},
        ]
    else:
        logger.warning("Unknown interactive action=%s", action)
        return False

    url = f"{settings.WATI_API_ENDPOINT}/api/v1/sendInteractiveButtonsMessage"
    headers = {
        "Authorization": f"Bearer {settings.WATI_AUTH_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "body": text,
        "footer": "",
        "buttons": [
            {
                "text": button["title"],
            }
            for button in buttons
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                url,
                headers=headers,
                params={"whatsappNumber": phone},
                json=payload,
            )
            logger.info(
                "WATI interactive response status=%s text=%s",
                response.status_code,
                response.text,
            )
            return wati_response_indicates_success(response)
    except Exception:
        logger.exception("Failed sending interactive buttons action=%s", action)
        return False


async def send_interactive_platform_list_message(phone: str, message: str) -> bool:
    """WhatsApp list message for supported phone brands (up to 10 rows in one message)."""
    if not phone:
        logger.warning("Skipping platform list message: phone missing")
        return False

    text = (message or "").strip()
    if not text:
        logger.warning("Skipping platform list message: empty body")
        return False

    url = f"{settings.WATI_API_ENDPOINT}/api/v1/sendInteractiveListMessage"
    headers = {
        "Authorization": f"Bearer {settings.WATI_AUTH_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "header": "Phone brand",
        "body": text,
        "footer": "",
        "buttonText": "Choose phone",
        "sections": [
            {
                "title": "Supported phones",
                "rows": [
                    {"title": "Apple", "description": "iPhone / iPad"},
                    {"title": "Samsung", "description": "Galaxy"},
                    {"title": "Pixel", "description": "Google Pixel"},
                    {"title": "Oppo", "description": ""},
                    {"title": "Xiaomi", "description": "Redmi / POCO"},
                ],
            }
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                url,
                headers=headers,
                params={"whatsappNumber": phone},
                json=payload,
            )
            logger.info(
                "WATI interactive list response status=%s text=%s",
                response.status_code,
                response.text,
            )
            return wati_response_indicates_success(response)
    except Exception:
        logger.exception("Failed sending interactive platform list message")
        return False


async def send_template_message(phone: str, parameters: list[str] | None = None) -> bool:
    if not phone:
        return False

    template_name = getattr(settings, "WATI_TEMPLATE_NAME", "tech_saathi_welcome")
    payload = {
        "template_name": template_name,
        "broadcast_name": "session_start",
        "parameters": [
            {"name": f"{{{{{idx + 1}}}}}", "value": value}
            for idx, value in enumerate(parameters or [])
        ],
    }
    channel_number = getattr(settings, "WATI_CHANNEL_NUMBER", None)
    if channel_number:
        payload["channel_number"] = channel_number

    url = f"{settings.WATI_API_ENDPOINT}/api/v1/sendTemplateMessage"
    headers = {
        "Authorization": f"Bearer {settings.WATI_AUTH_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                url,
                headers=headers,
                params={"whatsappNumber": phone},
                json=payload,
            )
            logger.info(
                "WATI template response status=%s text=%s",
                response.status_code,
                response.text,
            )
            return wati_response_indicates_success(response)
    except Exception:
        logger.exception("Failed sending template message")
        return False



def _extract_name(payload: dict) -> str | None:
    for key in ["senderName", "contactName", "name", "profileName"]:
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()[:512]
    return None


def _get_or_create_user(db, phone: str, first_name: str | None) -> User:
    user = db.query(User).filter(User.phone_number == phone).first()
    if user:
        return user

    user = User(
        phone_number=phone,
        first_name=first_name,
        user_type="senior",
        is_superuser=False,
        is_staff=False,
    
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
        gender="male",
        onboarding_type="self",
        zoom_call=False,
        whatsapp_call=False,
        whatsapp_msg=True,
        updates=False,
    )
    db.add(senior)
    db.flush()
    return senior


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

        logger.info(
            "Saved message id=%s phone=%s conv=%s wati_conv=%s",
            message_id,
            phone,
            conversation.id,
            wati_conversation_id,
        )

        return message_id, thread.id, created_new_thread

    except Exception:
        db.rollback()
        logger.exception("DB persist failed")
        raise

    finally:
        db.close()


# =========================================================
# UPDATE BOT RESPONSE
# =========================================================
def _update_bot_response(
    message_id: int,
    bot_response: str | None = None,
    template: str | None = None,
    message_source: str | None = None,
    confidence_score: float | None = None,
):
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


def resolve_thread_and_create_new_chatbot_thread(db, thread_id: int) -> int | None:
    """Resolve chatbot thread and start a fresh chatbot thread. Returns new thread id."""
    thread = db.query(Thread).filter(Thread.id == thread_id).first()
    if not thread:
        return None
    if thread.role != "chatbot":
        return None
    thread.status = "resolved"
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


def _extract_button_reply_id(payload: dict | None) -> str | None:
    raw = payload or {}

    list_reply = raw.get("listReply")
    if isinstance(list_reply, dict):
        title = list_reply.get("title")
        if isinstance(title, str) and title.strip():
            normalized_title = title.strip().lower()
            if normalized_title in {"apple", "samsung", "pixel", "oppo", "xiaomi"}:
                return normalized_title
        reply_id = list_reply.get("id")
        if isinstance(reply_id, str) and reply_id.strip():
            normalized = reply_id.strip().lower()
            if normalized in {"apple", "samsung", "pixel", "oppo", "xiaomi"}:
                return normalized

    interactive_button_reply = raw.get("interactiveButtonReply")
    if isinstance(interactive_button_reply, dict):
        title = interactive_button_reply.get("title")
        if isinstance(title, str) and title.strip():
            normalized_title = title.strip().lower()
            if "tech help" in normalized_title:
                return "tech"
            if "scam help" in normalized_title:
                return "scam"
            if normalized_title in {"apple", "samsung", "pixel", "oppo", "xiaomi"}:
                return normalized_title
            if "resolved" in normalized_title and "stuck" not in normalized_title:
                return "resolved"
            if "stuck" in normalized_title or "not resolved" in normalized_title:
                return "not_resolved"
        reply_id = interactive_button_reply.get("id")
        if isinstance(reply_id, str) and reply_id.strip():
            normalized = reply_id.strip().lower()
            if normalized in {"resolved", "not_resolved", "tech", "scam"}:
                return normalized

    button_reply = raw.get("buttonReply")
    if isinstance(button_reply, dict):
        title = button_reply.get("title")
        if isinstance(title, str) and title.strip():
            normalized_title = title.strip().lower()
            if "tech help" in normalized_title:
                return "tech"
            if "scam help" in normalized_title:
                return "scam"
            if normalized_title in {"apple", "samsung", "pixel", "oppo", "xiaomi"}:
                return normalized_title
            if "resolved" in normalized_title and "stuck" not in normalized_title:
                return "resolved"
            if "stuck" in normalized_title or "not resolved" in normalized_title:
                return "not_resolved"
        reply_id = button_reply.get("id")
        if isinstance(reply_id, str) and reply_id.strip():
            return reply_id.strip().lower()

    interactive_data = raw.get("interactiveData")
    if isinstance(interactive_data, dict):
        button_id = interactive_data.get("buttonId")
        if isinstance(button_id, str) and button_id.strip():
            return button_id.strip().lower()

    return None


def _default_button_message(action: str) -> str:
    if action == "mode_buttons":
        return "How can I help you today?"
    if action == "platform_buttons":
        return "Which phone are you using?"
    if action == "feedback_buttons":
        return "Did this solve the issue?"
    return "Please choose an option."


def _post_steps_unresolved_count(
    db, thread_id: int, current_message_id: int, current_turn_unresolved: bool
) -> tuple[bool, int]:
    msgs = (
        db.query(Message)
        .filter(
            Message.thread_id == thread_id,
            Message.is_deleted.is_(False),
            Message.id != current_message_id,
        )
        .order_by(Message.created.asc())
        .all()
    )
    post_steps_started = False
    unresolved_count = 0
    for msg in msgs:
        bot_text = (msg.bot_response or "").strip()
        lowered = bot_text.lower()
        if _looks_like_troubleshooting_steps(bot_text) or "did this help" in lowered or "did this solve" in lowered:
            post_steps_started = True
        if not post_steps_started:
            continue
        user_text = (msg.user_message or "").strip().lower()
        if user_text in {"still stuck", "still stuck ", "not resolved", "not_resolved"}:
            unresolved_count += 1

    if post_steps_started and current_turn_unresolved:
        unresolved_count += 1
    return post_steps_started, unresolved_count


def append_human_operator_text_to_latest_message(
    phone: str, text: str, payload: dict
) -> None:
    body = (text or "").strip()
    if not body or not phone:
        return
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.phone_number == phone).first()
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
        button_reply_id = _extract_button_reply_id(payload)

        override = None

        # 1. Save incoming message
        message_id, thread_id, created_new_thread = _persist_incoming(phone, incoming_message, payload)

        intent_result = {}
        classified_intent = ""
        classifier_next_action = ""
        classifier_confidence = None
        post_steps_stage = False
        unresolved_rounds = 0

        should_run_classifier = True
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
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

            classified_intent = (intent_result.get("intent") or "").strip().upper()
            classifier_next_action = (intent_result.get("next_action") or "").strip().upper()
            classifier_confidence = intent_result.get("confidence")
            if not isinstance(classifier_confidence, (int, float)):
                classifier_confidence = None
            logger.info(
                "LLM_INTENT intent=%s next_action=%s confidence=%s phone=%s",
                classified_intent,
                classifier_next_action,
                classifier_confidence,
                phone,
            )
        unresolved_signal_current_turn = (
            button_reply_id == "not_resolved" or classified_intent == "NOT_RESOLVED"
        )

        db = SessionLocal()
        try:
            post_steps_stage, unresolved_rounds = _post_steps_unresolved_count(
                db,
                thread_id=thread_id,
                current_message_id=message_id,
                current_turn_unresolved=unresolved_signal_current_turn,
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

        if classified_intent == "RESOLVED":
            control_result = conversation_control.invoke({"action": "resolved"})
            override = {
                "kind": "control",
                "action": (control_result.get("action") or "").strip(),
                "message_source": "intent_classifier",
                "confidence_score": classifier_confidence,
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
            _update_bot_response(
                message_id,
                bot_response="",
                message_source="human_resolution",
            )
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
                    classified_intent == "REQUEST_HUMAN"
                    and classifier_next_action == "ESCALATE_TO_HUMAN"
                )
                if classifier_requested_handoff and not switched_from_human_to_chatbot_this_turn:
                    hid = resolve_assigned_chatbot_thread_and_create_techsaathi_thread(
                        db, thread_id
                    )
                    if hid:
                        _reassign_message_thread(db, message_id, hid)
                        thread_id = hid
                    handoff_needed = True
                    handoff_count = 1
                elif post_steps_stage and unresolved_rounds >= 2:
                    hid = resolve_assigned_chatbot_thread_and_create_techsaathi_thread(
                        db, thread_id
                    )
                    if hid:
                        _reassign_message_thread(db, message_id, hid)
                        thread_id = hid
                    handoff_needed = True
                    handoff_count = unresolved_rounds
                    logger.info(
                        "handoff_by_unresolved_threshold thread_id=%s phone=%s unresolved_rounds=%s",
                        thread_id,
                        phone,
                        unresolved_rounds,
                    )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

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
            channel_number = (getattr(settings, "WATI_CHANNEL_NUMBER", None) or "").strip()

            tenant = (getattr(settings, "WATI_TENANT_ID", None) or "").strip()
            assign_team_url = f"{base}/api/v1/assignTeam"
            if tenant and not base.rstrip("/").endswith(tenant):
                assign_team_url = f"{base}/{tenant}/api/v1/assignTeam"
            assign_operator_url = f"{base}/api/v1/assignOperator"
            if tenant and not base.rstrip("/").endswith(tenant):
                assign_operator_url = f"{base}/{tenant}/api/v1/assignOperator"

            if teams:
                try:
                    async with httpx.AsyncClient(timeout=20.0) as client:
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
                except Exception:
                    logger.exception("WATI assignTeam request failed")

            if assignee_email and phone:
                try:
                    async with httpx.AsyncClient(timeout=20.0) as client:
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
                except Exception:
                    logger.exception("WATI assignOperator request failed")

            if channel_number and phone:
                try:
                    async with httpx.AsyncClient(timeout=20.0) as client:
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
                    logger.exception("WATI updateChatStatus request failed")

            if teams or assignee_email:
                wait_text = (
                    "I'm connecting you to a human expert. "
                    "Your ticket is open and the chat is being assigned to support—please wait for an agent."
                )
            else:
                wait_text = "I'm connecting you to a human expert. Please wait."
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

        if classified_intent == "REQUEST_HUMAN" and classifier_next_action != "ESCALATE_TO_HUMAN":
            confirm_text = (
                "Sure — do you want me to connect you to a human agent now? "
                "Please reply with Yes or No."
            )
            await send_message(phone, confirm_text)
            _update_bot_response(
                message_id,
                bot_response=confirm_text,
                message_source="handoff_confirmation",
                confidence_score=classifier_confidence,
            )
            return

        # 2. Session start template message
        if created_new_thread:
            template_sent = await send_template_message(phone)
            if template_sent:
                _update_bot_response(
                    message_id,
                    template=getattr(settings, "WATI_TEMPLATE_NAME", "tech_saathi_welcome"),
                    message_source="wati_template",
                )

        # 3. Generate response from history + tool calling
        db = SessionLocal()
        try:
            llm_result = override or await generate_wati_reply(
                db, thread_id, message_id, incoming_message
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
                    resolve_thread_and_create_new_chatbot_thread(db, thread_id)
                    db.commit()
                except Exception:
                    db.rollback()
                    raise
                finally:
                    db.close()

                sent_ack = await send_message(phone, "Glad it's working now 😊")
                sent_mode = await send_interactive_buttons_message(
                    phone,
                    "Anything else I can help you with?",
                    "mode_buttons",
                )
                sent = sent_ack and sent_mode
                bot_response = "Glad it's working now 😊\nAnything else I can help you with?"
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

    except Exception:
        logger.exception("Processing failed for phone=%s", phone)