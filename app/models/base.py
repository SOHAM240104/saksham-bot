# base_ingestion.py

import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, DateTime, Boolean, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class BaseModel(Base):
    """
    Minimal shared base model.

    ********************* Model Fields *********************
        PK          - id
        Unique      - uuid
        Datetime    - created, modified
        Boolean     - is_active, is_deleted
    """
    __abstract__ = True

    id = Column(Integer, primary_key=True, autoincrement=True)
    uuid = Column(UUID(as_uuid=True), default=uuid.uuid4, unique=True, nullable=False)

    created = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    modified = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

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
