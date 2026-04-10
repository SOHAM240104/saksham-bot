"""LangChain PGVector + OpenAI embeddings (separate ``tech`` and scam collections)."""

from __future__ import annotations

from langchain_openai import OpenAIEmbeddings
from langchain_postgres import PGVector

from settings import (
    DATABASE_URL,
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL,
    SCAM_VECTOR_COLLECTION,
    TECH_VECTOR_COLLECTION,
    normalize_vector_collection,
)

_embeddings: OpenAIEmbeddings | None = None
_vector_stores: dict[str, PGVector] = {}


def get_embeddings() -> OpenAIEmbeddings:
    global _embeddings
    if _embeddings is None:
        _embeddings = OpenAIEmbeddings(model=EMBEDDING_MODEL, dimensions=EMBEDDING_DIMENSIONS)
    return _embeddings


def ensure_pgvector_tables_and_collections() -> None:
    """Create pgvector extension and ``langchain_pg_*`` tables; touch both default collections."""
    get_vector_store(TECH_VECTOR_COLLECTION)
    get_vector_store(SCAM_VECTOR_COLLECTION)


def get_vector_store(collection_name: str) -> PGVector:
    name = normalize_vector_collection(collection_name)
    if name not in _vector_stores:
        _vector_stores[name] = PGVector(
            embeddings=get_embeddings(),
            collection_name=name,
            connection=DATABASE_URL,
            embedding_length=EMBEDDING_DIMENSIONS,
            use_jsonb=True,
            create_extension=True,
            async_mode=False,
        )
    return _vector_stores[name]
