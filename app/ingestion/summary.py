"""Aggregate stats returned from ingestion entrypoints."""

from dataclasses import dataclass
from uuid import UUID


@dataclass
class IngestSummary:
    input_count: int = 0
    processed_sources: int = 0
    skipped_duplicates: int = 0
    skipped_invalid: int = 0
    failed_sources: int = 0
    chunks_inserted: int = 0
    tokens_used: int = 0
    cost_usd: float = 0.0
    uuid: UUID | None = None
    status: str = "completed"
