from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field

from schema.common import EnvelopeResponse


class ArchiveStatePatchRequest(BaseModel):
    """PATCH body for archive state on context entities."""

    is_archived: Optional[bool] = None


class UpdateIngestionUsageRequest(BaseModel):
    """PATCH body for ingestion usage (archive + optional workflow status)."""

    is_archived: Optional[bool] = None
    status: Optional[str] = None


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
    is_archived: bool = False
    is_deleted: bool = False


class IdentityEnvelope(EnvelopeResponse[IdentityOutput]):
    pass


class TrainURLInput(BaseModel):
    """Context train: vectors always go to the ``tech`` PGVector collection."""

    url: str = Field(..., min_length=1)
    source_type: SourceType | None = None


class TrainBulkURLsInput(BaseModel):
    urls: list[str] = Field(..., min_length=1)
    source_type: SourceType | None = None


class ScamTrainURLInput(BaseModel):
    """Scam pipeline: dedicated collection (e.g. ``scam_kb``); no platform/os/version."""

    url: str = Field(..., min_length=1)
    source_type: SourceType | None = None


class ScamTrainBulkURLsInput(BaseModel):
    urls: list[str] = Field(..., min_length=1)
    source_type: SourceType | None = None
