from typing import List

from pydantic import BaseModel, Field


class SourceInput(BaseModel):
    platform: str = Field(..., min_length=1)
    os: str = Field(..., min_length=1)
    version: str = Field(..., min_length=1)


class URLInput(SourceInput):
    url: str = Field(..., min_length=1)


class BulkURLInput(SourceInput):
    urls: List[str] = Field(..., min_length=1)


class IngestResponse(BaseModel):
    processed: int
    skipped: int
    failed: int
    chunks: int
    tokens_used: int
    cost_usd: float
