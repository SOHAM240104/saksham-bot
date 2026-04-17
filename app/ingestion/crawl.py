"""Fetch page markdown (crawl4ai) and PDF text (docling)."""

import asyncio
import logging

from crawl4ai import AsyncWebCrawler, CrawlerRunConfig

logger = logging.getLogger(__name__)
from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator

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


async def _fetch_one_url(url: str) -> str:
    try:
        async with AsyncWebCrawler() as crawler:
            result = await asyncio.wait_for(
                crawler.arun(url, config=CRAWL4AI_CONFIG),
                timeout=15
            )

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

    except asyncio.TimeoutError:
        logger.warning("Timeout → skipping URL: %s", url)
        return ""

    except Exception:
        logger.warning("Failed → skipping URL: %s", url)
        return ""


def fetch_markdown(url: str) -> str:
    return asyncio.run(_fetch_one_url(url))


def extract_pdf_markdown(file_path: str) -> str:
    try:
        from docling.document_converter import DocumentConverter

        converter = DocumentConverter()
        result = converter.convert(file_path)
        doc = result.document

        if hasattr(doc, "export_to_markdown"):
            return doc.export_to_markdown() or ""
        if hasattr(doc, "export_to_text"):
            return doc.export_to_text() or ""
        return str(doc)
    except Exception:
        logger.exception("Error during PDF extraction: %s", file_path)
        raise
