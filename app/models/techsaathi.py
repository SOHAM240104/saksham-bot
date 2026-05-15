from sqlalchemy import Column, ForeignKey, Integer, Table, Date
from sqlalchemy.orm import relationship
from app.models.config import GENDER_ENUM

from app.models.base import Base, BaseModel

tech_saathi_language_association = Table(
    "tech_saathi_techsaathi_language",
    Base.metadata,
    Column("techsaathi_id", Integer, ForeignKey("tech_saathi_techsaathi.id", ondelete="CASCADE"), primary_key=True),
    Column("language_id", Integer, ForeignKey("administration_language.id", ondelete="CASCADE"), primary_key=True),
)


class TechSaathi(BaseModel):
    __tablename__ = "tech_saathi_techsaathi"

    user_id = Column(Integer, ForeignKey("access_user.id", ondelete="CASCADE"), nullable=False, unique=True)
    language = relationship("Language", secondary=tech_saathi_language_association, lazy="dynamic")
    dob = Column(Date, nullable=False)
    gender = Column(GENDER_ENUM, nullable=False, default="male")
    assigned_count=Column(Integer, default=0)


    def __str__(self):
        return f"{self.user.first_name} [{self.user.is_active}]"

