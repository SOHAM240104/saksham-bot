from sqlalchemy import Column, Float, Integer, String, Text, UniqueConstraint

from models.base import SqlRecordBase


class SourceModel(SqlRecordBase):
    __tablename__ = "sources"

    source = Column(Text, nullable=False, unique=True)
    type = Column(String, nullable=False)
    platform = Column(String, nullable=False)
    os = Column(String, nullable=False)
    version = Column(String, nullable=False)


class ScamIngestionModel(SqlRecordBase):
    """Listing and dedup for dedicated scam pipeline (no platform/os/version)."""

    __tablename__ = "scam_ingestions"
    __table_args__ = (UniqueConstraint("source_key", name="uq_scam_ingestions_source_key"),)

    source_key = Column(Text, nullable=False)
    source_type = Column(String, nullable=False)
    processed = Column(Integer, nullable=False, default=0)
    skipped = Column(Integer, nullable=False, default=0)
    failed = Column(Integer, nullable=False, default=0)
    chunks = Column(Integer, nullable=False, default=0)
    tokens_used = Column(Integer, nullable=False, default=0)
    cost_usd = Column(Float, nullable=False, default=0.0)
    status = Column(String, nullable=False, default="completed")


class IngestionUsageModel(SqlRecordBase):
    __tablename__ = "ingestion_usage"

    source = Column(Text, nullable=False)
    source_type = Column(String, nullable=False)
    platform = Column(String, nullable=False)
    os = Column(String, nullable=False)
    version = Column(String, nullable=False)
    processed = Column(Integer, nullable=False, default=0)
    skipped = Column(Integer, nullable=False, default=0)
    failed = Column(Integer, nullable=False, default=0)
    chunks = Column(Integer, nullable=False, default=0)
    tokens_used = Column(Integer, nullable=False, default=0)
    cost_usd = Column(Float, nullable=False, default=0.0)
    status = Column(String, nullable=False, default="completed")
