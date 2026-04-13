"""Scam-KB pipeline: minimal metadata in PGVector + ``scam_ingestions`` rows (no tech usage/sources)."""

from __future__ import annotations

import logging
import os as os_module
from typing import Iterable, List, Tuple
from uuid import UUID, uuid4

import pandas as pd
from langchain_core.documents import Document
from sqlalchemy.orm import Session

from app.vectorstore import get_vector_store
from config.database import SessionLocal
from models.ingestion_records import ScamIngestionModel
from settings import SCAM_VECTOR_COLLECTION

from .chunking import clean_text, semantic_chunk
from .db_utils import bootstrap_orm_metadata
from .crawl import extract_pdf_markdown, fetch_markdown
from .source_type import is_valid_url, resolve_source_type
from .summary import IngestSummary
from .token_cost import estimate_embedding_tokens_and_cost

logger = logging.getLogger(__name__)


def _normalize_scam_source_key(url: str) -> str:
    return (url or "").strip()


def _get_scam_ingestion_by_source_key(db: Session, key: str) -> ScamIngestionModel | None:
    if not key:
        return None
    return db.query(ScamIngestionModel).filter(ScamIngestionModel.source_key == key).first()


def _compute_scam_ingestion_status(processed: int, skipped: int, failed: int, explicit: str | None = None) -> str:
    if explicit in ("completed", "failed"):
        return explicit
    if failed > 0 and processed == 0:
        return "failed"
    return "completed"


