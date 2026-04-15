from enum import Enum
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field

from app.schema.common import EnvelopeResponse


class IngestionUsageStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class UpdateIngestionUsageRequest(BaseModel):
    """PATCH body for ingestion usage (workflow status)."""

    status: Optional[IngestionUsageStatus] = None


class IdentityInput(BaseModel):
    identity: str = Field(..., min_length=1)


SourceType = Literal["url", "pdf"]


class IdentityOutput(BaseModel):
    id: int
    uuid: UUID
    status: str
    identity: str
    created: str | None = None
    modified: str | None = None
    is_deleted: bool = False


class IdentityEnvelope(EnvelopeResponse[IdentityOutput]):
    pass


class TrainURLInput(BaseModel):
    """Context train: vectors always go to the ``tech`` PGVector collection."""

    platform: str = Field(..., min_length=1)
    operating_system: str = Field(..., min_length=1)
    version: str = Field(..., min_length=1)
    url: str = Field(..., min_length=1)
    source_type: SourceType | None = None


class TrainBulkURLsInput(BaseModel):
    platform: str = Field(..., min_length=1)
    operating_system: str = Field(..., min_length=1)
    version: str = Field(..., min_length=1)
    urls: list[str] = Field(..., min_length=1)
    source_type: SourceType | None = None


class ScamTrainURLInput(BaseModel):
    """Scam pipeline: dedicated collection (e.g. ``scam_kb``); no platform/os/version."""

    url: str = Field(..., min_length=1)
    source_type: SourceType | None = None


class ScamTrainBulkURLsInput(BaseModel):
    urls: list[str] = Field(..., min_length=1)
    source_type: SourceType | None = None
