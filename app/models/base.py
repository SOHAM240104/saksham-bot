# base_ingestion.py

import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, DateTime, Boolean, Integer, String, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship, declared_attr, declarative_base


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
