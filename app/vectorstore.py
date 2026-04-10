"""LangChain PGVector + OpenAI embeddings (collections: tech, scam)."""

from __future__ import annotations

from langchain_openai import OpenAIEmbeddings
from langchain_postgres import PGVector

from settings import DATABASE_URL, DEFAULT_VECTOR_COLLECTION, EMBEDDING_DIMENSIONS, normalize_vector_collection

EMBEDDING_MODEL = "text-embedding-3-small"

_embeddings: OpenAIEmbeddings | None = None
_vector_stores: dict[str, PGVector] = {}


def get_embeddings() -> OpenAIEmbeddings:
    global _embeddings
    if _embeddings is None:
        _embeddings = OpenAIEmbeddings(model=EMBEDDING_MODEL, dimensions=EMBEDDING_DIMENSIONS)
    return _embeddings


def ensure_pgvector_tables_and_collections() -> None:
    """Create pgvector extension and ``langchain_pg_*`` tables (via first store init)."""
    get_vector_store(DEFAULT_VECTOR_COLLECTION)


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
