"""Markdown-aware chunking for RAG ingestion."""

import logging
import re
from typing import Any, Dict, List

from langchain_core.documents import Document
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

from .constants import HEADERS_TO_SPLIT_ON, MAX_HEADER_CHUNK_SIZE

logger = logging.getLogger(__name__)

MARKDOWN_SPLITTER = MarkdownHeaderTextSplitter(headers_to_split_on=HEADERS_TO_SPLIT_ON)
FALLBACK_SPLITTER = RecursiveCharacterTextSplitter(
    chunk_size=1500,
    chunk_overlap=200,
    separators=["\n\n", "\n", ". ", "! ", "? ", " ", ""],
    length_function=len,
)


def clean_text(text: str) -> str:
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
        logger.exception(
            "Markdown header split failed; using single-document fallback (source=%s)",
            (source_value or "")[:200],
        )
        header_docs = [Document(page_content=markdown, metadata={})]

    documents: List[Document] = []
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
            documents.append(Document(page_content=content, metadata=metadata))
            continue

        split_docs = FALLBACK_SPLITTER.split_documents([Document(page_content=content, metadata=metadata)])
        for split_doc in split_docs:
            split_doc.metadata["source"] = source_value
            split_doc.metadata["header"] = header_path
            split_doc.metadata["title"] = next(
                (line.strip() for line in split_doc.page_content.splitlines() if line.strip()),
                "untitled",
            )[:120]
            documents.append(split_doc)
    return documents
