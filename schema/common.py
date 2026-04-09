from typing import Generic, List, TypeVar
from uuid import UUID

from pydantic import BaseModel, Field

T = TypeVar("T")


class EnvelopeBase(BaseModel):
    request_id: UUID
    status_code: int = Field(..., ge=100, le=599)
    message: str


class EnvelopeResponse(EnvelopeBase, Generic[T]):
    data: List[T]
