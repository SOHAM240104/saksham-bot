import os

from dotenv import load_dotenv

load_dotenv()


def get_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


ADMIN_TOKEN = get_env("ADMIN_TOKEN")
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "")
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_REGION_NAME = os.getenv("AWS_REGION_NAME")
TECH_S3_BUCKET = os.getenv("TECH_S3_BUCKET", "")
# OpenAI text-embedding-3-large with dimensions=1024 only (must match PGVector embedding_length everywhere).
# This project does not support other sizes; changing requires coordinated code + DB / re-ingest.
EMBEDDING_MODEL = "text-embedding-3-large"
EMBEDDING_DIMENSIONS = 1024
# PGVector: two separate collections — tech (full context metadata) vs scam (minimal metadata).
TECH_VECTOR_COLLECTION = "tech"
SCAM_VECTOR_COLLECTION = "scam_kb"

# Optional scam-support bank helpline deep link (empty = omit from prompts).
BANK_HELPLINE_URL = os.getenv("BANK_HELPLINE_URL", "").strip()

# Valid collection names for get_vector_store (tech train uses only tech; scam pipeline uses SCAM_VECTOR_COLLECTION).
ALLOWED_PGVECTOR_COLLECTIONS = frozenset({TECH_VECTOR_COLLECTION, SCAM_VECTOR_COLLECTION})

def normalize_vector_collection(name: str) -> str:
    """Validate PGVector collection name: ``tech`` (contextual RAG) or configured scam collection (e.g. ``scam_kb``)."""
    key = (name or "").strip().lower()
    if key not in ALLOWED_PGVECTOR_COLLECTIONS:
        raise ValueError(
            f"Invalid vector_collection: {name!r}. Allowed: {sorted(ALLOWED_PGVECTOR_COLLECTIONS)}"
        )
    return key
