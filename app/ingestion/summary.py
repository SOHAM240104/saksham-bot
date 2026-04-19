"""Aggregate stats returned from ingestion entrypoints."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID


@dataclass
class IngestSummary:
    input_count: int = 0
    processed_sources: int = 0
    skipped_duplicates: int = 0
    skipped_invalid: int = 0
    failed_sources: int = 0
    tokens_used: int = 0
    cost_usd: float = 0.0
    status: str = "no_op"
    uuid: UUID | None = None
    results: list[dict[str, Any]] = field(default_factory=list)
