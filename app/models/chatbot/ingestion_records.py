from sqlalchemy import Column, String, Integer, Numeric, ForeignKey, UniqueConstraint, Enum 
from sqlalchemy.orm import relationship, backref

from app.models.base import BaseModel

SOURCE_TYPE_ENUM = Enum("url", "pdf", name="source_type")
INGESTION_STATUS_ENUM = Enum("pending", "completed", "failed", name="ingestion_status")
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


class TechFile(BaseModel):
    """
    File is a basic file model representing a stored document.

    ********************* Model Fields *********************
        PK          - id
        Char        - file (file path or identifier)
    """
    __tablename__ = "chatbot_file"

    file = Column(String(1024), nullable=False)

# -------------------------------
# Ingestion Usage Model
# -------------------------------

class IngestionUsageModel(BaseModel):
    """
    Central ingestion tracking model for ALL sources (PDF, URL, etc.)

    ********************* Model Fields *********************
        PK          - id
        FK          - file_id, platform_id, os_id, version_id
        Char/Text   - url, source_type
        Numeric     - tokens_used, cost_usd
        Char        - status
        Timestamp   - created_at, updated_at
    """

    __tablename__ = "chatbot_techingestion"

   
    file_id = Column(Integer, ForeignKey("chatbot_file.id", ondelete="CASCADE"), nullable=True)

    url = Column(String(1024), nullable=True)

  
    source_type = Column(SOURCE_TYPE_ENUM, nullable=False)

  
    platform_id = Column(Integer, ForeignKey("chatbot_platform.id", ondelete="CASCADE"), nullable=True)
    os_id = Column(Integer, ForeignKey("chatbot_os.id", ondelete="CASCADE"), nullable=False)
    version_id = Column(Integer, ForeignKey("chatbot_version.id", ondelete="CASCADE"), nullable=False)


    tokens_used = Column(Integer, nullable=True)
    cost_usd = Column(Numeric(10, 2), nullable=True)
    size = Column(Integer, default=0, nullable=True)
    total_chars = Column(Integer, default=0, nullable=True)


  
    status = Column(INGESTION_STATUS_ENUM, default="pending", nullable=False)

    file = relationship("TechFile", backref=backref("ingestions", lazy="dynamic"))
    platform = relationship("PlatformModel", backref=backref("related_ingestions", lazy="dynamic"))
    os = relationship("OSModel", backref=backref("related_ingestions", lazy="dynamic"))
    version = relationship("VersionModel", backref=backref("related_ingestions", lazy="dynamic"))


