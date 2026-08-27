"""Persist an inbound website senior message into conversation tables."""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.models.chat.chat import Conversation, Message, Thread
from app.models.senior import Senior

logger = logging.getLogger("app.services.website.inbound")


@dataclass(frozen=True)
class PersistedWebsiteInbound:
    message_id: int
    thread_id: int
    conversation_id: int


def _get_or_create_conversation(db: Session, senior: Senior) -> Conversation:
    latest = (
        db.query(Conversation)
        .filter(
            Conversation.senior_id == senior.id,
            Conversation.is_deleted.is_(False),
        )
        .order_by(desc(Conversation.created))
        .first()
    )
    if latest:
        return latest

    conversation = Conversation(senior_id=senior.id)
    db.add(conversation)
    db.flush()
    return conversation


def _get_or_create_thread(
    db: Session,
    conversation: Conversation,
    now: datetime,
) -> Thread:
    active_human = (
        db.query(Thread)
        .filter(
            Thread.conversation_id == conversation.id,
            Thread.role == "techsaathi",
            Thread.status == "assigned",
        )
        .order_by(desc(Thread.created))
        .first()
    )
    if active_human:
        return active_human

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
    idle_cutoff = now - timedelta(hours=1.0)
    if not last_thread or not last_message or last_message.created < idle_cutoff:
        thread = Thread(conversation_id=conversation.id, role="chatbot", status="assigned")
        db.add(thread)
        db.flush()
        return thread
    return last_thread


def persist_website_inbound(
    db: Session,
    senior: Senior,
    message: str,
) -> PersistedWebsiteInbound:
    """Create conversation/thread/message rows for one website chat turn."""
    now = datetime.now(UTC)
    conversation = _get_or_create_conversation(db, senior)
    thread = _get_or_create_thread(db, conversation, now)
    text = (message or "").strip()

    msg = Message(
        conversation_id=conversation.id,
        thread_id=thread.id,
        user_message=text,
        template=text[:4000] if text else "text",
        message_source="website",
        wati_message_id=f"website-{uuid.uuid4()}",
    )
    db.add(msg)
    db.flush()

    logger.info(
        "Saved website inbound message_id=%s senior_id=%s conv=%s",
        msg.id,
        senior.id,
        conversation.id,
    )
    return PersistedWebsiteInbound(
        message_id=msg.id,
        thread_id=thread.id,
        conversation_id=conversation.id,
    )
