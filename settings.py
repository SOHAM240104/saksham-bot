import os

from dotenv import load_dotenv

load_dotenv()


def get_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def get_env_optional(name: str, default: str) -> str:
    value = os.getenv(name, "").strip()
    return value if value else default


DATABASE_URL = get_env("DATABASE_URL")
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

ADMIN_TOKEN = get_env("ADMIN_TOKEN")

# OpenAI text-embedding-3-* reduced dimensions; must match PGVector embedding_length (fixed in code, not env).
EMBEDDING_DIMENSIONS = 1024

# PGVector: two separate collections — tech (full context metadata) vs scam (minimal metadata).
TECH_VECTOR_COLLECTION = "tech"
SCAM_VECTOR_COLLECTION = get_env_optional("SCAM_VECTOR_COLLECTION", "scam_kb").lower()

# Valid collection names for get_vector_store (tech train uses only tech; scam pipeline uses SCAM_VECTOR_COLLECTION).
ALLOWED_PGVECTOR_COLLECTIONS = frozenset({TECH_VECTOR_COLLECTION, SCAM_VECTOR_COLLECTION})

# Back-compat alias for ingestion defaults (tech only).
DEFAULT_VECTOR_COLLECTION = TECH_VECTOR_COLLECTION


def normalize_vector_collection(name: str) -> str:
    """Validate PGVector collection name: ``tech`` (contextual RAG) or configured scam collection (e.g. ``scam_kb``)."""
    key = (name or "").strip().lower()
    if key not in ALLOWED_PGVECTOR_COLLECTIONS:
        raise ValueError(
            f"Invalid vector_collection: {name!r}. Allowed: {sorted(ALLOWED_PGVECTOR_COLLECTIONS)}"
        )
    return key
