from sqlalchemy import Column, String, Text, UniqueConstraint

from models.base import IngestionMetricsBase, IngestionMetricsMixin, ModelBase


class SourceModel(ModelBase):
    __tablename__ = "sources"
    __table_args__ = (UniqueConstraint("source", "type", name="uq_sources_source_type"),)

    source = Column(Text, nullable=False)
    type = Column(String, nullable=False)
    platform = Column(String, nullable=False)
    os = Column(String, nullable=False)
    version = Column(String, nullable=False)


class ScamIngestionModel(IngestionMetricsMixin, ModelBase):
    """Listing and dedup for dedicated scam pipeline (no platform/os/version)."""

    __tablename__ = "scam_ingestions"
    __table_args__ = (UniqueConstraint("source_key", name="uq_scam_ingestions_source_key"),)

    source_key = Column(Text, nullable=False)
    source_type = Column(String, nullable=False)


class IngestionUsageModel(IngestionMetricsBase):
    __tablename__ = "ingestion_usage"


class IngestionUsageSummaryModel(IngestionMetricsBase):
    __tablename__ = "ingestion_usage_summary"
    __table_args__ = (
        UniqueConstraint(
            "source",
            "source_type",
            "platform",
            "os",
            "version",
            name="uq_ingestion_usage_summary_source_context",
        ),
    )
