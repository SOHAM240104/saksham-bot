from sqlalchemy import BigInteger, Column, ForeignKey, UniqueConstraint

from models.base import BaseNamedEntity


class BaseContextModel(BaseNamedEntity):
    __abstract__ = True


class PlatformModel(BaseContextModel):
    __tablename__ = "platforms"


class OSModel(BaseContextModel):
    __tablename__ = "oses"
    __table_args__ = (UniqueConstraint("platform_id", "name", name="uq_platform_os_name"),)

    platform_id = Column(BigInteger, ForeignKey("platforms.id", ondelete="CASCADE"), nullable=False)


class VersionModel(BaseContextModel):
    __tablename__ = "versions"
    __table_args__ = (UniqueConstraint("os_id", "name", name="uq_os_version_name"),)

    os_id = Column(BigInteger, ForeignKey("oses.id", ondelete="CASCADE"), nullable=False)
