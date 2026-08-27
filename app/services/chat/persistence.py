"""Persist bot and TechSaathi replies on conversation message rows."""

import logging
import uuid

from sqlalchemy.orm import Session

from app.config.base import SessionLocal
from app.models.chat.chat import Message

logger = logging.getLogger("app.services.chat.persistence")


def _website_wati_message_id() -> str:
    return f"website-{uuid.uuid4()}"


def save_bot_reply(
    message_id: int,
    *,
    bot_response: str | None = None,
    message_source: str | None = None,
    confidence_score: float | None = None,
    patch_message_source_only: bool = False,
    template: str | None = None,
) -> None:
    """Update the inbound message row with the bot's reply metadata."""
    if not any(
        v is not None
        for v in (bot_response, message_source, confidence_score, template)
    ) and not patch_message_source_only:
        return

    db = SessionLocal()
    try:
        msg = db.query(Message).filter(Message.id == message_id).first()
        if not msg:
            logger.warning("Message not found id=%s", message_id)
            return

        if patch_message_source_only and message_source:
            prev = (msg.message_source or "").strip()
            new_source = message_source
            if prev.startswith("scam_os:") and not new_source.startswith("scam_os:"):
                new_source = f"{prev},{new_source}"
            msg.message_source = new_source
        else:
            if bot_response is not None:
                msg.bot_response = bot_response
            if message_source is not None:
                prev = (msg.message_source or "").strip()
                new_source = message_source
                if prev.startswith("scam_os:") and not new_source.startswith("scam_os:"):
                    new_source = f"{prev},{new_source}"
                msg.message_source = new_source
            if confidence_score is not None:
                msg.confidence_score = confidence_score
            if template is not None:
                msg.template = template
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Failed saving bot reply message_id=%s", message_id)
    finally:
        db.close()


def save_techsaathi_reply(
    db: Session,
    *,
    conversation_id: int,
    thread_id: int,
    tech_saathi_id: int,
    text: str,
) -> Message:
    """Insert a new message row for a TechSaathi agent reply."""
    body = (text or "").strip()
    msg = Message(
        conversation_id=conversation_id,
        thread_id=thread_id,
        user_message="",
        tech_saathi_response=body,
        tech_saathi_id=tech_saathi_id,
        message_source="techsaathi_api",
        wati_message_id=_website_wati_message_id(),
    )
    db.add(msg)
    db.flush()
    return msg


POST_HANDOFF_WELCOME_SOURCE = "post_techsaathi_welcome,post_resolve_welcome,mode_buttons"


def persist_post_handoff_welcome(
    db: Session,
    *,
    conversation_id: int,
    new_thread_id: int,
    wati_message_id: str | None = None,
    welcome_text: str | None = None,
) -> Message:
    """Persist the post-human-handoff welcome on the new chatbot thread."""
    from app.services.chat.reply import _prior_thread_snapshot

    text = (welcome_text or "").strip()
    if not text:
        snap = _prior_thread_snapshot(db, new_thread_id)
        text = (snap.get("prior_welcome_blend") or "").strip() or "How can I help you today?"
    msg = Message(
        conversation_id=conversation_id,
        thread_id=new_thread_id,
        user_message="",
        bot_response=text,
        template=text[:4000],
        message_source=POST_HANDOFF_WELCOME_SOURCE,
        wati_message_id=wati_message_id or _website_wati_message_id(),
    )
    db.add(msg)
    db.flush()
    return msg
