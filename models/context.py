from uuid import uuid4

from sqlalchemy import BigInteger, Boolean, Column, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID

from config.database import Base


class ContextBaseModel(Base):
    __abstract__ = True

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    uuid = Column(UUID(as_uuid=True), nullable=False, unique=True, default=uuid4)
    name = Column(String, nullable=False)
    created = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    modified = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    is_archived = Column(Boolean, nullable=False, default=False)
    is_deleted = Column(Boolean, nullable=False, default=False)


class PlatformModel(ContextBaseModel):
    __tablename__ = "platforms"

    name = Column(String, nullable=False, unique=True)


class OSModel(ContextBaseModel):
    __tablename__ = "oses"
    __table_args__ = (UniqueConstraint("platform_id", "name", name="uq_platform_os_name"),)

    platform_id = Column(BigInteger, ForeignKey("platforms.id", ondelete="CASCADE"), nullable=False)


class VersionModel(ContextBaseModel):
    __tablename__ = "versions"
    __table_args__ = (UniqueConstraint("os_id", "name", name="uq_os_version_name"),)

    os_id = Column(BigInteger, ForeignKey("oses.id", ondelete="CASCADE"), nullable=False)
