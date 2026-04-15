from sqlalchemy import Column, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.orm import relationship, backref

from app.models.base import BaseNamedEntity

# -------------------------------
# Platform Model
# -------------------------------

class PlatformModel(BaseNamedEntity):
    """
    PlatformModel stores platform-level classifications
    (e.g., iOS, Android, Web).

    ********************* Model Fields *********************
        PK          - id
        Char        - identity
    """

    __tablename__ = "platforms"

    oses = relationship(
        "OSModel",
        backref=backref("platform", lazy="joined"),
        cascade="all, delete-orphan",
        lazy="dynamic",
    )


# -------------------------------
# OS Model
# -------------------------------

class OSModel(BaseNamedEntity):
    """
    OSModel represents operating systems under a platform.

    ********************* Model Fields *********************
        PK          - id
        FK          - platform_id
        Char        - identity
    """

    __tablename__ = "oses"
    __table_args__ = (
        UniqueConstraint("platform_id", "identity", name="uq_platform_os_identity"),
    )

    platform_id = Column(
        Integer,
        ForeignKey("platforms.id", ondelete="CASCADE"),
        nullable=False,
    )

    versions = relationship(
        "VersionModel",
        backref=backref("os", lazy="joined"),
        cascade="all, delete-orphan",
        lazy="dynamic",
    )


# -------------------------------
# Version Model
# -------------------------------

class VersionModel(BaseNamedEntity):
    """
    VersionModel stores version information for a given OS.

    ********************* Model Fields *********************
        PK          - id
        FK          - os_id
        Char        - identity
    """

    __tablename__ = "versions"
    __table_args__ = (
        UniqueConstraint("os_id", "identity", name="uq_os_version_identity"),
    )

    os_id = Column(
        Integer,
        ForeignKey("oses.id", ondelete="CASCADE"),
        nullable=False,
    )