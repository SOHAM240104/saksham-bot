"""Tech (contextual) pipeline: sources + ``tech`` PGVector collection + usage rows."""

from __future__ import annotations

import logging
import os as os_module
from typing import Callable, Iterable, List, Tuple
from uuid import UUID, uuid4

import pandas as pd
from langchain_core.documents import Document
from sqlalchemy.orm import Session

from app.vectorstore import get_vector_store
from config.database import SessionLocal
from models.ingestion_records import IngestionUsageModel, SourceModel
from settings import TECH_VECTOR_COLLECTION

from .chunking import clean_text, semantic_chunk
from .db_utils import bootstrap_orm_metadata
from .crawl import extract_pdf_markdown, fetch_markdown
from .source_type import is_valid_url, resolve_source_type
from .summary import IngestSummary
from .token_cost import estimate_embedding_tokens_and_cost

logger = logging.getLogger(__name__)


def _url_exists(db: Session, url: str, source_type: str) -> bool:
    return db.query(SourceModel.id).filter(SourceModel.type == source_type, SourceModel.source == url).first() is not None


def _insert_source(
    db: Session,
    source: str,
    source_type: str,
    platform: str,
    os_name: str,
    version: str,
) -> int:
    existing = db.query(SourceModel).filter(SourceModel.source == source, SourceModel.type == source_type).first()
    if existing is not None:
        changed = False
        if existing.platform != platform:
            existing.platform = platform
            changed = True
        if existing.os != os_name:
            existing.os = os_name
            changed = True
        if existing.version != version:
            existing.version = version
            changed = True
        if changed:
            db.flush()
        return int(existing.id)

    row = SourceModel(
        source=source,
        type=source_type,
        platform=platform,
        os=os_name,
        version=version,
    )
    db.add(row)
    db.flush()
    return int(row.id)


def _insert_chunks(
    db: Session,
    source_id: int,
    source_value: str,
    source_type: str,
    platform: str,
    os_name: str,
    version: str,
    docs: Iterable[Document],
) -> Tuple[int, int, float]:
    docs_list = list(docs)
    if not docs_list:
        return 0, 0, 0.0

    collection_key = TECH_VECTOR_COLLECTION
    ids = [str(uuid4()) for _ in docs_list]
    to_store: List[Document] = []
    for doc in docs_list:
        meta = dict(doc.metadata or {})
        meta.update(
            {
                "source_id": source_id,
                "source": source_value,
                "source_type": source_type,
                "platform": platform,
                "os": os_name,
                "version": version,
                "title": meta.get("title", "untitled"),
                "header": meta.get("header", ""),
            }
        )
        to_store.append(Document(page_content=doc.page_content, metadata=meta))

    texts = [d.page_content for d in to_store]
    tokens_used, cost_usd = estimate_embedding_tokens_and_cost(texts)

    vector_store = get_vector_store(collection_key)
    vector_store.add_documents(documents=to_store, ids=ids)

    db.commit()
    return len(docs_list), tokens_used, cost_usd


def _log_usage(
    db: Session,
    source: str,
    source_type: str,
    platform: str,
    os_name: str,
    version: str,
    processed: int,
    skipped: int,
    failed: int,
    chunks: int,
    tokens_used: int,
    cost_usd: float,
) -> tuple[UUID, str]:
    status = "completed"
    if failed > 0 and processed == 0:
        status = "failed"
    elif skipped > 0 and processed == 0:
        status = "not_started"

    usage = IngestionUsageModel(
        source=source,
        source_type=source_type,
        platform=platform,
        os=os_name,
        version=version,
        processed=processed,
        skipped=skipped,
        failed=failed,
        chunks=chunks,
        tokens_used=tokens_used,
        cost_usd=cost_usd,
        status=status,
    )
    db.add(usage)
    db.commit()
    db.refresh(usage)
    return usage.uuid, usage.status


def process_source(
    db: Session,
    source: str,
    text: str,
    platform: str,
    os_name: str,
    version: str,
    source_type: str | None = None,
    *,
    source_storage_key: str | None = None,
) -> Tuple[int, int, float]:
    clean = clean_text(text)
    if not clean:
        return 0, 0, 0.0

    resolved_source_type = resolve_source_type(source, source_type)
    row_source = source_storage_key if source_storage_key is not None else source
    source_id = _insert_source(
        db=db,
        source=row_source,
        source_type=resolved_source_type,
        platform=platform,
        os_name=os_name,
        version=version,
    )
    chunk_docs = semantic_chunk(clean, source_value=source)
    return _insert_chunks(
        db,
        source_id=source_id,
        source_value=source,
        source_type=resolved_source_type,
        platform=platform,
        os_name=os_name,
        version=version,
        docs=chunk_docs,
    )


