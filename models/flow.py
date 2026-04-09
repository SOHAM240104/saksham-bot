from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, String, UniqueConstraint, func

from config.database import Base


class PlatformModel(Base):
    __tablename__ = "platforms"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False, unique=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class OSModel(Base):
    __tablename__ = "oses"
    __table_args__ = (UniqueConstraint("platform_id", "name", name="uq_platform_os_name"),)

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    platform_id = Column(BigInteger, ForeignKey("platforms.id", ondelete="CASCADE"), nullable=False)
    name = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class VersionModel(Base):
    __tablename__ = "versions"
    __table_args__ = (UniqueConstraint("os_id", "name", name="uq_os_version_name"),)

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    os_id = Column(BigInteger, ForeignKey("oses.id", ondelete="CASCADE"), nullable=False)
    name = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
