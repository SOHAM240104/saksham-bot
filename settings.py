import os

from dotenv import load_dotenv

load_dotenv()


def get_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


DATABASE_URL = get_env("DATABASE_URL")
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

ADMIN_TOKEN = get_env("ADMIN_TOKEN")

# OpenAI text-embedding-3-* reduced dimensions; must match PGVector embedding_length (fixed in code, not env).
EMBEDDING_DIMENSIONS = 1024

# PGVector collection names (each appears as a row in langchain_pg_collection).
ALLOWED_VECTOR_COLLECTIONS = frozenset({"tech", "scam"})
DEFAULT_VECTOR_COLLECTION = "tech"
SCAM_VECTOR_COLLECTION = "scam"


def normalize_vector_collection(name: str) -> str:
    """Return a valid collection key: lowercase ``tech`` or ``scam``."""
    key = (name or "").strip().lower()
    if key not in ALLOWED_VECTOR_COLLECTIONS:
        raise ValueError(
            f"Invalid vector_collection: {name!r}. Allowed: {sorted(ALLOWED_VECTOR_COLLECTIONS)}"
        )
    return key
