"""Tech (contextual) pipeline: ``tech`` PGVector collection + ingestion rows."""

from __future__ import annotations

import logging
import os as os_module
from typing import Iterable, List, Tuple
from uuid import UUID, uuid4

from langchain_core.documents import Document
from sqlalchemy.orm import Session

from app.vectorstore import get_vector_store
from app.config.base import SessionLocal
from app.models.chatbot.context import OSModel, PlatformModel, VersionModel
from app.models.chatbot.ingestion_records import IngestionUsageModel
from app.settings import TECH_VECTOR_COLLECTION

from .chunking import clean_text, semantic_chunk
from .crawl import extract_pdf_markdown, fetch_markdown
from .common import IngestionRunStats, derive_ingestion_status, process_bulk_items, read_excel_urls
from .source_type import is_valid_url, resolve_source_type
from .summary import IngestSummary
from .token_cost import estimate_embedding_tokens_and_cost

logger = logging.getLogger(__name__)


def _resolve_context_ids(db: Session, platform: str, operating_system: str, version: str) -> tuple[int | None, int | None, int | None]:
    platform_row = db.query(PlatformModel).filter(PlatformModel.identity == platform).first()
    if platform_row is None:
        return None, None, None

    os_row = (
        db.query(OSModel)
        .filter(OSModel.platform_id == platform_row.id, OSModel.identity == operating_system)
        .first()
    )
    if os_row is None:
        return int(platform_row.id), None, None

    version_row = db.query(VersionModel).filter(VersionModel.os_id == os_row.id, VersionModel.identity == version).first()
    version_id = int(version_row.id) if version_row is not None else None
    return int(platform_row.id), int(os_row.id), version_id


def _get_usage_row(db: Session, url: str, source_type: str) -> IngestionUsageModel | None:
    return (
        db.query(IngestionUsageModel)
        .filter(
            IngestionUsageModel.url == url,
            IngestionUsageModel.source_type == source_type,
            IngestionUsageModel.is_deleted.is_(False),
        )
        .first()
    )


def _url_exists(db: Session, url: str, source_type: str) -> bool:
    row = _get_usage_row(db, url, source_type)
    return row is not None and row.status == "completed"


def _insert_chunks(
    source_value: str,
    source_type: str,
    platform: str,
    operating_system: str,
    version: str,
    docs: Iterable[Document],
) -> Tuple[int, int, float]:
    docs_list = list(docs)
    if not docs_list:
        return 0, 0, 0.0

    ids = [str(uuid4()) for _ in docs_list]
    to_store: List[Document] = []
    for doc in docs_list:
        meta = dict(doc.metadata or {})
        meta.update(
            {
                "source": source_value,
                "source_type": source_type,
                "platform": platform,
                "os": operating_system,
                "version": version,
                "title": meta.get("title", "untitled"),
                "header": meta.get("header", ""),
            }
        )
        to_store.append(Document(page_content=doc.page_content, metadata=meta))

    tokens_used, cost_usd = estimate_embedding_tokens_and_cost([d.page_content for d in to_store])
    get_vector_store(TECH_VECTOR_COLLECTION).add_documents(documents=to_store, ids=ids)
    return len(docs_list), tokens_used, cost_usd


def _log_usage(
    db: Session,
    url: str,
    source_type: str,
    platform: str,
    operating_system: str,
    version: str,
    tokens_used: int,
    cost_usd: float,
    status: str,
) -> tuple[UUID, str]:

    platform_id, os_id, version_id = _resolve_context_ids(db, platform, operating_system, version)

    existing = (
        db.query(IngestionUsageModel)
        .filter(
            IngestionUsageModel.url == url,
            IngestionUsageModel.source_type == source_type,
            IngestionUsageModel.is_deleted.is_(False),
        )
        .first()
    )

    if existing:
        existing.tokens_used = tokens_used
        existing.cost_usd = cost_usd
        existing.status = status
        existing.platform_id = platform_id
        existing.os_id = os_id
        existing.version_id = version_id

        db.flush()
        return existing.uuid, existing.status

    usage = IngestionUsageModel(
        url=url,
        source_type=source_type,
        platform_id=platform_id,
        os_id=os_id,
        version_id=version_id,
        tokens_used=tokens_used,
        cost_usd=cost_usd,
        status=status,
    )
    db.add(usage)
    db.flush()

    return usage.uuid, usage.status


def process_source(
    db: Session,
    source: str,
    text: str,
    platform: str,
    operating_system: str,
    version: str,
    source_type: str | None = None,
) -> Tuple[int, int, float]:
    clean = clean_text(text)
    if not clean:
        return 0, 0, 0.0
    resolved_source_type = resolve_source_type(source, source_type)
    chunk_docs = semantic_chunk(clean, source_value=source)
    return _insert_chunks(
        source_value=source,
        source_type=resolved_source_type,
        platform=platform,
        operating_system=operating_system,
        version=version,
        docs=chunk_docs,
    )


