# base_ingestion.py

import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, DateTime, Boolean, Integer, String, ForeignKey, Enum, Table, Date, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship, declared_attr,backref,declarative_base


Base = declarative_base()


class BaseModel(Base):
    """
    Contains the last modified and the created fields, basically
    the base model for the entire app.

    ********************* Model Fields *********************
        PK          - id
        Unique      - uuid
        FK          - created_by, modified_by, deleted_by
        Datetime    - created, modified, deleted
        Boolean     - is_deleted
    """
    __abstract__ = True

    id = Column(Integer, primary_key=True, autoincrement=True)
    uuid = Column(UUID(as_uuid=True), default=uuid.uuid4, unique=True, nullable=False)
    
    created = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    modified = Column(DateTime(timezone=True),
                    default=lambda: datetime.now(timezone.utc),
                    onupdate=lambda: datetime.now(timezone.utc))
    deleted = Column(DateTime, nullable=True)

    created_by_id = Column(Integer, ForeignKey("access_user.id"), nullable=True)
    updated_by_id = Column(Integer, ForeignKey("access_user.id"), nullable=True)
    deleted_by_id = Column(Integer, ForeignKey("access_user.id"), nullable=True)

    @declared_attr
    def created_by(cls):
        return relationship("User", foreign_keys=[cls.created_by_id])

    @declared_attr
    def updated_by(cls):
        return relationship("User", foreign_keys=[cls.updated_by_id])

    @declared_attr
    def deleted_by(cls):
        return relationship("User", foreign_keys=[cls.deleted_by_id])

    is_active = Column(Boolean, default=True)
    is_deleted = Column(Boolean, default=False)


# -------------------------------
# Identity Entity (converted)
# -------------------------------

class BaseNamedEntity(BaseModel):
    """
    Abstract entity with identity field.
    Used for normalized identity tables.
    """

    __abstract__ = True

    identity = Column(String, nullable=False, index=True, unique=True)

# -------------------------------
# Access Enums
# -------------------------------

USER_TYPE_ENUM = Enum("user", "senior", "tech_saathi", name="user_type")
GENDER_ENUM = Enum("male", "female", "other", name="gender")
ONBOARDING_TYPE_ENUM = Enum("self", "assisted", name="onboarding_type")


# -------------------------------
# Association Tables
# -------------------------------

senior_language_association = Table(
    "senior_senior_language",
    Base.metadata,
    Column("senior_id", Integer, ForeignKey("senior_senior.id", ondelete="CASCADE"), primary_key=True),
    Column("language_id", Integer, ForeignKey("administration_language.id", ondelete="CASCADE"), primary_key=True),
)

senior_relative_association = Table(
    "senior_senior_relation",
    Base.metadata,
    Column("senior_id", Integer, ForeignKey("senior_senior.id", ondelete="CASCADE"), primary_key=True),
    Column("relative_id", Integer, ForeignKey("senior_relative.id", ondelete="CASCADE"), primary_key=True),
)

tech_saathi_language_association = Table(
    "senior_techsaathi_language",
    Base.metadata,
    Column("techsaathi_id", Integer, ForeignKey("senior_techsaathi.id", ondelete="CASCADE"), primary_key=True),
    Column("language_id", Integer, ForeignKey("administration_language.id", ondelete="CASCADE"), primary_key=True),
)


# -------------------------------
# User Model
# -------------------------------

class User(BaseModel):
    """
    User model for the entire application.
    This model holds data other than auth related data.

    ********************* Model Fields *********************
        PK          - id
        Unique      - phone_number
        Char        - email, password, user_type, first_name, last_name
    """

    __tablename__ = "access_user"

    phone_number = Column(String(20), unique=True, nullable=False, index=True)
    email = Column(String(255), nullable=True)
    password = Column(String(255), nullable=True)

    user_type = Column(USER_TYPE_ENUM, nullable=False, default="user")

    first_name = Column(String(255), nullable=True)
    last_name = Column(String(255), nullable=True)

    senior_profile = relationship(
        "Senior",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
    )
    tech_saathi_profile = relationship(
        "TechSaathi",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
    )

    def __str__(self):
        return str(self.phone_number)


# -------------------------------
# Senior Model
# -------------------------------

class Senior(BaseModel):
    __tablename__ = "senior_senior"

    user_id = Column(Integer, ForeignKey("access_user.id", ondelete="CASCADE"), nullable=False, unique=True)

    initial = Column(String(5), nullable=False)
    dob = Column(Date, nullable=False)

    gender = Column(GENDER_ENUM, nullable=False, default="male")

    onboarding_type = Column(ONBOARDING_TYPE_ENUM, nullable=False, default="self")

    device_id = Column(Integer, ForeignKey("chatbot_platform.id", ondelete="SET NULL"), nullable=True)
    device_model_id = Column(Integer, ForeignKey("administration_devicemodel.id", ondelete="SET NULL"), nullable=True)

    # communication preferences
    zoom_call = Column(Boolean, default=False, nullable=False)
    whatsapp_call = Column(Boolean, default=False, nullable=False)
    whatsapp_msg = Column(Boolean, default=False, nullable=False)
    updates = Column(Boolean, default=False, nullable=False)

    user = relationship("User", back_populates="senior_profile")
    device = relationship("PlatformModel", backref=backref("seniors", lazy="dynamic"))
    device_model = relationship("DeviceModel", backref=backref("seniors", lazy="dynamic"))

    languages = relationship("LanguageModel", secondary=senior_language_association)
    relatives = relationship("RelativeModel", secondary=senior_relative_association, lazy="dynamic")

    def __str__(self):
        return f"{self.user.first_name} ({self.user.phone_number})"


# -------------------------------
# TechSaathi Model
# -------------------------------

class TechSaathi(BaseModel):
    __tablename__ = "senior_techsaathi"

    user_id = Column(Integer, ForeignKey("access_user.id", ondelete="CASCADE"), nullable=False, unique=True)

    user = relationship("User", back_populates="tech_saathi_profile")
    languages = relationship("LanguageModel", secondary=tech_saathi_language_association, lazy="dynamic")

    def __str__(self):
        return f"{self.user.first_name} [{self.user.is_active}]"


# -------------------------------
# Language Model
# -------------------------------

class LanguageModel(BaseNamedEntity):
    __tablename__ = "administration_language"


# -------------------------------
# Relative Model
# -------------------------------

class RelativeModel(BaseModel):
    __tablename__ = "senior_relative"

    relationship_name = Column("relationship", String(255), nullable=False)
    user_id = Column(Integer, ForeignKey("access_user.id", ondelete="CASCADE"), nullable=False)
    user = relationship("User", backref=backref("relatives", lazy="dynamic"))


# -------------------------------
# Device Model
# -------------------------------

class DeviceModel(BaseNamedEntity):
    __tablename__ = "administration_devicemodel"
    __table_args__ = (
        UniqueConstraint("identity", "platform_id", name="administration_devicemodel_identity_platform_id_a9615e7f_uniq"),
    )

    platform_id = Column(Integer, ForeignKey("chatbot_platform.id", ondelete="CASCADE"), nullable=False)
    platform = relationship("PlatformModel", backref=backref("device_models", lazy="dynamic"))
