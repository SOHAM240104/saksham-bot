"""TechSaathi Team Inbox — queue, history, reply, resolve."""

from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException, status
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.models.chat.chat import Conversation, Message, Thread
from app.models.senior import Senior
from app.schema.agent import (
    AgentConversationResponse,
    AgentResolveResponse,
)
from app.schema.messages import MessageResponse
from app.services.chat.persistence import persist_post_handoff_welcome, save_techsaathi_reply
from app.services.chat.threads import (
    assign_techsaathi_to_thread,
    get_active_human_thread,
    resolve_human_thread_and_start_chatbot,
)


def _conversation_state(thread: Thread | None) -> str:
    if thread and thread.role == "techsaathi" and thread.status == "assigned":
        return "human_active"
    if thread and thread.status == "resolved":
        return "resolved"
    return "bot_active"


def _conversation_summary(
    conversation: Conversation,
    thread: Thread | None,
    *,
    preview_text: str = "",
    last_activity: datetime | None = None,
    senior_name: str | None = None,
) -> AgentConversationResponse:
    activity = last_activity or (thread.created if thread else conversation.created)
    return AgentConversationResponse(
        conversation_id=conversation.id,
        senior_id=conversation.senior_id,
        senior_name=senior_name,
        state=_conversation_state(thread),
        thread_id=thread.id if thread else None,
        assigned_techsaathi_id=thread.tech_saathi_id if thread else None,
        preview=preview_text,
        last_activity=activity,
    )


def enrich_conversation_summary(db: Session, summary: AgentConversationResponse) -> AgentConversationResponse:
    if not summary.senior_id:
        return summary
    senior = db.query(Senior).filter(Senior.id == summary.senior_id).first()
    if senior and getattr(senior, "user", None):
        return summary.model_copy(update={"senior_name": senior.user.first_name or ""})
    return summary


def list_inbox_conversations(db: Session, tech_saathi_id: int) -> list[AgentConversationResponse]:
    """Unassigned human queue plus conversations assigned to this agent."""
    rows = (
        db.query(Thread, Conversation)
        .join(Conversation, Thread.conversation_id == Conversation.id)
        .filter(
            Thread.role == "techsaathi",
            Thread.status == "assigned",
            Conversation.is_deleted.is_(False),
            (Thread.tech_saathi_id.is_(None)) | (Thread.tech_saathi_id == tech_saathi_id),
        )
        .order_by(desc(Thread.modified), desc(Thread.created))
        .all()
    )

    summaries: list[AgentConversationResponse] = []
    for thread, conversation in rows:
        last_msg = (
            db.query(Message)
            .filter(
                Message.conversation_id == conversation.id,
                Message.is_deleted.is_(False),
            )
            .order_by(desc(Message.created))
            .first()
        )
        preview = ""
        if last_msg:
            preview = (
                (last_msg.user_message or "").strip()
                or (last_msg.bot_response or "").strip()
                or (last_msg.tech_saathi_response or "").strip()
            )
        summary = _conversation_summary(
            conversation,
            thread,
            preview_text=preview[:200],
            last_activity=last_msg.created if last_msg else thread.created,
        )
        summaries.append(enrich_conversation_summary(db, summary))
    return summaries


def get_conversation_for_agent(
    db: Session,
    conversation_id: int,
    tech_saathi_id: int,
) -> AgentConversationResponse:
    conversation = (
        db.query(Conversation)
        .filter(
            Conversation.id == conversation_id,
            Conversation.is_deleted.is_(False),
        )
        .first()
    )
    if not conversation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")

    thread = get_active_human_thread(db, conversation_id)
    if not thread:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active human conversation",
        )

    if thread.tech_saathi_id is not None and thread.tech_saathi_id != tech_saathi_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not assigned to you")

    summary = _conversation_summary(conversation, thread)
    return enrich_conversation_summary(db, summary)


def get_conversation_messages(
    db: Session,
    conversation_id: int,
    tech_saathi_id: int,
) -> list[MessageResponse]:
    get_conversation_for_agent(db, conversation_id, tech_saathi_id)

    messages = (
        db.query(Message)
        .filter(
            Message.conversation_id == conversation_id,
            Message.is_deleted.is_(False),
        )
        .order_by(Message.created.asc(), Message.id.asc())
        .all()
    )

    return [MessageResponse.from_message(msg) for msg in messages]


def send_agent_reply(
    db: Session,
    conversation_id: int,
    tech_saathi_id: int,
    text: str,
) -> MessageResponse:
    thread = get_active_human_thread(db, conversation_id)
    if not thread:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active human conversation",
        )
    if thread.tech_saathi_id is not None and thread.tech_saathi_id != tech_saathi_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not assigned to you")

    if thread.tech_saathi_id is None:
        thread = assign_techsaathi_to_thread(db, thread.id, tech_saathi_id)
        if not thread:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Could not claim conversation")

    msg = save_techsaathi_reply(
        db,
        conversation_id=conversation_id,
        thread_id=thread.id,
        tech_saathi_id=tech_saathi_id,
        text=text,
    )
    return MessageResponse.from_message(msg)


def resolve_conversation_for_agent(
    db: Session,
    conversation_id: int,
    tech_saathi_id: int,
) -> AgentResolveResponse:
    thread = get_active_human_thread(db, conversation_id)
    if not thread:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active human conversation",
        )
    if thread.tech_saathi_id is not None and thread.tech_saathi_id != tech_saathi_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not assigned to you")
    if thread.tech_saathi_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Assign the conversation before resolving",
        )

    resolved_thread_id = thread.id
    assigned_id = thread.tech_saathi_id
    new_thread_id = resolve_human_thread_and_start_chatbot(db, thread.id)
    if not new_thread_id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Could not resolve conversation")

    conversation = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    # Always leave a welcome on the new chatbot thread so website seniors see bot return.
    persist_post_handoff_welcome(
        db,
        conversation_id=conversation_id,
        new_thread_id=new_thread_id,
    )
    return AgentResolveResponse(
        conversation_id=conversation_id,
        state="resolved",
        resolved_thread_id=resolved_thread_id,
        new_chatbot_thread_id=new_thread_id,
        assigned_techsaathi_id=assigned_id,
        senior_id=conversation.senior_id if conversation else None,
    )
