from sqlalchemy import Column, Enum, ForeignKey, Integer, Table

from app.models.base import Base


USER_TYPE_ENUM = Enum("user", "senior", "tech_saathi", name="user_type")
GENDER_ENUM = Enum("male", "female", "other", name="gender")
ONBOARDING_TYPE_ENUM = Enum("self", "assisted", name="onboarding_type")


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