def ingest_single_url(
    url: str,
    platform: str,
    os: str,
    version: str,
    source_type: str | None = None,
    *,
    source_storage_key: str | None = None,
) -> IngestSummary:
    summary = IngestSummary(input_count=1)
    resolved_source_type = resolve_source_type(url, source_type)
    dedup_key = source_storage_key if source_storage_key is not None else url
    if not is_valid_url(url):
        summary.skipped_invalid += 1
        with SessionLocal() as db:
            bootstrap_orm_metadata(db)
            usage_uuid, usage_status = _log_usage(
                db=db,
                source=url or "(invalid-url)",
                source_type=resolved_source_type,
                platform=platform,
                os_name=os,
                version=version,
                processed=0,
                skipped=1,
                failed=0,
                chunks=0,
                tokens_used=0,
                cost_usd=0.0,
            )
            summary.uuid = usage_uuid
            summary.status = usage_status
        return summary

    with SessionLocal() as db:
        bootstrap_orm_metadata(db)
        if _url_exists(db, dedup_key, resolved_source_type):
            summary.skipped_duplicates += 1
            usage_uuid, usage_status = _log_usage(
                db=db,
                source=url,
                source_type=resolved_source_type,
                platform=platform,
                os_name=os,
                version=version,
                processed=0,
                skipped=1,
                failed=0,
                chunks=0,
                tokens_used=0,
                cost_usd=0.0,
            )
            summary.uuid = usage_uuid
            summary.status = usage_status
            return summary

        markdown = fetch_markdown(url)
        if not markdown.strip():
            summary.failed_sources += 1
            usage_uuid, usage_status = _log_usage(
                db=db,
                source=url,
                source_type=resolved_source_type,
                platform=platform,
                os_name=os,
                version=version,
                processed=0,
                skipped=0,
                failed=1,
                chunks=0,
                tokens_used=0,
                cost_usd=0.0,
            )
            summary.uuid = usage_uuid
            summary.status = usage_status
            return summary

        inserted, tokens_used, cost_usd = process_source(
            db=db,
            source=url,
            text=markdown,
            platform=platform,
            os_name=os,
            version=version,
            source_type=resolved_source_type,
            source_storage_key=source_storage_key,
        )
        summary.processed_sources += 1
        summary.chunks_inserted += inserted
        summary.tokens_used += tokens_used
        summary.cost_usd = round(summary.cost_usd + cost_usd, 8)
        usage_uuid, usage_status = _log_usage(
            db=db,
            source=url,
            source_type=resolved_source_type,
            platform=platform,
            os_name=os,
            version=version,
            processed=1,
            skipped=0,
            failed=0,
            chunks=inserted,
            tokens_used=tokens_used,
            cost_usd=cost_usd,
        )
        summary.uuid = usage_uuid
        summary.status = usage_status
    return summary


