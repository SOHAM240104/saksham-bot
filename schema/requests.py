from typing import List
from uuid import UUID

from pydantic import BaseModel, Field

from schema.common import EnvelopeResponse

class NameInput(BaseModel):
    name: str = Field(..., min_length=1)


class NameOutput(BaseModel):
    id: int
    uuid: UUID
    status: str
    name: str
    created: str | None = None
    modified: str | None = None
    is_archived: bool = False
    is_deleted: bool = False


class NameEnvelope(EnvelopeResponse[NameOutput]):
    pass


class TrainURLInput(BaseModel):
    url: str = Field(..., min_length=1)


class TrainBulkURLsInput(BaseModel):
    urls: list[str] = Field(..., min_length=1)
