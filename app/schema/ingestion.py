from uuid import UUID

from pydantic import BaseModel

from app.schema.common import EnvelopeResponse


class IngestResponse(BaseModel):
    uuid: UUID | None = None
    status: str
    tokens_used: int = 0
    cost_usd: float = 0.0
    created: str | None = None
    modified: str | None = None


class IngestEnvelope(EnvelopeResponse[IngestResponse]):
    pass


class ScamIngestionItem(BaseModel):
    uuid: UUID
    url: str
    source_type: str
    status: str
    tokens_used: int = 0
    cost_usd: float = 0.0
    created: str | None = None
    modified: str | None = None
    is_deleted: bool = False


class ScamIngestionEnvelope(EnvelopeResponse[ScamIngestionItem]):
    pass
