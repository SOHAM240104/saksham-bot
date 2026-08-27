from datetime import datetime
from typing import Generic, TypeVar

from pydantic import BaseModel, Field

from app.schema.messages import MessageResponse

T = TypeVar("T")


class AgentMessageRequest(BaseModel):
    """Agent reply body — text only (no website chip selection_id)."""

    message: str = Field(..., min_length=1, max_length=4000)


class AgentConversationResponse(BaseModel):
    conversation_id: int
    senior_id: int | None = None
    senior_name: str | None = None
    state: str
    thread_id: int | None = None
    assigned_techsaathi_id: int | None = None
    preview: str = ""
    last_activity: datetime | None = None


class AgentResolveResponse(BaseModel):
    conversation_id: int
    state: str
    resolved_thread_id: int
    new_chatbot_thread_id: int
    assigned_techsaathi_id: int | None = None
    senior_id: int | None = None


class AgentDataResponse(BaseModel, Generic[T]):
    """Thin ``{"data": ...}`` wrapper used by agent routes (no admin status_code)."""

    data: T


AgentConversationListResponse = AgentDataResponse[list[AgentConversationResponse]]
AgentConversationDetailResponse = AgentDataResponse[AgentConversationResponse]
AgentMessageListResponse = AgentDataResponse[list[MessageResponse]]
AgentMessageDetailResponse = AgentDataResponse[MessageResponse]
AgentResolveDetailResponse = AgentDataResponse[AgentResolveResponse]
