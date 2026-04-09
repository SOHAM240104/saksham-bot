"""
Standalone ingestion layer for URLs, Excel URL lists, and PDFs.

Features:
- Generic metadata fields: platform, os, version (no hardcoded values)
- Input entry points:
    ingest_single_url(...)
    ingest_bulk_urls(...)
    ingest_excel(...)
    ingest_pdf(...)
- Shared pipeline:
    fetch/extract -> clean -> semantic chunk -> append with SQLAlchemy
- Basic URL dedup: skip if URL already exists in sources table
- Append-only writes for chunks

"""

from __future__ import annotations

import asyncio
import os as os_module
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Tuple
from urllib.parse import urlparse

import pandas as pd
from dotenv import load_dotenv
from langchain_core.documents import Document
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter
from crawl4ai import AsyncWebCrawler, CrawlerRunConfig
from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator
from openai import OpenAI
from sqlalchemy.orm import Session

from config.database import Base, SessionLocal
from models.ingestion_records import ChunkModel, IngestionUsageModel, SourceModel

load_dotenv()

EMBEDDING_MODEL = "text-embedding-3-small"
# OpenAI price for text-embedding-3-small is $0.00002 per 1K tokens.
EMBEDDING_COST_PER_1K_TOKENS_USD = 0.00002

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


def _require_env(name: str) -> str:
    value = os_module.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


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


def _url_exists(db: Session, url: str) -> bool:
    return db.query(SourceModel.id).filter(SourceModel.type == "url", SourceModel.source == url).first() is not None


def _insert_source(
    db: Session,
    source: str,
    source_type: str,
    platform: str,
    os_name: str,
    version: str,
) -> int:
    existing = db.query(SourceModel).filter(SourceModel.source == source).first()
    if existing is not None:
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


def _insert_chunks(db: Session, source_id: int, docs: Iterable[Document]) -> Tuple[int, int, float]:
    docs_list = list(docs)
    if not docs_list:
        return 0, 0, 0.0
    texts = [doc.page_content for doc in docs_list]
    vectors, tokens_used, cost_usd = _embed_texts_with_usage(texts)
    for doc, vector in zip(docs_list, vectors):
        metadata = {
            "title": (doc.metadata or {}).get("title", "untitled"),
            "header": (doc.metadata or {}).get("header", ""),
        }
        db.add(
            ChunkModel(
                source_id=source_id,
                text=doc.page_content,
                embedding=vector,
                metadata=metadata,
            )
        )
    db.commit()
    return len(docs_list), tokens_used, cost_usd


def _embed_texts_with_usage(texts: List[str]) -> Tuple[List[List[float]], int, float]:
    if not texts:
        return [], 0, 0.0
    client = OpenAI(api_key=_require_env("OPENAI_API_KEY"))
    result = client.embeddings.create(model=EMBEDDING_MODEL, input=texts)
    vectors = [item.embedding for item in result.data]
    tokens_used = int(getattr(result.usage, "total_tokens", 0) or 0)
    cost_usd = round((tokens_used / 1000.0) * EMBEDDING_COST_PER_1K_TOKENS_USD, 8)
    return vectors, tokens_used, cost_usd


def _infer_source_type(source: str) -> str:
    return "url" if _is_valid_url(source) else "pdf"


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
) -> None:
    db.add(
        IngestionUsageModel(
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
            estimated_cost=cost_usd,
        )
    )
    db.commit()


def process_source(
    db: Session,
    source: str,
    text: str,
    platform: str,
    os_name: str,
    version: str,
) -> Tuple[int, int, float]:
    clean = _clean_text(text)
    if not clean:
        return 0, 0, 0.0

    source_id = _insert_source(
        db=db,
        source=source,
        source_type=_infer_source_type(source),
        platform=platform,
        os_name=os_name,
        version=version,
    )
    chunks = semantic_chunk(clean, source_value=source)
    return _insert_chunks(db, source_id=source_id, docs=chunks)


