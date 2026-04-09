from typing import List
from uuid import UUID

from pydantic import BaseModel, Field

from schema.common import EnvelopeResponse

class SourceInput(BaseModel):
    platform: str = Field(..., min_length=1)
    os: str = Field(..., min_length=1)
    version: str = Field(..., min_length=1)


class URLInput(SourceInput):
    url: str = Field(..., min_length=1)


class BulkURLInput(SourceInput):
    urls: List[str] = Field(..., min_length=1)


class IngestResponse(BaseModel):
    uuid: UUID
    status: str
    processed: int
    skipped: int
    failed: int
    chunks: int
    tokens_used: int
    cost_usd: float


class IngestEnvelope(EnvelopeResponse[IngestResponse]):
    pass
