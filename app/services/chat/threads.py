"""Conversation thread lifecycle: resolve, escalate to human, reassign messages."""

from sqlalchemy import desc

from app.models.chat.chat import Message, Thread


def is_human_handoff_active(db, thread_id: int) -> bool:
    """True when this thread is an assigned TechSaathi (human) handoff."""
    thread = db.query(Thread).filter(Thread.id == thread_id).first()
    return bool(thread and thread.role == "techsaathi" and thread.status == "assigned")


def get_active_human_thread(db, conversation_id: int) -> Thread | None:
    """Return the current human-handoff thread for a conversation, if any."""
    return (
        db.query(Thread)
        .filter(
            Thread.conversation_id == conversation_id,
            Thread.role == "techsaathi",
            Thread.status == "assigned",
        )
        .order_by(desc(Thread.created))
        .first()
    )


def assign_techsaathi_to_thread(
    db,
    thread_id: int,
    tech_saathi_id: int,
) -> Thread | None:
    """Claim an unassigned human thread for a TechSaathi (idempotent if already theirs)."""
    thread = db.query(Thread).filter(Thread.id == thread_id).first()
    if not thread or thread.role != "techsaathi" or thread.status != "assigned":
        return None
    if thread.tech_saathi_id is not None and thread.tech_saathi_id != tech_saathi_id:
        return None
    thread.tech_saathi_id = tech_saathi_id
    db.flush()
    return thread


def resolve_human_thread_and_start_chatbot(db, thread_id: int) -> int | None:
    """End a human handoff thread and open a fresh chatbot thread.

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


def reassign_message_to_thread(db, message_id: int, new_thread_id: int) -> None:
    msg = db.query(Message).filter(Message.id == message_id).first()
    if msg:
        msg.thread_id = new_thread_id


def resolve_chatbot_thread_and_start_new(db, thread_id: int) -> int | None:
    """Mark chatbot thread resolved and open a fresh chatbot thread."""
    thread = db.query(Thread).filter(Thread.id == thread_id).first()
    if not thread or thread.role != "chatbot":
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


def escalate_chatbot_thread_to_human(db, thread_id: int) -> int | None:
    """Close assigned chatbot thread and open a techsaathi (human queue) thread."""
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