def ingest_single_url(
    url: str,
    platform: str,
    operating_system: str,
    version: str,
    source_type: str | None = None,
) -> IngestSummary:
    return ingest_bulk_urls(
        [url],
        platform=platform,
        operating_system=operating_system,
        version=version,
        source_type=source_type,
    )


def ingest_bulk_urls(
    urls: List[str],
    platform: str,
    operating_system: str,
    version: str,
    source_type: str | None = None,
) -> IngestSummary:
    summary = IngestSummary(input_count=len(urls))
    stats = IngestionRunStats()
    with SessionLocal() as db:
        def _process(raw_url: str) -> None:
            url = (raw_url or "").strip()
            if not is_valid_url(url):
                stats.skipped_invalid += 1
                logger.warning("Invalid URL skipped: %s", url or "(empty)")
                return
            resolved_source_type = resolve_source_type(url, source_type)

            if _url_exists(db, url, resolved_source_type):
                stats.skipped_duplicates += 1
                return

            markdown = fetch_markdown(url)
            if not markdown.strip():
                stats.failed += 1
                uid, _ = _log_usage(
                    db=db,
                    url=url,
                    source_type=resolved_source_type,
                    platform=platform,
                    operating_system=operating_system,
                    version=version,
                    tokens_used=0,
                    cost_usd=0.0,
                    status="failed",
                )
                summary.uuid = uid
                db.commit()

                return

            _, tokens_used, cost_usd = process_source(
                db=db,
                source=url,
                text=markdown,
                platform=platform,
                operating_system=operating_system,
                version=version,
                source_type=resolved_source_type,
            )
            uid, _ = _log_usage(
                db=db,
                url=url,
                source_type=resolved_source_type,
                platform=platform,
                operating_system=operating_system,
                version=version,
                tokens_used=tokens_used,
                cost_usd=cost_usd,
                status="completed",
            )
            summary.uuid = uid
            db.commit()
            stats.processed += 1
            stats.tokens_used += tokens_used
            stats.cost_usd = round(stats.cost_usd + cost_usd, 8)

        def _on_exception(raw_url: str) -> None:
            url = (raw_url or "").strip()
            resolved_source_type = resolve_source_type(url, source_type)
            source_value = url or "(invalid-url)"
            try:
                db.rollback()
                logger.exception("Tech ingestion failed for source: %s", source_value)
                stats.failed += 1
                uid, _ = _log_usage(
                    db=db,
                    url=source_value,
                    source_type=resolved_source_type,
                    platform=platform,
                    operating_system=operating_system,
                    version=version,
                    tokens_used=0,
                    cost_usd=0.0,
                    status="failed",
                )
                summary.uuid = uid
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
    summary.tokens_used = stats.tokens_used
    summary.cost_usd = stats.cost_usd
    summary.status = derive_ingestion_status(
        processed=summary.processed_sources,
        skipped=summary.skipped_duplicates + summary.skipped_invalid,
        failed=summary.failed_sources,
    )
    return summary


def ingest_excel(
    file_path: str,
    platform: str,
    operating_system: str,
    version: str,
) -> IngestSummary:
    urls = read_excel_urls(file_path, logger, "tech")
    return ingest_bulk_urls(
        urls,
        platform=platform,
        operating_system=operating_system,
        version=version,
    )


def ingest_pdf(
    file_path: str,
    platform: str,
    operating_system: str,
    version: str,
) -> IngestSummary:
    summary = IngestSummary(input_count=1)
    if not os_module.path.exists(file_path):
        summary.failed_sources += 1
        return summary

    source_value = os_module.path.abspath(file_path)
    with SessionLocal() as db:
        try:
            markdown = extract_pdf_markdown(file_path)
            if not markdown.strip():
                summary.failed_sources += 1
                uid, _ = _log_usage(
                    db=db,
                    url=source_value,
                    source_type="pdf",
                    platform=platform,
                    operating_system=operating_system,
                    version=version,
                    tokens_used=0,
                    cost_usd=0.0,
                    status="failed",
                )
                summary.uuid = uid
                db.commit()
                return summary

            _, tokens_used, cost_usd = process_source(
                db=db,
                source=source_value,
                text=markdown,
                platform=platform,
                operating_system=operating_system,
                version=version,
            )
            uid, _ = _log_usage(
                db=db,
                url=source_value,
                source_type="pdf",
                platform=platform,
                operating_system=operating_system,
                version=version,
                tokens_used=tokens_used,
                cost_usd=cost_usd,
                status="completed",
            )
            summary.uuid = uid
            db.commit()
            summary.processed_sources += 1
            summary.tokens_used += tokens_used
            summary.cost_usd = round(summary.cost_usd + cost_usd, 8)
            return summary
        except Exception:
            db.rollback()
            logger.exception("Tech PDF ingestion failed: %s", source_value)
            summary.failed_sources += 1
            uid, _ = _log_usage(
                db=db,
                url=source_value,
                source_type="pdf",
                platform=platform,
                operating_system=operating_system,
                version=version,
                tokens_used=0,
                cost_usd=0.0,
                status="failed",
            )
            summary.uuid = uid
            db.commit()
            return summary
