from sqlalchemy import Column, String, Text, UniqueConstraint

from models.base import BaseIngestionMetrics, BaseModel, IngestionMetricsMixin


class SourceModel(BaseModel):
    __tablename__ = "sources"
    __table_args__ = (UniqueConstraint("source", "type", name="uq_sources_source_type"),)

    source = Column(Text, nullable=False)
    type = Column(String, nullable=False)
    platform = Column(String, nullable=False)
    os = Column(String, nullable=False)
    version = Column(String, nullable=False)


class ScamIngestionModel(IngestionMetricsMixin, BaseModel):
    """Listing and dedup for dedicated scam pipeline (no platform/os/version)."""

    __tablename__ = "scam_ingestions"
    __table_args__ = (UniqueConstraint("source_key", name="uq_scam_ingestions_source_key"),)

    source_key = Column(Text, nullable=False)
    source_type = Column(String, nullable=False)


class IngestionUsageModel(BaseIngestionMetrics):
    __tablename__ = "ingestion_usage"
