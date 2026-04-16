"""Shared constants for ingestion pipelines."""

ALLOWED_SOURCE_TYPES = frozenset({"url", "pdf"})

EMBEDDING_COST_PER_1K_TOKENS_USD = 0.00013
TIKTOKEN_ENCODING = "cl100k_base"

HEADERS_TO_SPLIT_ON = [("#", "Header 1"), ("##", "Header 2"), ("###", "Header 3")]
MAX_HEADER_CHUNK_SIZE = 1500
