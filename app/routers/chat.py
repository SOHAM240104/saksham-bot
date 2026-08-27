"""Website senior chat — one HTTP POST per turn, SSE response."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.auth import AuthenticatedSenior, require_senior_user
from app.config.base import SessionLocal, get_db
from app.schema.messages import MessageListResponse, MessageRequest
from app.services.chat.persistence import save_bot_reply
from app.services.chat.outbound import should_save_bot_reply
from app.services.chat.reply import is_unresolved_negative_only
from app.services.chat.threads import is_human_handoff_active
from app.services.chat.turn import TurnContext, process_inbound_turn
from app.services.website.handoff import agent_reply_sse_events
from app.services.website.history import agent_replies_since_prior_senior_turn, list_senior_messages
from app.services.website.inbound import persist_website_inbound
from app.services.website.sse import format_sse_event, iter_sse_stream, outcome_to_sse_events

logger = logging.getLogger("app.routers.chat")

router = APIRouter(prefix="/chat", tags=["chat"])


def _button_id_from_request(message: str, selection_id: str | None) -> str:
    button_id = (selection_id or "").strip()
    if not button_id and is_unresolved_negative_only(message, ""):
        return "not_resolved"
    return button_id


@router.get("/messages", response_model=MessageListResponse)
def get_chat_messages(
    auth: AuthenticatedSenior = Depends(require_senior_user),
    db: Session = Depends(get_db),
    since_id: int | None = Query(default=None, ge=0),
    limit: int = Query(default=200, ge=1, le=500),
) -> MessageListResponse:
    """Conversation history for the logged-in senior (poll during human handoff)."""
    items = list_senior_messages(db, auth.senior, since_id=since_id, limit=limit)
    return MessageListResponse(data=items)


async def _stream_chat_turn(
    auth: AuthenticatedSenior,
    body: MessageRequest,
) -> StreamingResponse:
    async def event_generator():
        db = SessionLocal()
        try:
            inbound = persist_website_inbound(db, auth.senior, body.message)
            db.commit()

            if is_human_handoff_active(db, inbound.thread_id):
                save_bot_reply(
                    inbound.message_id,
                    bot_response="",
                    message_source="human_handoff",
                )
                agent_replies = agent_replies_since_prior_senior_turn(
                    db,
                    thread_id=inbound.thread_id,
                    current_message_id=inbound.message_id,
                )
                for event_name, payload in agent_reply_sse_events(agent_replies):
                    yield format_sse_event(event_name, payload)
                yield format_sse_event("status", {"status": "completed"})
                return

            ctx = TurnContext(
                message_id=inbound.message_id,
                thread_id=inbound.thread_id,
                conversation_id=inbound.conversation_id,
                user_message=body.message,
                button_id=_button_id_from_request(body.message, body.selection_id),
            )
            # Early wait signal so long RAG/LLM turns don't feel abandoned
            yield format_sse_event(
                "status",
                {
                    "status": "thinking",
                    "message": "Looking that up…",
                },
            )
            outcome: dict = await process_inbound_turn(ctx)

            if should_save_bot_reply(outcome):
                save_bot_reply(
                    inbound.message_id,
                    bot_response=outcome.get("bot_response"),
                    message_source=outcome.get("message_source"),
                    confidence_score=outcome.get("confidence_score"),
                    patch_message_source_only=bool(outcome.get("patch_message_source_only")),
                )

            for chunk in iter_sse_stream(iter(outcome_to_sse_events(outcome))):
                yield chunk
        except Exception:
            logger.exception(
                "Website chat turn failed senior_id=%s",
                auth.senior.id,
            )
            yield format_sse_event(
                "error",
                {"message": "We could not process your message. Please try again."},
            )
            yield format_sse_event("status", {"status": "completed"})
        finally:
            db.close()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/message")
async def post_chat_message(
    body: MessageRequest,
    auth: AuthenticatedSenior = Depends(require_senior_user),
) -> StreamingResponse:
    return await _stream_chat_turn(auth, body)
