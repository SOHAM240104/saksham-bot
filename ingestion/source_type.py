"""URL validation and source-type resolution for tech and scam pipelines."""

import logging
from urllib.parse import urlparse

from .constants import ALLOWED_SOURCE_TYPES

logger = logging.getLogger(__name__)


def is_valid_url(url: str) -> bool:
    try:
        parsed = urlparse(url.strip())
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except Exception:
        logger.debug("URL parse failed in is_valid_url: %r", url, exc_info=True)
        return False


def infer_source_type(source: str) -> str:
    return "url" if is_valid_url(source) else "pdf"


def resolve_source_type(source: str, source_type: str | None) -> str:
    if source_type is None:
        return infer_source_type(source)
    normalized = source_type.strip().lower()
    if normalized not in ALLOWED_SOURCE_TYPES:
        err = ValueError(
            f"Invalid source_type: {source_type}. Allowed: {sorted(ALLOWED_SOURCE_TYPES)}"
        )
        logger.warning("%s", err)
        raise err
    return normalized
