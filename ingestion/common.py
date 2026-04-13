"""Shared ingestion helpers used by multiple pipelines."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, TypeVar

import pandas as pd


def read_excel_urls(file_path: str, logger, pipeline_label: str) -> list[str]:
    try:
        df = pd.read_excel(file_path)
    except Exception:
        logger.exception("Error reading Excel for %s ingestion: %s", pipeline_label, file_path)
        raise

    normalized = {str(c).strip().lower(): c for c in df.columns}
    url_col = normalized.get("url") or normalized.get("urls")
    if not url_col:
        err = ValueError("Excel must contain a URL column: url / urls")
        logger.warning("%s (file=%s)", err, file_path)
        raise err
    return [str(v).strip() for v in df[url_col].dropna().tolist() if str(v).strip()]


def derive_ingestion_status(processed: int, skipped: int, failed: int) -> str:
    if failed > 0 and processed == 0:
        return "failed"
    if skipped > 0 and processed == 0:
        return "not_started"
    return "completed"


@dataclass
class BulkExecutionStats:
    processed: int = 0
    skipped_invalid: int = 0
    skipped_duplicates: int = 0
    failed: int = 0
    chunks: int = 0
    tokens_used: int = 0
    cost_usd: float = 0.0


T = TypeVar("T")


def process_bulk_items(
    items: Iterable[T],
    normalize: Callable[[T], str],
    process_item: Callable[[str], None],
    on_item_exception: Callable[[str], None],
) -> None:
    for raw_item in items:
        item = normalize(raw_item)
        try:
            process_item(item)
        except Exception:
            on_item_exception(item)


IngestionRunStats = BulkExecutionStats
