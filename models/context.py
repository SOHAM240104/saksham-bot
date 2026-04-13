from sqlalchemy import BigInteger, Column, ForeignKey, String, UniqueConstraint

from models.base import SqlRecordBase


class ContextBaseModel(SqlRecordBase):
    __abstract__ = True

    identity = Column("name", String, nullable=False)


class PlatformModel(ContextBaseModel):
    __tablename__ = "platforms"


class OSModel(ContextBaseModel):
    __tablename__ = "oses"
    __table_args__ = (UniqueConstraint("platform_id", "name", name="uq_platform_os_name"),)

    platform_id = Column(BigInteger, ForeignKey("platforms.id", ondelete="CASCADE"), nullable=False)


class VersionModel(ContextBaseModel):
    __tablename__ = "versions"
    __table_args__ = (UniqueConstraint("os_id", "name", name="uq_os_version_name"),)

    os_id = Column(BigInteger, ForeignKey("oses.id", ondelete="CASCADE"), nullable=False)
