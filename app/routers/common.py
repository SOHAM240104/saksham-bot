"""Shared helpers for admin routers."""

from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from typing import Iterator
from uuid import UUID

from fastapi import HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.models.chatbot.context import OSModel, PlatformModel, VersionModel
from app.schema.ingestion import IngestResponse
from app.schema.requests import IdentityOutput


def parse_uuid_or_bad_request(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid UUID format") from exc


def to_identity_output(row: PlatformModel | OSModel | VersionModel, status: str = "completed") -> IdentityOutput:
    return IdentityOutput(
        id=row.id,
        uuid=row.uuid,
        status=status,
        identity=row.identity,
        created=row.created.isoformat() if row.created else None,
        modified=row.modified.isoformat() if row.modified else None,
        is_deleted=row.is_deleted,
    )


def to_ingest_response(summary) -> IngestResponse:
    return IngestResponse(
        uuid=summary.uuid,
        status=summary.status,
        tokens_used=summary.tokens_used,
        cost_usd=summary.cost_usd,
        created=None,
        modified=None,
    )


@contextmanager
def with_temp_upload_file(file: UploadFile, fallback_suffix: str) -> Iterator[str]:
    suffix = os.path.splitext(file.filename or "")[1] or fallback_suffix
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            temp_path = tmp.name
            tmp.write(file.file.read())
        yield temp_path
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


def soft_delete_and_refresh(db: Session, row) -> str:
    if not row.is_deleted:
        row.is_deleted = True
        db.commit()
        db.refresh(row)
    return "deleted" if row.is_deleted else "completed"


def resolve_platform_or_404(db: Session, platform_uuid: str) -> PlatformModel:
    platform_parsed = parse_uuid_or_bad_request(platform_uuid)
    platform_row = db.query(PlatformModel).filter(PlatformModel.uuid == platform_parsed).first()
    if platform_row is None:
        raise HTTPException(status_code=404, detail="Platform not found")
    return platform_row


def resolve_platform_os_or_404(db: Session, platform_uuid: str, operating_system_uuid: str) -> tuple[PlatformModel, OSModel]:
    platform_row = resolve_platform_or_404(db, platform_uuid)
    os_parsed = parse_uuid_or_bad_request(operating_system_uuid)
    operating_system = (
        db.query(OSModel)
        .filter(OSModel.platform_id == platform_row.id, OSModel.uuid == os_parsed)
        .first()
    )
    if operating_system is None:
        raise HTTPException(status_code=404, detail="Operating system not found under platform")
    return platform_row, operating_system


def resolve_platform_os_version_or_404(
    db: Session,
    platform_uuid: str,
    operating_system_uuid: str,
    version_uuid: str,
) -> tuple[PlatformModel, OSModel, VersionModel]:
    platform_row, operating_system = resolve_platform_os_or_404(db, platform_uuid, operating_system_uuid)
    version_parsed = parse_uuid_or_bad_request(version_uuid)

    version_row = (
        db.query(VersionModel)
        .filter(VersionModel.os_id == operating_system.id, VersionModel.uuid == version_parsed)
        .first()
    )
    if version_row is None:
        raise HTTPException(status_code=404, detail="Version not found under platform and operating system")

    return platform_row, operating_system, version_row
