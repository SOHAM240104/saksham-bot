-- Dedicated scam ingestion listing (separate from sources / ingestion_usage).
-- Run once against your Postgres DB if not using create_all.

CREATE TABLE IF NOT EXISTS scam_ingestions (
    id BIGSERIAL PRIMARY KEY,
    uuid UUID NOT NULL UNIQUE,
    source_key TEXT NOT NULL,
    source_type VARCHAR NOT NULL,
    processed INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    chunks INTEGER NOT NULL DEFAULT 0,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    status VARCHAR NOT NULL DEFAULT 'completed',
    created TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_archived BOOLEAN NOT NULL DEFAULT FALSE,
    is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
    CONSTRAINT uq_scam_ingestions_source_key UNIQUE (source_key)
);

CREATE INDEX IF NOT EXISTS ix_scam_ingestions_created ON scam_ingestions (created DESC);
