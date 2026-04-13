"""Shared SQLAlchemy declarative bases used across model modules."""

from uuid import uuid4

from sqlalchemy import BigInteger, Boolean, Column, DateTime, func
from sqlalchemy.dialects.postgresql import UUID

from config.database import Base


class SqlRecordBase(Base):
    __abstract__ = True

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    uuid = Column(UUID(as_uuid=True), nullable=False, unique=True, default=uuid4)
    created = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    modified = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    is_deleted = Column(Boolean, nullable=False, default=False)
