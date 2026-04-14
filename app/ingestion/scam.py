"""Scam-KB pipeline: minimal metadata in PGVector + ``scam_ingestions`` rows."""

from __future__ import annotations

import logging
import os as os_module
from typing import Iterable, List, Tuple
from uuid import UUID, uuid4

from langchain_core.documents import Document
from sqlalchemy.orm import Session

from app.vectorstore import get_vector_store
from app.config.database import SessionLocal
from app.models.ingestion_records import ScamIngestionModel
from app.settings import SCAM_VECTOR_COLLECTION

from .chunking import clean_text, semantic_chunk
from .crawl import extract_pdf_markdown, fetch_markdown
from .common import IngestionRunStats, derive_ingestion_status, process_bulk_items, read_excel_urls
from .source_type import is_valid_url, resolve_source_type
from .summary import IngestSummary
from .token_cost import estimate_embedding_tokens_and_cost

logger = logging.getLogger(__name__)


def _normalize_scam_source_key(source: str) -> str:
    return (source or "").strip()


def _get_scam_ingestion_by_source_key(db: Session, source_key: str) -> ScamIngestionModel | None:
    if not source_key:
        return None
    return db.query(ScamIngestionModel).filter(ScamIngestionModel.source_key == source_key).first()


def _compute_scam_ingestion_status(processed: int, skipped: int, failed: int, explicit: str | None = None) -> str:
    if explicit in ("completed", "failed", "not_started"):
        return explicit
    return derive_ingestion_status(processed=processed, skipped=skipped, failed=failed)


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
        db.flush()
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
    db.flush()
    return row.uuid


def _scam_source_key_has_embeddings(db: Session, source_key: str) -> bool:
    row = _get_scam_ingestion_by_source_key(db, source_key)
    return row is not None and row.chunks > 0


def _embed_scam_chunks_for_source(source_key: str, docs: Iterable[Document]) -> Tuple[int, int, float]:
    docs_list = list(docs)
    if not docs_list:
        return 0, 0, 0.0

    ids = [str(uuid4()) for _ in docs_list]
    to_store = [Document(page_content=doc.page_content, metadata={"url": source_key}) for doc in docs_list]
    tokens_used, cost_usd = estimate_embedding_tokens_and_cost([d.page_content for d in to_store])
    get_vector_store(SCAM_VECTOR_COLLECTION).add_documents(documents=to_store, ids=ids)
    return len(docs_list), tokens_used, cost_usd


def process_scam_source(source_key: str, text: str) -> Tuple[int, int, float]:
    clean = clean_text(text)
    if not clean:
        return 0, 0, 0.0
    chunk_docs = semantic_chunk(clean, source_value=source_key)
    return _embed_scam_chunks_for_source(source_key, chunk_docs)


def ingest_scam_single_url(url: str, source_type: str | None = None) -> IngestSummary:
    return ingest_scam_bulk_urls([url], source_type=source_type)


