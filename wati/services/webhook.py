import asyncio
import logging
from datetime import UTC, date, datetime, timedelta

import httpx
from sqlalchemy import desc

from app.config.base import SessionLocal
from app.models.chat.chat import Conversation, Message, Thread
from app.models.senior import Senior
from app.models.user import User
from wati.services.conversation import generate_wati_reply
from wati.settings import settings

logger = logging.getLogger("wati.services.webhook")


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
                return response.status_code < 500

        except httpx.TimeoutException:
            logger.warning("Timeout attempt %s for %s", attempt, phone)
            await asyncio.sleep(attempt)

        except Exception as exc:
            logger.exception("Send failed attempt %s: %s", attempt, exc)
            await asyncio.sleep(attempt)

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
        date_joined=datetime.now(UTC),
        is_active=True,
        is_deleted=False,
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

    if (
        not last_thread
        or not last_message
        or last_message.created < now - timedelta(minutes=2)
    ):
        thread = Thread(
            conversation_id=conversation.id,
            role="chatbot",
            status="assigned",
        )
        db.add(thread)
        db.flush()
        return thread

    return last_thread


# =========================================================
# PERSIST INCOMING MESSAGE
# =========================================================
def _persist_incoming(phone: str, message: str, payload: dict) -> int:
    db = SessionLocal()
    now = datetime.now(UTC)

    try:
        first_name = _extract_name(payload)

        
        wati_conversation_id = str(payload.get("conversationId") or "").strip()
        wati_message_id = str(
            payload.get("whatsappMessageId") or payload.get("id") or ""
        ).strip()

        template_value = str(payload.get("type") or "text").strip() or "text"

        user = _get_or_create_user(db, phone, first_name)
        senior = _get_or_create_senior(db, user, first_name)

        conversation = _get_or_create_conversation(
            db, senior, wati_conversation_id
        )

        thread = _get_or_create_thread(db, conversation, now)

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

        return message_id

    except Exception:
        db.rollback()
        logger.exception("DB persist failed")
        raise

    finally:
        db.close()


# =========================================================
# UPDATE BOT RESPONSE
# =========================================================
def _update_bot_response(message_id: int, bot_response: str):
    db = SessionLocal()
    try:
        msg = db.query(Message).filter(Message.id == message_id).first()

        if msg:
            msg.bot_response = bot_response
            db.commit()
        else:
            logger.warning("Message not found id=%s", message_id)

    except Exception:
        db.rollback()
        logger.exception("Failed updating bot_response")

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
        # 1. Save incoming message
        message_id = _persist_incoming(phone, message, payload or {})

        # 2. Generate response (LLM handles everything)
        response = await generate_wati_reply(phone, message)

        # 3. Save bot response
        _update_bot_response(message_id, response)

        # 4. Send response
        sent = await send_message(phone, response)

        if not sent:
            logger.error("Failed sending message to %s", phone)

    except Exception:
        logger.exception("Processing failed for phone=%s", phone)