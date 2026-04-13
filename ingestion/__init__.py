"""Ingest URLs, Excel URL lists, and PDFs into PGVector (chunked text + embeddings)."""

from .scam import (
    ingest_scam_bulk_urls,
    ingest_scam_excel,
    ingest_scam_pdf,
    ingest_scam_single_url,
    process_scam_source,
)
from .summary import IngestSummary
from .tech import (
    ingest_bulk_urls,
    ingest_excel,
    ingest_pdf,
    ingest_single_url,
    process_source,
)

__all__ = [
    "IngestSummary",
    "ingest_bulk_urls",
    "ingest_excel",
    "ingest_pdf",
    "ingest_single_url",
    "ingest_scam_bulk_urls",
    "ingest_scam_excel",
    "ingest_scam_pdf",
    "ingest_scam_single_url",
    "process_scam_source",
    "process_source",
]
