"""Ingest URLs, Excel URL lists, and PDFs into PGVector (chunked text + embeddings)."""

from __future__ import annotations

import asyncio
import os as os_module
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Tuple
from urllib.parse import urlparse
from uuid import UUID, uuid4

import pandas as pd
import tiktoken
from langchain_core.documents import Document
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter
from crawl4ai import AsyncWebCrawler, CrawlerRunConfig
from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.vectorstore import get_vector_store
from config.database import Base, SessionLocal
from models.ingestion_records import IngestionUsageModel, SourceModel
from settings import DEFAULT_VECTOR_COLLECTION, SCAM_VECTOR_COLLECTION, normalize_vector_collection

# OpenAI price for text-embedding-3-small is $0.00002 per 1K tokens (used for estimates only).
EMBEDDING_COST_PER_1K_TOKENS_USD = 0.00002
# tiktoken cl100k_base aligns with OpenAI embedding tokenization for cost estimates.
_TIKTOKEN_ENCODING = "cl100k_base"

_crawl4ai_md_generator = DefaultMarkdownGenerator(
    options={
        "ignore_links": False,
        "ignore_images": False,
        "escape_html": False,
        "body_width": 0,
        "skip_internal_links": False,
    }
)
CRAWL4AI_CONFIG = CrawlerRunConfig(markdown_generator=_crawl4ai_md_generator)

HEADERS_TO_SPLIT_ON = [("#", "Header 1"), ("##", "Header 2"), ("###", "Header 3")]
MARKDOWN_SPLITTER = MarkdownHeaderTextSplitter(headers_to_split_on=HEADERS_TO_SPLIT_ON)
FALLBACK_SPLITTER = RecursiveCharacterTextSplitter(
    chunk_size=1500,
    chunk_overlap=200,
    separators=["\n\n", "\n", ". ", "! ", "? ", " ", ""],
    length_function=len,
)
MAX_HEADER_CHUNK_SIZE = 1500
ALLOWED_SOURCE_TYPES = {"url", "pdf"}


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


def _clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _header_path(metadata: Dict[str, Any]) -> str:
    parts = []
    for key in ("Header 1", "Header 2", "Header 3"):
        value = metadata.get(key)
        if value:
            parts.append(str(value).strip())
    return " > ".join(parts) if parts else ""


def semantic_chunk(markdown: str, source_value: str) -> List[Document]:
    if not markdown or not markdown.strip():
        return []

    try:
        header_docs = MARKDOWN_SPLITTER.split_text(markdown)
    except Exception:
        header_docs = [Document(page_content=markdown, metadata={})]

    chunks: List[Document] = []
    for doc in header_docs:
        content = (doc.page_content or "").strip()
        if len(content) < 50:
            continue

        metadata = dict(doc.metadata or {})
        header_path = _header_path(metadata)

        if len(content) <= MAX_HEADER_CHUNK_SIZE:
            metadata.update(
                {
                    "source": source_value,
                    "header": header_path,
                    "title": next((line.strip() for line in content.splitlines() if line.strip()), "untitled")[:120],
                }
            )
            chunks.append(Document(page_content=content, metadata=metadata))
            continue

        split_docs = FALLBACK_SPLITTER.split_documents([Document(page_content=content, metadata=metadata)])
        for split_doc in split_docs:
            split_doc.metadata["source"] = source_value
            split_doc.metadata["header"] = header_path
            split_doc.metadata["title"] = next(
                (line.strip() for line in split_doc.page_content.splitlines() if line.strip()),
                "untitled",
            )[:120]
            chunks.append(split_doc)
    return chunks


def _is_valid_url(url: str) -> bool:
    try:
        parsed = urlparse(url.strip())
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except Exception:
        return False


