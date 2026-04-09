from sqlalchemy import BigInteger, Column, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB

from config.database import Base


class SourceModel(Base):
    __tablename__ = "sources"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    source = Column(Text, nullable=False, unique=True)
    type = Column(String, nullable=False)
    platform = Column(String, nullable=False)
    os = Column(String, nullable=False)
    version = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ChunkModel(Base):
    __tablename__ = "chunks"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    source_id = Column(BigInteger, ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False, index=True)
    text = Column(Text, nullable=False)
    embedding = Column(JSONB, nullable=False)
    metadata = Column(JSONB, nullable=False)


class IngestionUsageModel(Base):
    __tablename__ = "ingestion_usage"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
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
    estimated_cost = Column(Float, nullable=False, default=0.0)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
