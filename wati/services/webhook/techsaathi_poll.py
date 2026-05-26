"""TechSaathi polling — detect when a human agent closes a chat and return control to the bot."""

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import desc, func

from app.config.base import SessionLocal
from app.models.chat.chat import Conversation, Message, Thread
from app.models.senior import Senior
from app.models.user import User
from wati.services.conversation import _prior_thread_snapshot
from wati.settings import settings

from .wati_webhook import (
    read_wati_timeline_state,
    send_interactive_buttons_message,
    send_message,
)

logger = logging.getLogger("wati.services.techsaathi_poll")

_WELCOME_SOURCE = "post_techsaathi_welcome,post_resolve_welcome,mode_buttons"


@dataclass(frozen=True)
class TechsaathiIncomingResult:
    """Outcome of the early techsaathi gate in ``process_incoming_message``.

    - ``continue`` — not on an active techsaathi thread; normal bot flow proceeds.
    - ``human_active`` — human still has the chat; bot must stay silent.
    - ``resolved`` — agent closed; we switched to bot and sent welcome buttons.
    """

    kind: Literal["continue", "human_active", "resolved"]
    new_thread_id: int | None = None
    welcome_text: str = ""
    welcome_source: str = ""


def resolve_techsaathi_thread_and_create_new_chatbot_thread(db, thread_id: int) -> int | None:
    """End the human handoff in our DB and open a fresh chatbot thread.

    Only acts when the thread is still ``techsaathi + assigned`` (idempotent guard).
    Returns the new chatbot thread id, or None if nothing to do.
    """
    thread = db.query(Thread).filter(Thread.id == thread_id).first()
    if not thread or thread.role != "techsaathi" or thread.status != "assigned":
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


async def send_post_techsaathi_return_welcome(
    db,
    *,
    conversation_id: int,
    new_thread_id: int,
    phone: str,
) -> bool:
    """Send the post-handoff welcome (Tech/Scam buttons) and persist it in DB."""
    snap = _prior_thread_snapshot(db, new_thread_id)
    welcome_text = (snap.get("prior_welcome_blend") or "").strip() or "How can I help you today?"
    digits = "".join(c for c in str(phone or "") if c.isdigit())

    wati_message_id = await send_interactive_buttons_message(
        digits, welcome_text, "mode_buttons"
    )
    if not wati_message_id:
        logger.warning("poll welcome send failed thread=%s phone=%s", new_thread_id, digits)
        return False

    db.add(
        Message(
            conversation_id=conversation_id,
            thread_id=new_thread_id,
            user_message="",
            bot_response=welcome_text,
            template=welcome_text[:4000],
            message_source=_WELCOME_SOURCE,
            wati_message_id=wati_message_id,
        )
    )
    db.flush()
    return True


async def handle_techsaathi_incoming_message(
    thread_id: int,
    message_id: int,
    phone: str,
) -> TechsaathiIncomingResult:
    """Webhook-path gate: run before classifier/LLM on every incoming message."""
    digits = "".join(c for c in str(phone or "") if c.isdigit())

    # --- 1) Check thread + WATI timeline; switch DB if agent closed ---
    db = SessionLocal()
    try:
        thread = db.query(Thread).filter(Thread.id == thread_id).first()
        if not thread or thread.role != "techsaathi" or thread.status != "assigned":
            return TechsaathiIncomingResult(kind="continue")

        conversation_id = thread.conversation_id

        if getattr(settings, "WATI_EXT_MESSAGES_ENABLED", True):
            ext_state = await read_wati_timeline_state(digits)
            logger.info(
                "TECHSAATHI_TIMELINE thread=%s phone=%s ext_state=%s",
                thread_id,
                digits,
                ext_state,
            )
            if ext_state != "resolved":
                return TechsaathiIncomingResult(kind="human_active")
        else:
            return TechsaathiIncomingResult(kind="human_active")

        new_thread_id = resolve_techsaathi_thread_and_create_new_chatbot_thread(db, thread_id)
        if not new_thread_id:
            return TechsaathiIncomingResult(kind="human_active")

        msg = db.query(Message).filter(Message.id == message_id).first()
        if msg:
            msg.thread_id = new_thread_id

        db.commit()
        logger.info(
            "TECHSAATHI_RESOLVED_SWITCH_TO_BOT old_thread=%s new_thread=%s phone=%s",
            thread_id,
            new_thread_id,
            digits,
        )
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    # --- 2) Send welcome on a fresh session (don't roll back thread switch if send fails) ---
    welcome_text = ""
    welcome_source = _WELCOME_SOURCE
    welcome_db = SessionLocal()
    try:
        ok = await send_post_techsaathi_return_welcome(
            welcome_db,
            conversation_id=conversation_id,
            new_thread_id=new_thread_id,
            phone=phone,
        )
        if ok:
            welcome_db.commit()
            welcome_row = (
                welcome_db.query(Message)
                .filter(
                    Message.thread_id == new_thread_id,
                    Message.is_deleted.is_(False),
                    Message.message_source.contains("post_resolve_welcome"),
                )
                .order_by(desc(Message.created))
                .first()
            )
            if welcome_row:
                welcome_text = welcome_row.bot_response or ""
                welcome_source = welcome_row.message_source or welcome_source
            logger.info("TECHSAATHI_RETURN_WELCOME_SENT thread=%s phone=%s", new_thread_id, digits)
        else:
            welcome_db.rollback()
            snap = _prior_thread_snapshot(welcome_db, new_thread_id)
            fallback_text = (snap.get("prior_welcome_blend") or "").strip() or "How can I help you today?"
            wati_message_id = await send_interactive_buttons_message(
                digits, fallback_text, "mode_buttons"
            )
            if wati_message_id:
                welcome_db.add(
                    Message(
                        conversation_id=conversation_id,
                        thread_id=new_thread_id,
                        user_message="",
                        bot_response=fallback_text,
                        template=fallback_text[:4000],
                        message_source=_WELCOME_SOURCE,
                        wati_message_id=wati_message_id,
                    )
                )
                welcome_db.commit()
                welcome_text = fallback_text
                logger.info("TECHSAATHI_RETURN_WELCOME_RETRY_OK thread=%s phone=%s", new_thread_id, digits)
            elif await send_message(digits, fallback_text):
                welcome_text = fallback_text
                logger.warning("TECHSAATHI_RETURN_WELCOME_TEXT_ONLY thread=%s phone=%s", new_thread_id, digits)
            else:
                logger.error("TECHSAATHI_RETURN_WELCOME_FAILED thread=%s phone=%s", new_thread_id, digits)
    except Exception:
        welcome_db.rollback()
        raise
    finally:
        welcome_db.close()

    return TechsaathiIncomingResult(
        kind="resolved",
        new_thread_id=new_thread_id,
        welcome_text=welcome_text,
        welcome_source=welcome_source,
    )


