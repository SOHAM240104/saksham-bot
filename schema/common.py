from typing import Generic, List, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class EnvelopeBase(BaseModel):
    status_code: int = Field(..., ge=100, le=599)


class EnvelopeResponse(EnvelopeBase, Generic[T]):
    data: List[T]