async def _fetch_one_url(url: str) -> str:
    try:
        async with AsyncWebCrawler() as crawler:
            result = await crawler.arun(url, config=CRAWL4AI_CONFIG)
        if not result or not getattr(result, "success", False):
            return ""
        md = getattr(result, "markdown", None)
        if md is None:
            return ""
        if hasattr(md, "raw_markdown") and md.raw_markdown:
            return md.raw_markdown or ""
        if isinstance(md, str):
            return md
        return ""
    except Exception:
        return ""


def fetch_markdown(url: str) -> str:
    return asyncio.run(_fetch_one_url(url))


def _extract_pdf_markdown_docling(file_path: str) -> str:
    from docling.document_converter import DocumentConverter

    converter = DocumentConverter()
    result = converter.convert(file_path)
    doc = result.document

    if hasattr(doc, "export_to_markdown"):
        return doc.export_to_markdown() or ""
    if hasattr(doc, "export_to_text"):
        return doc.export_to_text() or ""
    return str(doc)


def _bootstrap_db(db: Session) -> None:
    Base.metadata.create_all(bind=db.get_bind())


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


def _estimate_embedding_tokens_and_cost(texts: List[str]) -> Tuple[int, float]:
    """Estimate token count and USD cost for embedding inputs (no API usage object from LangChain)."""
    if not texts:
        return 0, 0.0
    enc = tiktoken.get_encoding(_TIKTOKEN_ENCODING)
    tokens_used = sum(len(enc.encode(t)) for t in texts)
    cost_usd = round((tokens_used / 1000.0) * EMBEDDING_COST_PER_1K_TOKENS_USD, 8)
    return tokens_used, cost_usd


def _insert_chunks(
    db: Session,
    source_id: int,
    source_value: str,
    source_type: str,
    platform: str,
    os_name: str,
    version: str,
    docs: Iterable[Document],
    vector_collection: str = DEFAULT_VECTOR_COLLECTION,
) -> Tuple[int, int, float]:
    docs_list = list(docs)
    if not docs_list:
        return 0, 0, 0.0

    collection_key = normalize_vector_collection(vector_collection)
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
    tokens_used, cost_usd = _estimate_embedding_tokens_and_cost(texts)

    vector_store = get_vector_store(collection_key)
    vector_store.add_documents(documents=to_store, ids=ids)

    db.commit()
    return len(docs_list), tokens_used, cost_usd


def _normalize_scam_source_key(url: str) -> str:
    return (url or "").strip()


def _scam_url_already_ingested(db: Session, url: str) -> bool:
    """True if the scam collection already has at least one chunk for this source key (metadata.url)."""
    key = _normalize_scam_source_key(url)
    if not key:
        return False
    cname = normalize_vector_collection(SCAM_VECTOR_COLLECTION)
    row = db.execute(
        text(
            """
            SELECT 1
            FROM langchain_pg_embedding e
            INNER JOIN langchain_pg_collection c ON e.collection_id = c.uuid
            WHERE c.name = :cname
              AND e.cmetadata->>'url' = :url
            LIMIT 1
            """
        ),
        {"cname": cname, "url": key},
    ).fetchone()
    return row is not None


def _insert_scam_chunks(
    url: str,
    docs: Iterable[Document],
) -> Tuple[int, int, float]:
    """Store chunks in the scam collection with metadata ``{url}`` only (ids are UUID strings)."""
    docs_list = list(docs)
    if not docs_list:
        return 0, 0, 0.0

    norm_url = _normalize_scam_source_key(url)
    collection_key = normalize_vector_collection(SCAM_VECTOR_COLLECTION)
    ids = [str(uuid4()) for _ in docs_list]
    to_store = [
        Document(page_content=doc.page_content, metadata={"url": norm_url}) for doc in docs_list
    ]

    texts = [d.page_content for d in to_store]
    tokens_used, cost_usd = _estimate_embedding_tokens_and_cost(texts)

    vector_store = get_vector_store(collection_key)
    vector_store.add_documents(documents=to_store, ids=ids)
    return len(docs_list), tokens_used, cost_usd


