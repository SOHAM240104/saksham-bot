from sqlalchemy import Column, String, Text, Integer, Float, ForeignKey, UniqueConstraint, Enum as SAEnum
from sqlalchemy.orm import relationship, backref

from app.models.base import BaseIngestionSourceContext, BaseModel

INGESTION_STATUS_ENUM = SAEnum("not_started", "completed", "failed", name="ingestion_status")


# -------------------------------
# Scam Ingestion Model
# -------------------------------

class ScamIngestionModel(BaseModel):
    """
    ScamIngestionModel tracks ingestion workflow status for scam-specific pipelines.
    This model does NOT depend on platform/OS/version normalization.

    ********************* Model Fields *********************
        PK          - id
        Unique      - url
        Char/Text   - url, source_type
        Numeric     - tokens_used, cost_usd
        Char        - status
    """

    __tablename__ = "scam_ingestions"
    __table_args__ = (
        UniqueConstraint("url", name="uq_scam_ingestions_url"),
    )

    url = Column(Text, nullable=True)
    source_type = Column(String, nullable=False)
    tokens_used = Column(Integer, default=0, nullable=False)
    cost_usd = Column(Float, default=0.0, nullable=False)

    status = Column(INGESTION_STATUS_ENUM, default="completed", nullable=False)


# -------------------------------
# Ingestion Usage Model
# -------------------------------

class IngestionUsageModel(BaseIngestionSourceContext):
    """
    IngestionUsageModel tracks detailed ingestion usage across
    sources with platform and version context.

    ********************* Model Fields *********************
        PK          - id
        FK          - platform_id, os_id, version_id
        Char/Text   - source, source_type
        Numeric     - tokens_used, cost_usd
        Char        - status
    """

    __tablename__ = "ingestion"

    # URL-driven ingestions can set source/source_type; other ingestion types may keep them null.
    url = Column(Text, nullable=True)
    source_type = Column(String, nullable=True)

    platform_id = Column(Integer, ForeignKey("platforms.id", ondelete="SET NULL"), nullable=True)
    os_id = Column(Integer, ForeignKey("oses.id", ondelete="SET NULL"), nullable=True)
    version_id = Column(Integer, ForeignKey("versions.id", ondelete="SET NULL"), nullable=True)

    platform = relationship("PlatformModel", backref=backref("related_ingestions", lazy="dynamic"))
    os = relationship("OSModel", backref=backref("related_ingestions", lazy="dynamic"))
    version = relationship("VersionModel", backref=backref("related_ingestions", lazy="dynamic"))
    tokens_used = Column(Integer, default=0, nullable=False)
    cost_usd = Column(Float, default=0.0, nullable=False)

    status = Column(INGESTION_STATUS_ENUM, default="completed", nullable=False)