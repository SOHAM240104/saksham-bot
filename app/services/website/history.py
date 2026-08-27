"""Senior chat history — load conversation rows for the website UI."""

from __future__ import annotations

from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.models.chat.chat import Conversation, Message
from app.models.senior import Senior
from app.schema.messages import MessageResponse


def _latest_conversation(db: Session, senior: Senior) -> Conversation | None:
    return (
        db.query(Conversation)
        .filter(
            Conversation.senior_id == senior.id,
            Conversation.is_deleted.is_(False),
        )
        .order_by(desc(Conversation.created))
        .first()
    )


def list_senior_messages(
    db: Session,
    senior: Senior,
    *,
    since_id: int | None = None,
    limit: int = 200,
) -> list[MessageResponse]:
    """Return message rows for the senior's active conversation, oldest first."""
    conversation = _latest_conversation(db, senior)
    if not conversation:
        return []

    query = db.query(Message).filter(
        Message.conversation_id == conversation.id,
        Message.is_deleted.is_(False),
    )
    if since_id is not None and since_id > 0:
        query = query.filter(Message.id > since_id)

    rows = (
        query.order_by(Message.created.asc(), Message.id.asc())
        .limit(max(1, min(limit, 500)))
        .all()
    )
    return [MessageResponse.from_message(row) for row in rows]


def agent_replies_since_prior_senior_turn(
    db: Session,
    *,
    thread_id: int,
    current_message_id: int,
) -> list[MessageResponse]:
    """TechSaathi replies on the human thread since the previous senior message."""
    prior_senior = (
        db.query(Message)
        .filter(
            Message.thread_id == thread_id,
            Message.id < current_message_id,
            Message.user_message.isnot(None),
            Message.user_message != "",
        )
        .order_by(desc(Message.id))
        .first()
    )
    after_id = prior_senior.id if prior_senior else 0
    rows = (
        db.query(Message)
        .filter(
            Message.thread_id == thread_id,
            Message.id > after_id,
            Message.id < current_message_id,
            Message.tech_saathi_response.isnot(None),
            Message.tech_saathi_response != "",
        )
        .order_by(Message.id.asc())
        .all()
    )
    return [MessageResponse.from_message(row) for row in rows]
