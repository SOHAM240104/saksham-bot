from sqlalchemy import Column, String, Integer, Numeric, ForeignKey, UniqueConstraint, Enum as SAEnum
from sqlalchemy.orm import relationship, backref

from app.models.base import BaseModel

SOURCE_TYPE_ENUM = SAEnum("url", "pdf", name="source_type")
INGESTION_STATUS_ENUM = SAEnum("pending", "completed", "failed", name="ingestion_status")
URL_MAX_LENGTH = 255


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

    __tablename__ = "chatbot_scamingestion"
    __table_args__ = (
        UniqueConstraint("url", name="uq_chatbot_scam_ingestions_url"),
    )

    url = Column(String(URL_MAX_LENGTH), nullable=True, unique=True)
    source_type = Column(SOURCE_TYPE_ENUM, default="url", nullable=False)
    tokens_used = Column(Integer, nullable=True)
    cost_usd = Column(Numeric(10, 2), nullable=True)

    status = Column(INGESTION_STATUS_ENUM, default="pending", nullable=False)


# -------------------------------
# Ingestion Usage Model
# -------------------------------

class IngestionUsageModel(BaseModel):
    """
    IngestionUsageModel tracks detailed ingestion usage across
    sources with platform and version context.

    ********************* Model Fields *********************
        PK          - id
        FK          - platform_id, os_id, version_id
        Char/Text   - url, source_type
        Numeric     - tokens_used, cost_usd
        Char        - status
    """

    __tablename__ = "chatbot_techingestion"

    # URL-driven ingestions can set url/source_type; other ingestion types may keep them null.
    url = Column(String(URL_MAX_LENGTH), nullable=True, unique=True)
    source_type = Column(SOURCE_TYPE_ENUM, default="url", nullable=False)

    platform_id = Column(Integer, ForeignKey("chatbot_platforms.id", ondelete="CASCADE"), nullable=True)
    os_id = Column(Integer, ForeignKey("chatbot_operating_systems.id", ondelete="CASCADE"), nullable=False)
    version_id = Column(Integer, ForeignKey("chatbot_versions.id", ondelete="CASCADE"), nullable=False)

    platform = relationship("PlatformModel", backref=backref("related_ingestions", lazy="dynamic"))
    os = relationship("OSModel", backref=backref("related_ingestions", lazy="dynamic"))
    version = relationship("VersionModel", backref=backref("related_ingestions", lazy="dynamic"))
    tokens_used = Column(Integer, nullable=True)
    cost_usd = Column(Numeric(10, 2), nullable=True)

    status = Column(INGESTION_STATUS_ENUM, default="pending", nullable=False)