from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.models.chat.chat import Message


class MessageRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    selection_id: str | None = Field(
        default=None,
        max_length=64,
        description="Optional chip/button id when the user taps structured UI",
    )


class MessageResponse(BaseModel):
    """One chatbot_message row — matches how messages are stored in Postgres."""

    id: int
    uuid: UUID
    thread_id: int
    user_question: str = Field(default="", validation_alias="user_message")
    ai_response: str = Field(default="", validation_alias="bot_response")
    tech_saathi_response: str | None = None
    tech_saathi_id: int | None = None
    confidence_score: float | None = None
    message_source: str | None = None
    created: datetime | None = None

    model_config = {"populate_by_name": True}

    @classmethod
    def from_message(cls, msg: Message) -> "MessageResponse":
        return cls(
            id=msg.id,
            uuid=msg.uuid,
            thread_id=msg.thread_id,
            user_question=(msg.user_message or "").strip(),
            ai_response=(msg.bot_response or "").strip(),
            tech_saathi_response=(msg.tech_saathi_response or "").strip() or None,
            tech_saathi_id=msg.tech_saathi_id,
            confidence_score=msg.confidence_score,
            message_source=(msg.message_source or "").strip() or None,
            created=msg.created,
        )


class MessageListResponse(BaseModel):
    data: list[MessageResponse]
