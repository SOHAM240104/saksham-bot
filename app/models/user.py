from sqlalchemy import Column, String
from sqlalchemy.orm import relationship

from app.models.base import BaseModel
from app.models.config import USER_TYPE_ENUM


class User(BaseModel):
    """
    User model for the entire application.
    This model holds data other than auth related data.
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