async def run_techsaathi_poll_loop() -> None:
    """Background task: watch active techsaathi threads and switch back to bot when WATI says resolved."""
    active_interval = getattr(settings, "WATI_TECHSAATHI_POLL_INTERVAL_SECONDS", 60)
    idle_interval = getattr(settings, "WATI_TECHSAATHI_POLL_IDLE_SECONDS", 300)

    logger.info(
        "Techsaathi poll loop started active=%ss idle=%ss",
        active_interval,
        idle_interval,
    )

    while True:
        try:
            # --- Find conversations whose latest thread is techsaathi + assigned ---
            db = SessionLocal()
            try:
                latest = (
                    db.query(
                        Thread.conversation_id.label("conversation_id"),
                        func.max(Thread.created).label("max_created"),
                    )
                    .group_by(Thread.conversation_id)
                    .subquery()
                )
                targets = [
                    (thread, "".join(c for c in str(phone or "") if c.isdigit()))
                    for thread, phone in (
                        db.query(Thread, User.phone_number)
                        .join(
                            latest,
                            (Thread.conversation_id == latest.c.conversation_id)
                            & (Thread.created == latest.c.max_created),
                        )
                        .join(Conversation, Thread.conversation_id == Conversation.id)
                        .join(Senior, Conversation.senior_id == Senior.id)
                        .join(User, Senior.user_id == User.id)
                        .filter(
                            Thread.role == "techsaathi",
                            Thread.status == "assigned",
                            Conversation.is_deleted.is_(False),
                        )
                        .all()
                    )
                    if "".join(c for c in str(phone or "") if c.isdigit())
                ]
            finally:
                db.close()

            if not targets:
                await asyncio.sleep(idle_interval)
                continue

            # --- For each target: WATI check → resolve thread → welcome → commit ---
            resolved_count = 0
            db = SessionLocal()
            try:
                for thread, phone in targets:
                    try:
                        if not getattr(settings, "WATI_EXT_MESSAGES_ENABLED", True):
                            continue

                        ext_state = await read_wati_timeline_state(phone)
                        logger.info(
                            "TECHSAATHI_TIMELINE thread=%s phone=%s ext_state=%s",
                            thread.id,
                            phone,
                            ext_state,
                        )
                        if ext_state != "resolved":
                            db.rollback()
                            continue

                        new_id = resolve_techsaathi_thread_and_create_new_chatbot_thread(
                            db, thread.id
                        )
                        if not new_id:
                            db.rollback()
                            continue

                        ok = await send_post_techsaathi_return_welcome(
                            db,
                            conversation_id=thread.conversation_id,
                            new_thread_id=new_id,
                            phone=phone,
                        )
                        if not ok:
                            db.rollback()
                            continue

                        db.commit()
                        resolved_count += 1
                        logger.info(
                            "TECHSAATHI_POLL_RESOLVED old_thread=%s new_thread=%s phone=%s",
                            thread.id,
                            new_id,
                            phone,
                        )
                    except Exception:
                        db.rollback()
                        logger.exception(
                            "TECHSAATHI_POLL_FAILED thread=%s phone=%s", thread.id, phone
                        )
            finally:
                db.close()

            if resolved_count:
                logger.info("TECHSAATHI_POLL_TICK resolved=%s", resolved_count)

            await asyncio.sleep(active_interval)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Techsaathi poll loop error")
            await asyncio.sleep(active_interval)