def process_scam_source(db: Session, url: str, text: str) -> Tuple[int, int, float]:
    """Chunk and embed into the scam collection only (no sources / usage rows; metadata is url-only)."""
    clean = _clean_text(text)
    if not clean:
        return 0, 0, 0.0

    chunk_docs = semantic_chunk(clean, source_value=url)
    return _insert_scam_chunks(url, chunk_docs)


def _infer_source_type(source: str) -> str:
    return "url" if _is_valid_url(source) else "pdf"


def _resolve_source_type(source: str, source_type: str | None) -> str:
    if source_type is None:
        return _infer_source_type(source)
    normalized = source_type.strip().lower()
    if normalized not in ALLOWED_SOURCE_TYPES:
        raise ValueError(f"Invalid source_type: {source_type}. Allowed: {sorted(ALLOWED_SOURCE_TYPES)}")
    return normalized


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
    vector_collection: str = DEFAULT_VECTOR_COLLECTION,
    *,
    source_storage_key: str | None = None,
) -> Tuple[int, int, float]:
    clean = _clean_text(text)
    if not clean:
        return 0, 0, 0.0

    resolved_source_type = _resolve_source_type(source, source_type)
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
        vector_collection=vector_collection,
    )


def ingest_single_url(
    url: str,
    platform: str,
    os: str,
    version: str,
    source_type: str | None = None,
    vector_collection: str = DEFAULT_VECTOR_COLLECTION,
    *,
    source_storage_key: str | None = None,
) -> IngestSummary:
    summary = IngestSummary(input_count=1)
    resolved_source_type = _resolve_source_type(url, source_type)
    dedup_key = source_storage_key if source_storage_key is not None else url
    if not _is_valid_url(url):
        summary.skipped_invalid += 1
        with SessionLocal() as db:
            _bootstrap_db(db)
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
        _bootstrap_db(db)
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
            vector_collection=vector_collection,
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
    vector_collection: str = DEFAULT_VECTOR_COLLECTION,
    *,
    source_storage_key_fn: Callable[[str], str] | None = None,
) -> IngestSummary:
    summary = IngestSummary(input_count=len(urls))
    resolved_default_source_type = _resolve_source_type("https://example.com", source_type)

    def _row_key(u: str) -> str:
        return source_storage_key_fn(u) if source_storage_key_fn is not None else u

    with SessionLocal() as db:
        _bootstrap_db(db)
        for raw_url in urls:
            url = (raw_url or "").strip()
            if not _is_valid_url(url):
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
                vector_collection=vector_collection,
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
    vector_collection: str = DEFAULT_VECTOR_COLLECTION,
    *,
    source_storage_key_fn: Callable[[str], str] | None = None,
) -> IngestSummary:
    df = pd.read_excel(file_path)
    normalized = {str(c).strip().lower(): c for c in df.columns}
    # Accept common variants to reduce upload friction.
    url_col = normalized.get("url") or normalized.get("urls")
    if not url_col:
        raise ValueError("Excel must contain a URL column: url / urls")

    urls = [str(v).strip() for v in df[url_col].dropna().tolist() if str(v).strip()]
    return ingest_bulk_urls(
        urls,
        platform=platform,
        os=os,
        version=version,
        vector_collection=vector_collection,
        source_storage_key_fn=source_storage_key_fn,
    )


def ingest_pdf(
    file_path: str,
    platform: str,
    os: str,
    version: str,
    vector_collection: str = DEFAULT_VECTOR_COLLECTION,
    *,
    source_storage_key: str | None = None,
) -> IngestSummary:
    summary = IngestSummary(input_count=1)
    if not os_module.path.exists(file_path):
        summary.failed_sources += 1
        return summary

    source_value = os_module.path.abspath(file_path)

    with SessionLocal() as db:
        _bootstrap_db(db)
        markdown = _extract_pdf_markdown_docling(file_path)
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
            vector_collection=vector_collection,
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