def ingest_single_url(url: str, platform: str, os: str, version: str) -> IngestSummary:
    summary = IngestSummary(input_count=1)
    if not _is_valid_url(url):
        summary.skipped_invalid += 1
        return summary

    with SessionLocal() as db:
        _bootstrap_db(db)
        if _url_exists(db, url):
            summary.skipped_duplicates += 1
            _log_usage(
                db=db,
                source=url,
                source_type="url",
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
            return summary

        markdown = fetch_markdown(url)
        if not markdown.strip():
            summary.failed_sources += 1
            _log_usage(
                db=db,
                source=url,
                source_type="url",
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
            return summary

        inserted, tokens_used, cost_usd = process_source(
            db=db,
            source=url,
            text=markdown,
            platform=platform,
            os_name=os,
            version=version,
        )
        summary.processed_sources += 1
        summary.chunks_inserted += inserted
        summary.tokens_used += tokens_used
        summary.cost_usd = round(summary.cost_usd + cost_usd, 8)
        _log_usage(
            db=db,
            source=url,
            source_type="url",
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
    return summary


def ingest_bulk_urls(urls: List[str], platform: str, os: str, version: str) -> IngestSummary:
    summary = IngestSummary(input_count=len(urls))
    with SessionLocal() as db:
        _bootstrap_db(db)
        for raw_url in urls:
            url = (raw_url or "").strip()
            if not _is_valid_url(url):
                summary.skipped_invalid += 1
                _log_usage(
                    db=db,
                    source=url or "(invalid-url)",
                    source_type="url",
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
                continue
            if _url_exists(db, url):
                summary.skipped_duplicates += 1
                _log_usage(
                    db=db,
                    source=url,
                    source_type="url",
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
                continue

            markdown = fetch_markdown(url)
            if not markdown.strip():
                summary.failed_sources += 1
                _log_usage(
                    db=db,
                    source=url,
                    source_type="url",
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
                continue

            inserted, tokens_used, cost_usd = process_source(
                db=db,
                source=url,
                text=markdown,
                platform=platform,
                os_name=os,
                version=version,
            )
            summary.processed_sources += 1
            summary.chunks_inserted += inserted
            summary.tokens_used += tokens_used
            summary.cost_usd = round(summary.cost_usd + cost_usd, 8)
            _log_usage(
                db=db,
                source=url,
                source_type="url",
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
    return summary


def ingest_excel(file_path: str, platform: str, os: str, version: str) -> IngestSummary:
    df = pd.read_excel(file_path)
    normalized = {str(c).strip().lower(): c for c in df.columns}
    # Accept common variants to reduce upload friction.
    url_col = normalized.get("url") or normalized.get("urls")
    if not url_col:
        raise ValueError("Excel must contain a URL column: url / urls")

    urls = [str(v).strip() for v in df[url_col].dropna().tolist() if str(v).strip()]
    return ingest_bulk_urls(urls, platform=platform, os=os, version=version)


def ingest_pdf(file_path: str, platform: str, os: str, version: str) -> IngestSummary:
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
            _log_usage(
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
            return summary

        inserted, tokens_used, cost_usd = process_source(
            db=db,
            source=source_value,
            text=markdown,
            platform=platform,
            os_name=os,
            version=version,
        )
        summary.processed_sources += 1
        summary.chunks_inserted += inserted
        summary.tokens_used += tokens_used
        summary.cost_usd = round(summary.cost_usd + cost_usd, 8)
        _log_usage(
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
    return summary


if __name__ == "__main__":
    # Example usage:
    # Ensure .env contains DATABASE_URL and OPENAI_API_KEY.
    one = ingest_single_url(
        url="https://support.apple.com/en-in/guide/iphone/iph4fd8a0b89/18.0/ios/18.0",
        platform="Apple",
        os="iOS",
        version="18",
    )
    print("single_url:", one)

