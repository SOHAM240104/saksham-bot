from sqlalchemy import Column, String, Boolean,DateTime, Integer, ForeignKey
from datetime import datetime
from sqlalchemy.orm import backref, relationship

from app.models.base import BaseModel, BaseNamedEntity
from app.models.config import USER_TYPE_ENUM


class UserRole(BaseNamedEntity):
    __tablename__ = "access_userrole"


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
    is_superuser = Column(Boolean, default=False)
    is_staff = Column(Boolean, default=False)
    date_joined = Column(DateTime, default=datetime.utcnow)
    is_active = Column(Boolean, default=True)
    user_role_id = Column(
    Integer,
    ForeignKey("access_userrole.id", ondelete="SET NULL"),
    nullable=True,)
    user_role = relationship("UserRole", backref="users", foreign_keys="User.user_role_id")
 
   


    senior_profile = relationship(
    "Senior",
    backref=backref("user", uselist=False),
    foreign_keys="Senior.user_id",   
    uselist=False,
    cascade="all, delete-orphan",
    )
    tech_saathi_profile = relationship(
        "TechSaathi",
        backref=backref("user", uselist=False),
        foreign_keys="TechSaathi.user_id",
        uselist=False,
        cascade="all, delete-orphan",
    )

    def __str__(self):
        return str(self.phone_number)

