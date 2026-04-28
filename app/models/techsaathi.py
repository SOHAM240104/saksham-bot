from sqlalchemy import Column, ForeignKey, Integer
from sqlalchemy.orm import relationship

from app.models.base import BaseModel
from app.models.config import tech_saathi_language_association


class TechSaathi(BaseModel):
    __tablename__ = "senior_techsaathi"

    user_id = Column(Integer, ForeignKey("access_user.id", ondelete="CASCADE"), nullable=False, unique=True)
    user = relationship("User", back_populates="tech_saathi_profile")
    languages = relationship("LanguageModel", secondary=tech_saathi_language_association, lazy="dynamic")

    def __str__(self):
        return f"{self.user.first_name} [{self.user.is_active}]"