def _upsert_scam_ingestion_record(
    db: Session,
    *,
    source_key: str,
    source_type: str,
    processed: int,
    skipped: int,
    failed: int,
    chunks: int,
    tokens_used: int,
    cost_usd: float,
    status: str | None = None,
) -> UUID:
    final_status = _compute_scam_ingestion_status(processed, skipped, failed, explicit=status)
    existing = _get_scam_ingestion_by_source_key(db, source_key)
    if existing is not None:
        existing.source_type = source_type
        existing.processed = processed
        existing.skipped = skipped
        existing.failed = failed
        existing.chunks = chunks
        existing.tokens_used = tokens_used
        existing.cost_usd = cost_usd
        existing.status = final_status
        db.commit()
        db.refresh(existing)
        return existing.uuid
    row = ScamIngestionModel(
        source_key=source_key,
        source_type=source_type,
        processed=processed,
        skipped=skipped,
        failed=failed,
        chunks=chunks,
        tokens_used=tokens_used,
        cost_usd=cost_usd,
        status=final_status,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row.uuid


def _scam_source_key_has_embeddings(db: Session, key: str) -> bool:
    """Dedup source of truth: prior successful ingest recorded with chunks."""
    row = _get_scam_ingestion_by_source_key(db, key)
    return row is not None and row.chunks > 0


def _embed_scam_chunks_for_url(
    url: str,
    docs: Iterable[Document],
) -> Tuple[int, int, float]:
    """Store chunks in the scam collection with metadata ``{url}`` only (ids are UUID strings)."""
    docs_list = list(docs)
    if not docs_list:
        return 0, 0, 0.0

    norm_url = _normalize_scam_source_key(url)
    collection_key = SCAM_VECTOR_COLLECTION
    ids = [str(uuid4()) for _ in docs_list]
    to_store = [
        Document(page_content=doc.page_content, metadata={"url": norm_url}) for doc in docs_list
    ]

    texts = [d.page_content for d in to_store]
    tokens_used, cost_usd = estimate_embedding_tokens_and_cost(texts)

    vector_store = get_vector_store(collection_key)
    vector_store.add_documents(documents=to_store, ids=ids)
    return len(docs_list), tokens_used, cost_usd


def process_scam_source(db: Session, url: str, text: str) -> Tuple[int, int, float]:
    """Chunk and embed into the scam collection only (no sources / usage rows; metadata is url-only)."""
    clean = clean_text(text)
    if not clean:
        return 0, 0, 0.0

    chunk_docs = semantic_chunk(clean, source_value=url)
    return _embed_scam_chunks_for_url(url, chunk_docs)


def ingest_scam_single_url(url: str, source_type: str | None = None) -> IngestSummary:
    """Ingest one URL into the scam collection only: ``metadata.url``, chunk ids, embeddings (no sources/usage)."""
    summary = IngestSummary(input_count=1)
    resolved_st = resolve_source_type(url, source_type)
    key = _normalize_scam_source_key(url)

    if not is_valid_url(url):
        summary.skipped_invalid += 1
        summary.status = "failed"
        with SessionLocal() as db:
            bootstrap_orm_metadata(db)
            sk = key or "(invalid-url)"
            summary.uuid = _upsert_scam_ingestion_record(
                db,
                source_key=sk,
                source_type=resolved_st,
                processed=0,
                skipped=1,
                failed=0,
                chunks=0,
                tokens_used=0,
                cost_usd=0.0,
                status="failed",
            )
        return summary

    with SessionLocal() as db:
        bootstrap_orm_metadata(db)
        if _scam_source_key_has_embeddings(db, key):
            summary.skipped_duplicates += 1
            summary.status = "completed"
            existing_ingestion = _get_scam_ingestion_by_source_key(db, key)
            if existing_ingestion is not None:
                summary.uuid = existing_ingestion.uuid
            return summary

        markdown = fetch_markdown(url)
        if not markdown.strip():
            summary.failed_sources += 1
            summary.status = "failed"
            summary.uuid = _upsert_scam_ingestion_record(
                db,
                source_key=key,
                source_type=resolved_st,
                processed=0,
                skipped=0,
                failed=1,
                chunks=0,
                tokens_used=0,
                cost_usd=0.0,
                status=None,
            )
            return summary

        inserted, tokens_used, cost_usd = process_scam_source(db, url, markdown)
        summary.processed_sources += 1
        summary.chunks_inserted += inserted
        summary.tokens_used += tokens_used
        summary.cost_usd = round(summary.cost_usd + cost_usd, 8)
        summary.status = "completed"
        summary.uuid = _upsert_scam_ingestion_record(
            db,
            source_key=key,
            source_type=resolved_st,
            processed=1,
            skipped=0,
            failed=0,
            chunks=inserted,
            tokens_used=tokens_used,
            cost_usd=cost_usd,
            status=None,
        )
    return summary


def ingest_scam_bulk_urls(urls: List[str], source_type: str | None = None) -> IngestSummary:
    resolved_default = resolve_source_type("https://example.com", source_type)
    summary = IngestSummary(input_count=len(urls))

    with SessionLocal() as db:
        bootstrap_orm_metadata(db)
        for raw_url in urls:
            url = (raw_url or "").strip()
            key = _normalize_scam_source_key(url)
            if not is_valid_url(url):
                summary.skipped_invalid += 1
                summary.uuid = _upsert_scam_ingestion_record(
                    db,
                    source_key=key or "(invalid-url)",
                    source_type=resolved_default,
                    processed=0,
                    skipped=1,
                    failed=0,
                    chunks=0,
                    tokens_used=0,
                    cost_usd=0.0,
                    status="failed",
                )
                continue
            resolved_st = resolve_source_type(url, source_type)
            if _scam_source_key_has_embeddings(db, key):
                summary.skipped_duplicates += 1
                existing_ingestion = _get_scam_ingestion_by_source_key(db, key)
                if existing_ingestion is not None:
                    summary.uuid = existing_ingestion.uuid
                continue

            markdown = fetch_markdown(url)
            if not markdown.strip():
                summary.failed_sources += 1
                summary.uuid = _upsert_scam_ingestion_record(
                    db,
                    source_key=key,
                    source_type=resolved_st,
                    processed=0,
                    skipped=0,
                    failed=1,
                    chunks=0,
                    tokens_used=0,
                    cost_usd=0.0,
                    status=None,
                )
                continue
            inserted, tokens_used, cost_usd = process_scam_source(db, url, markdown)
            summary.processed_sources += 1
            summary.chunks_inserted += inserted
            summary.tokens_used += tokens_used
            summary.cost_usd = round(summary.cost_usd + cost_usd, 8)
            summary.uuid = _upsert_scam_ingestion_record(
                db,
                source_key=key,
                source_type=resolved_st,
                processed=1,
                skipped=0,
                failed=0,
                chunks=inserted,
                tokens_used=tokens_used,
                cost_usd=cost_usd,
                status=None,
            )
    if summary.processed_sources > 0:
        summary.status = "completed"
    elif summary.skipped_duplicates > 0 and summary.failed_sources == 0 and summary.skipped_invalid == 0:
        summary.status = "completed"
    elif summary.failed_sources > 0 or summary.skipped_invalid > 0:
        summary.status = "failed"
    else:
        summary.status = "completed"
    return summary


def ingest_scam_excel(file_path: str) -> IngestSummary:
    try:
        df = pd.read_excel(file_path)
    except Exception:
        logger.exception("Error reading Excel for scam ingestion: %s", file_path)
        raise
    normalized = {str(c).strip().lower(): c for c in df.columns}
    url_col = normalized.get("url") or normalized.get("urls")
    if not url_col:
        err = ValueError("Excel must contain a URL column: url / urls")
        logger.warning("%s (file=%s)", err, file_path)
        raise err

    urls = [str(v).strip() for v in df[url_col].dropna().tolist() if str(v).strip()]
    return ingest_scam_bulk_urls(urls)


def ingest_scam_pdf(file_path: str) -> IngestSummary:
    summary = IngestSummary(input_count=1)
    source_key = os_module.path.abspath(file_path)

    if not os_module.path.exists(file_path):
        summary.failed_sources += 1
        summary.status = "failed"
        with SessionLocal() as db:
            bootstrap_orm_metadata(db)
            summary.uuid = _upsert_scam_ingestion_record(
                db,
                source_key=source_key,
                source_type="pdf",
                processed=0,
                skipped=0,
                failed=1,
                chunks=0,
                tokens_used=0,
                cost_usd=0.0,
                status=None,
            )
        return summary

    with SessionLocal() as db:
        bootstrap_orm_metadata(db)
        if _scam_source_key_has_embeddings(db, source_key):
            summary.skipped_duplicates += 1
            summary.status = "completed"
            existing_ingestion = _get_scam_ingestion_by_source_key(db, source_key)
            if existing_ingestion is not None:
                summary.uuid = existing_ingestion.uuid
            return summary

        markdown = extract_pdf_markdown(file_path)
        if not markdown.strip():
            summary.failed_sources += 1
            summary.status = "failed"
            summary.uuid = _upsert_scam_ingestion_record(
                db,
                source_key=source_key,
                source_type="pdf",
                processed=0,
                skipped=0,
                failed=1,
                chunks=0,
                tokens_used=0,
                cost_usd=0.0,
                status=None,
            )
            return summary

        inserted, tokens_used, cost_usd = process_scam_source(db, source_key, markdown)
        summary.processed_sources += 1
        summary.chunks_inserted += inserted
        summary.tokens_used += tokens_used
        summary.cost_usd = round(summary.cost_usd + cost_usd, 8)
        summary.status = "completed"
        summary.uuid = _upsert_scam_ingestion_record(
            db,
            source_key=source_key,
            source_type="pdf",
            processed=1,
            skipped=0,
            failed=0,
            chunks=inserted,
            tokens_used=tokens_used,
            cost_usd=cost_usd,
            status=None,
        )
    return summary
