"""Lightweight SQLAlchemy helpers shared by ingestion pipelines."""

from sqlalchemy.orm import Session

from config.database import Base


def bootstrap_orm_metadata(db: Session) -> None:
    Base.metadata.create_all(bind=db.get_bind())