def ingest_bulk_urls(
    urls: List[str],
    platform: str,
    os: str,
    version: str,
    source_type: str | None = None,
    *,
    source_storage_key_fn: Callable[[str], str] | None = None,
) -> IngestSummary:
    summary = IngestSummary(input_count=len(urls))
    resolved_default_source_type = resolve_source_type("https://example.com", source_type)

    def _row_key(u: str) -> str:
        return source_storage_key_fn(u) if source_storage_key_fn is not None else u

    with SessionLocal() as db:
        bootstrap_orm_metadata(db)
        for raw_url in urls:
            url = (raw_url or "").strip()
            if not is_valid_url(url):
                summary.skipped_invalid += 1
                usage_uuid, usage_status = _log_usage(
                    db=db,
                    source=url or "(invalid-url)",
                    source_type=resolved_default_source_type,
                    platform=platform,
                    os_name=os,
                    version=version,
                    processed=0,
                    skipped=1,
                    failed=0,
                    chunks=0,
                    tokens_used=0,
                    cost_usd=0.0,
                )
                summary.uuid = usage_uuid
                summary.status = usage_status
                continue
            row_key = _row_key(url)
            if _url_exists(db, row_key, resolved_default_source_type):
                summary.skipped_duplicates += 1
                usage_uuid, usage_status = _log_usage(
                    db=db,
                    source=url,
                    source_type=resolved_default_source_type,
                    platform=platform,
                    os_name=os,
                    version=version,
                    processed=0,
                    skipped=1,
                    failed=0,
                    chunks=0,
                    tokens_used=0,
                    cost_usd=0.0,
                )
                summary.uuid = usage_uuid
                summary.status = usage_status
                continue

            markdown = fetch_markdown(url)
            if not markdown.strip():
                summary.failed_sources += 1
                usage_uuid, usage_status = _log_usage(
                    db=db,
                    source=url,
                    source_type=resolved_default_source_type,
                    platform=platform,
                    os_name=os,
                    version=version,
                    processed=0,
                    skipped=0,
                    failed=1,
                    chunks=0,
                    tokens_used=0,
                    cost_usd=0.0,
                )
                summary.uuid = usage_uuid
                summary.status = usage_status
                continue

            sk = source_storage_key_fn(url) if source_storage_key_fn is not None else None
            inserted, tokens_used, cost_usd = process_source(
                db=db,
                source=url,
                text=markdown,
                platform=platform,
                os_name=os,
                version=version,
                source_type=resolved_default_source_type,
                source_storage_key=sk,
            )
            summary.processed_sources += 1
            summary.chunks_inserted += inserted
            summary.tokens_used += tokens_used
            summary.cost_usd = round(summary.cost_usd + cost_usd, 8)
            usage_uuid, usage_status = _log_usage(
                db=db,
                source=url,
                source_type=resolved_default_source_type,
                platform=platform,
                os_name=os,
                version=version,
                processed=1,
                skipped=0,
                failed=0,
                chunks=inserted,
                tokens_used=tokens_used,
                cost_usd=cost_usd,
            )
            summary.uuid = usage_uuid
            summary.status = usage_status
    return summary


def ingest_excel(
    file_path: str,
    platform: str,
    os: str,
    version: str,
    *,
    source_storage_key_fn: Callable[[str], str] | None = None,
) -> IngestSummary:
    try:
        df = pd.read_excel(file_path)
    except Exception:
        logger.exception("Error reading Excel for tech ingestion: %s", file_path)
        raise
    normalized = {str(c).strip().lower(): c for c in df.columns}
    url_col = normalized.get("url") or normalized.get("urls")
    if not url_col:
        err = ValueError("Excel must contain a URL column: url / urls")
        logger.warning("%s (file=%s)", err, file_path)
        raise err

    urls = [str(v).strip() for v in df[url_col].dropna().tolist() if str(v).strip()]
    return ingest_bulk_urls(
        urls,
        platform=platform,
        os=os,
        version=version,
        source_storage_key_fn=source_storage_key_fn,
    )


def ingest_pdf(
    file_path: str,
    platform: str,
    os: str,
    version: str,
    *,
    source_storage_key: str | None = None,
) -> IngestSummary:
    summary = IngestSummary(input_count=1)
    if not os_module.path.exists(file_path):
        summary.failed_sources += 1
        return summary

    source_value = os_module.path.abspath(file_path)

    with SessionLocal() as db:
        bootstrap_orm_metadata(db)
        markdown = extract_pdf_markdown(file_path)
        if not markdown.strip():
            summary.failed_sources += 1
            usage_uuid, usage_status = _log_usage(
                db=db,
                source=source_value,
                source_type="pdf",
                platform=platform,
                os_name=os,
                version=version,
                processed=0,
                skipped=0,
                failed=1,
                chunks=0,
                tokens_used=0,
                cost_usd=0.0,
            )
            summary.uuid = usage_uuid
            summary.status = usage_status
            return summary

        inserted, tokens_used, cost_usd = process_source(
            db=db,
            source=source_value,
            text=markdown,
            platform=platform,
            os_name=os,
            version=version,
            source_storage_key=source_storage_key,
        )
        summary.processed_sources += 1
        summary.chunks_inserted += inserted
        summary.tokens_used += tokens_used
        summary.cost_usd = round(summary.cost_usd + cost_usd, 8)
        usage_uuid, usage_status = _log_usage(
            db=db,
            source=source_value,
            source_type="pdf",
            platform=platform,
            os_name=os,
            version=version,
            processed=1,
            skipped=0,
            failed=0,
            chunks=inserted,
            tokens_used=tokens_used,
            cost_usd=cost_usd,
        )
        summary.uuid = usage_uuid
        summary.status = usage_status
    return summary