def ingest_scam_bulk_urls(urls: List[str], source_type: str | None = None) -> IngestSummary:
    summary = IngestSummary(input_count=len(urls))
    stats = IngestionRunStats()
    with SessionLocal() as db:
        def _process(raw_url: str) -> None:
            url = (raw_url or "").strip()
            source_key = _normalize_scam_source_key(url) or "(invalid-url)"
            resolved_source_type = resolve_source_type(url or "https://example.com", source_type)
            if not is_valid_url(url):
                stats.skipped_invalid += 1
                summary.uuid = _upsert_scam_ingestion_record(
                    db=db,
                    source_key=source_key,
                    source_type=resolved_source_type,
                    processed=0,
                    skipped=1,
                    failed=0,
                    chunks=0,
                    tokens_used=0,
                    cost_usd=0.0,
                    status=None,
                )
                db.commit()
                return

            if _scam_source_key_has_embeddings(db, source_key):
                stats.skipped_duplicates += 1
                existing = _get_scam_ingestion_by_source_key(db, source_key)
                if existing is not None:
                    summary.uuid = existing.uuid
                return

            markdown = fetch_markdown(url)
            if not markdown.strip():
                stats.failed += 1
                summary.uuid = _upsert_scam_ingestion_record(
                    db=db,
                    source_key=source_key,
                    source_type=resolved_source_type,
                    processed=0,
                    skipped=0,
                    failed=1,
                    chunks=0,
                    tokens_used=0,
                    cost_usd=0.0,
                    status=None,
                )
                db.commit()
                return

            inserted, tokens_used, cost_usd = process_scam_source(source_key, markdown)
            summary.uuid = _upsert_scam_ingestion_record(
                db=db,
                source_key=source_key,
                source_type=resolved_source_type,
                processed=1,
                skipped=0,
                failed=0,
                chunks=inserted,
                tokens_used=tokens_used,
                cost_usd=cost_usd,
                status=None,
            )
            db.commit()
            stats.processed += 1
            stats.chunks += inserted
            stats.tokens_used += tokens_used
            stats.cost_usd = round(stats.cost_usd + cost_usd, 8)

        def _on_exception(raw_url: str) -> None:
            url = (raw_url or "").strip()
            source_key = _normalize_scam_source_key(url) or "(invalid-url)"
            resolved_source_type = resolve_source_type(url or "https://example.com", source_type)
            try:
                db.rollback()
                logger.exception("Scam ingestion failed for source: %s", source_key)
                stats.failed += 1
                summary.uuid = _upsert_scam_ingestion_record(
                    db=db,
                    source_key=source_key,
                    source_type=resolved_source_type,
                    processed=0,
                    skipped=0,
                    failed=1,
                    chunks=0,
                    tokens_used=0,
                    cost_usd=0.0,
                    status=None,
                )
                db.commit()
            except Exception:
                db.rollback()
        process_bulk_items(
            items=urls,
            normalize=lambda raw: (raw or "").strip(),
            process_item=_process,
            on_item_exception=_on_exception,
        )
    summary.processed_sources = stats.processed
    summary.skipped_invalid = stats.skipped_invalid
    summary.skipped_duplicates = stats.skipped_duplicates
    summary.failed_sources = stats.failed
    summary.chunks_inserted = stats.chunks
    summary.tokens_used = stats.tokens_used
    summary.cost_usd = stats.cost_usd
    summary.status = derive_ingestion_status(
        processed=summary.processed_sources,
        skipped=summary.skipped_duplicates + summary.skipped_invalid,
        failed=summary.failed_sources,
    )
    return summary


def ingest_scam_excel(file_path: str) -> IngestSummary:
    urls = read_excel_urls(file_path, logger, "scam")
    return ingest_scam_bulk_urls(urls)


def ingest_scam_pdf(file_path: str) -> IngestSummary:
    summary = IngestSummary(input_count=1)
    source_key = os_module.path.abspath(file_path)
    if not os_module.path.exists(file_path):
        summary.failed_sources += 1
        summary.status = "failed"
        with SessionLocal() as db:
            summary.uuid = _upsert_scam_ingestion_record(
                db=db,
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
            db.commit()
        return summary

    with SessionLocal() as db:
        try:
            if _scam_source_key_has_embeddings(db, source_key):
                summary.skipped_duplicates += 1
                summary.status = "completed"
                existing = _get_scam_ingestion_by_source_key(db, source_key)
                if existing is not None:
                    summary.uuid = existing.uuid
                return summary

            markdown = extract_pdf_markdown(file_path)
            if not markdown.strip():
                summary.failed_sources += 1
                summary.status = "failed"
                summary.uuid = _upsert_scam_ingestion_record(
                    db=db,
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
                db.commit()
                return summary

            inserted, tokens_used, cost_usd = process_scam_source(source_key, markdown)
            summary.uuid = _upsert_scam_ingestion_record(
                db=db,
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
            db.commit()
            summary.processed_sources += 1
            summary.chunks_inserted += inserted
            summary.tokens_used += tokens_used
            summary.cost_usd = round(summary.cost_usd + cost_usd, 8)
            summary.status = "completed"
            return summary
        except Exception:
            db.rollback()
            logger.exception("Scam PDF ingestion failed: %s", source_key)
            summary.failed_sources += 1
            summary.status = "failed"
            summary.uuid = _upsert_scam_ingestion_record(
                db=db,
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
            db.commit()
            return summary
