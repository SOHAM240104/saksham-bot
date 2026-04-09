from uuid import uuid4

from sqlalchemy import BigInteger, Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID

from config.database import Base


class IngestionBaseModel(Base):
    __abstract__ = True

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    uuid = Column(UUID(as_uuid=True), nullable=False, unique=True, default=uuid4)
    created = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    modified = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    is_archived = Column(Boolean, nullable=False, default=False)
    is_deleted = Column(Boolean, nullable=False, default=False)


class SourceModel(IngestionBaseModel):
    __tablename__ = "sources"

    source = Column(Text, nullable=False, unique=True)
    type = Column(String, nullable=False)
    platform = Column(String, nullable=False)
    os = Column(String, nullable=False)
    version = Column(String, nullable=False)


class ChunkModel(IngestionBaseModel):
    __tablename__ = "chunks"

    source_id = Column(BigInteger, ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False, index=True)
    text = Column(Text, nullable=False)
    embedding = Column(JSONB, nullable=False)
    chunk_metadata = Column("metadata", JSONB, nullable=False)


class IngestionUsageModel(IngestionBaseModel):
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
