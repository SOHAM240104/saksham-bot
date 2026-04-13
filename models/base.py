"""Shared SQLAlchemy declarative bases used across model modules."""

from uuid import uuid4

from sqlalchemy import BigInteger, Boolean, Column, DateTime, Float, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID

from config.database import Base


class ModelBase(Base):
    __abstract__ = True

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    uuid = Column(UUID(as_uuid=True), nullable=False, unique=True, default=uuid4)
    created = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    modified = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    is_deleted = Column(Boolean, nullable=False, default=False)


class IngestionMetricsMixin:
    processed = Column(Integer, nullable=False, default=0)
    skipped = Column(Integer, nullable=False, default=0)
    failed = Column(Integer, nullable=False, default=0)
    chunks = Column(Integer, nullable=False, default=0)
    tokens_used = Column(Integer, nullable=False, default=0)
    cost_usd = Column(Float, nullable=False, default=0.0)
    status = Column(String, nullable=False, default="completed")


class NamedEntityBase(ModelBase):
    """Abstract base for entities stored in the `name` column."""

    __abstract__ = True
    identity = Column("name", String, nullable=False)


class IngestionSourceContextBase(ModelBase):
    """Abstract base for source/context dimensions used in ingestion tables."""

    __abstract__ = True
    source = Column(Text, nullable=False)
    source_type = Column(String, nullable=False)
    platform = Column(String, nullable=False)
    os = Column(String, nullable=False)
    version = Column(String, nullable=False)


class IngestionMetricsBase(IngestionMetricsMixin, IngestionSourceContextBase):
    """Abstract base combining ingestion metrics with source/context fields."""

    __abstract__ = True