def ingest_scam_single_url(url: str, source_type: str | None = None) -> IngestSummary:
    """Ingest one URL into the scam collection only: ``metadata.url``, chunk ids, embeddings (no sources/usage)."""
    summary = IngestSummary(input_count=1)
    _resolve_source_type(url, source_type)

    if not _is_valid_url(url):
        summary.skipped_invalid += 1
        summary.status = "completed"
        return summary

    with SessionLocal() as db:
        _bootstrap_db(db)
        if _scam_url_already_ingested(db, url):
            summary.skipped_duplicates += 1
            summary.status = "completed"
            return summary

        markdown = fetch_markdown(url)
        if not markdown.strip():
            summary.failed_sources += 1
            summary.status = "completed"
            return summary

        inserted, tokens_used, cost_usd = process_scam_source(db, url, markdown)
        summary.processed_sources += 1
        summary.chunks_inserted += inserted
        summary.tokens_used += tokens_used
        summary.cost_usd = round(summary.cost_usd + cost_usd, 8)
        summary.status = "completed"
    return summary


def ingest_scam_bulk_urls(urls: List[str], source_type: str | None = None) -> IngestSummary:
    _resolve_source_type("https://example.com", source_type)
    summary = IngestSummary(input_count=len(urls))

    with SessionLocal() as db:
        _bootstrap_db(db)
        for raw_url in urls:
            url = (raw_url or "").strip()
            if not _is_valid_url(url):
                summary.skipped_invalid += 1
                continue
            if _scam_url_already_ingested(db, url):
                summary.skipped_duplicates += 1
                continue
            markdown = fetch_markdown(url)
            if not markdown.strip():
                summary.failed_sources += 1
                continue
            inserted, tokens_used, cost_usd = process_scam_source(db, url, markdown)
            summary.processed_sources += 1
            summary.chunks_inserted += inserted
            summary.tokens_used += tokens_used
            summary.cost_usd = round(summary.cost_usd + cost_usd, 8)
    summary.status = "completed"
    return summary


def ingest_scam_excel(file_path: str) -> IngestSummary:
    df = pd.read_excel(file_path)
    normalized = {str(c).strip().lower(): c for c in df.columns}
    url_col = normalized.get("url") or normalized.get("urls")
    if not url_col:
        raise ValueError("Excel must contain a URL column: url / urls")

    urls = [str(v).strip() for v in df[url_col].dropna().tolist() if str(v).strip()]
    return ingest_scam_bulk_urls(urls)


def ingest_scam_pdf(file_path: str) -> IngestSummary:
    summary = IngestSummary(input_count=1)
    if not os_module.path.exists(file_path):
        summary.failed_sources += 1
        summary.status = "completed"
        return summary

    source_key = os_module.path.abspath(file_path)

    with SessionLocal() as db:
        _bootstrap_db(db)
        if _scam_url_already_ingested(db, source_key):
            summary.skipped_duplicates += 1
            summary.status = "completed"
            return summary

        markdown = _extract_pdf_markdown_docling(file_path)
        if not markdown.strip():
            summary.failed_sources += 1
            summary.status = "completed"
            return summary

        inserted, tokens_used, cost_usd = process_scam_source(db, source_key, markdown)
        summary.processed_sources += 1
        summary.chunks_inserted += inserted
        summary.tokens_used += tokens_used
        summary.cost_usd = round(summary.cost_usd + cost_usd, 8)
        summary.status = "completed"
    return summary


if __name__ == "__main__":
    # Example usage:
    # Ensure .env contains DATABASE_URL and OPENAI_API_KEY.
    one = ingest_single_url(
        url="https://support.apple.com/en-in/guide/iphone/iphd6288a67f/26/ios/26",
        platform="Apple",
        os="iOS",
        version="26",
        vector_collection="tech",
    )
    print("single_url:", one)

