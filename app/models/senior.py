from sqlalchemy import Boolean, Column, Date, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import backref, relationship

from app.models.base import BaseModel, BaseNamedEntity
from app.models.config import (
    GENDER_ENUM,
    ONBOARDING_TYPE_ENUM,
    senior_language_association,
    senior_relative_association,
)


class Senior(BaseModel):
    __tablename__ = "senior_senior"

    user_id = Column(Integer, ForeignKey("access_user.id", ondelete="CASCADE"), nullable=False, unique=True)
    initial = Column(String(5), nullable=False)
    dob = Column(Date, nullable=False)
    gender = Column(GENDER_ENUM, nullable=False, default="male")
    onboarding_type = Column(ONBOARDING_TYPE_ENUM, nullable=False, default="self")
    device_id = Column(Integer, ForeignKey("chatbot_platform.id", ondelete="SET NULL"), nullable=True)
    device_model_id = Column(Integer, ForeignKey("administration_devicemodel.id", ondelete="SET NULL"), nullable=True)

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


class LanguageModel(BaseNamedEntity):
    __tablename__ = "administration_language"


class RelativeModel(BaseModel):
    __tablename__ = "senior_relative"

    relationship_name = Column("relationship", String(20), nullable=False)
    user_id = Column(Integer, ForeignKey("access_user.id", ondelete="CASCADE"), nullable=False)
    user = relationship("User", backref=backref("relatives", lazy="dynamic"))

    def __str__(self):
        return f"{self.user.first_name} ({self.relationship_name})"


class DeviceModel(BaseNamedEntity):
    __tablename__ = "administration_devicemodel"
    __table_args__ = (
        UniqueConstraint("identity", "platform_id", name="administration_devicemodel_identity_platform_id_a9615e7f_uniq"),
    )

    platform_id = Column(Integer, ForeignKey("chatbot_platform.id", ondelete="CASCADE"), nullable=False)
    platform = relationship("PlatformModel", backref=backref("device_models", lazy="dynamic"))

    def __str__(self):
        return f"{self.identity} ({self.platform})"

