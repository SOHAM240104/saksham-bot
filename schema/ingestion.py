from uuid import UUID

from pydantic import BaseModel

from schema.common import EnvelopeResponse


class IngestResponse(BaseModel):
    uuid: UUID | None = None
    status: str
    created: str | None = None
    modified: str | None = None
    processed: int
    skipped: int
    failed: int
    chunks: int
    tokens_used: int
    cost_usd: float


class IngestEnvelope(EnvelopeResponse[IngestResponse]):
    pass


class ScamIngestionItem(BaseModel):
    uuid: UUID
    source_key: str
    source_type: str
    status: str
    processed: int
    skipped: int
    failed: int
    chunks: int
    tokens_used: int
    cost_usd: float
    created: str | None = None
    modified: str | None = None
    is_deleted: bool = False


class ScamIngestionEnvelope(EnvelopeResponse[ScamIngestionItem]):
    pass
