import os
import tempfile
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Security, UploadFile
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import require_admin_token
from app.ingestion import IngestSummary, ingest_bulk_urls, ingest_excel, ingest_pdf, ingest_single_url
from config.database import get_db
from models.context import OSModel, PlatformModel, VersionModel
from schema.requests import (
    NameEnvelope,
    NameInput,
    NameOutput,
    TrainBulkURLsInput,
    TrainURLInput,
)
from schema.ingestion import IngestEnvelope, IngestResponse

router = APIRouter(dependencies=[Security(require_admin_token)])


def _to_response(summary: IngestSummary) -> IngestResponse:
    return IngestResponse(
        processed=summary.processed_sources,
        skipped=summary.skipped_duplicates + summary.skipped_invalid,
        failed=summary.failed_sources,
        chunks=summary.chunks_inserted,
        tokens_used=summary.tokens_used,
        cost_usd=summary.cost_usd,
    )


def _wrap_response(data: list[Any], status_code: int, message: str) -> dict[str, Any]:
    return {
        "request_id": uuid4(),
        "status_code": status_code,
        "message": message,
        "data": data,
    }


def _wrap_ingest(summary: IngestSummary, status_code: int = 200, message: str = "success") -> IngestEnvelope:
    return IngestEnvelope(**_wrap_response([_to_response(summary)], status_code, message))


def _wrap_name(name: str, status_code: int = 201, message: str = "created") -> NameEnvelope:
    return NameEnvelope(**_wrap_response([NameOutput(name=name)], status_code, message))


def _get_platform_or_404(db: Session, platform: str) -> PlatformModel:
    platform_row = db.query(PlatformModel).filter(PlatformModel.name == platform).first()
    if platform_row is None:
        raise HTTPException(status_code=404, detail="Platform not found")
    return platform_row


def _get_os_or_404(db: Session, platform_id: int, os_name: str) -> OSModel:
    os_row = db.query(OSModel).filter(OSModel.platform_id == platform_id, OSModel.name == os_name).first()
    if os_row is None:
        raise HTTPException(status_code=404, detail="OS not found under platform")
    return os_row


def _ensure_context_exists(db: Session, platform: str, os_name: str, version: str) -> None:
    platform_row = _get_platform_or_404(db, platform)
    os_row = _get_os_or_404(db, platform_row.id, os_name)
    version_row = (
        db.query(VersionModel)
        .filter(VersionModel.os_id == os_row.id, VersionModel.name == version)
        .first()
    )
    if version_row is None:
        raise HTTPException(status_code=404, detail="Version not found under platform and OS")


def _run_file_ingestion(
    file: UploadFile,
    default_suffix: str,
    ingest_fn: Callable[[str, str, str, str], IngestSummary],
    platform: str,
    os_name: str,
    version: str,
    message: str,
) -> IngestEnvelope:
    suffix = os.path.splitext(file.filename or "")[1] or default_suffix
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            temp_path = tmp.name
            tmp.write(file.file.read())
        summary = ingest_fn(temp_path, platform=platform, os=os_name, version=version)
        return _wrap_ingest(summary, status_code=200, message=message)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


@router.post("/platforms", response_model=NameEnvelope, status_code=201)
def create_platform(payload: NameInput, db: Session = Depends(get_db)) -> NameEnvelope:
    try:
        row = PlatformModel(name=payload.name)
        db.add(row)
        db.commit()
        return _wrap_name(row.name)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Platform already exists") from exc


@router.post("/platforms/{platform}/oses", response_model=NameEnvelope, status_code=201)
def create_os(platform: str, payload: NameInput, db: Session = Depends(get_db)) -> NameEnvelope:
    platform_row = _get_platform_or_404(db, platform)
    try:
        row = OSModel(platform_id=platform_row.id, name=payload.name)
        db.add(row)
        db.commit()
        return _wrap_name(row.name)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="OS already exists under platform") from exc


@router.post("/platforms/{platform}/oses/{os}/versions", response_model=NameEnvelope, status_code=201)
def create_version(platform: str, os: str, payload: NameInput, db: Session = Depends(get_db)) -> NameEnvelope:
    platform_row = _get_platform_or_404(db, platform)
    os_row = _get_os_or_404(db, platform_row.id, os)
    try:
        row = VersionModel(os_id=os_row.id, name=payload.name)
        db.add(row)
        db.commit()
        return _wrap_name(row.name)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Version already exists under platform and OS") from exc


@router.post("/platforms/{platform}/oses/{os}/versions/{version}/train/url", response_model=IngestEnvelope, status_code=200)
def train_single_url(platform: str, os: str, version: str, payload: TrainURLInput, db: Session = Depends(get_db)) -> IngestEnvelope:
    _ensure_context_exists(db, platform, os, version)
    summary = ingest_single_url(
        url=payload.url,
        platform=platform,
        os=os,
        version=version,
    )
    return _wrap_ingest(summary, status_code=200, message="ingestion completed")


@router.post("/platforms/{platform}/oses/{os}/versions/{version}/train/urls", response_model=IngestEnvelope, status_code=200)
def train_bulk_urls(platform: str, os: str, version: str, payload: TrainBulkURLsInput, db: Session = Depends(get_db)) -> IngestEnvelope:
    _ensure_context_exists(db, platform, os, version)
    summary = ingest_bulk_urls(
        urls=payload.urls,
        platform=platform,
        os=os,
        version=version,
    )
    return _wrap_ingest(summary, status_code=200, message="bulk ingestion completed")


@router.post("/platforms/{platform}/oses/{os}/versions/{version}/train/excel", response_model=IngestEnvelope, status_code=200)
def train_excel(
    platform: str,
    os: str,
    version: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> IngestEnvelope:
    _ensure_context_exists(db, platform, os, version)
    return _run_file_ingestion(
        file=file,
        default_suffix=".xlsx",
        ingest_fn=ingest_excel,
        platform=platform,
        os_name=os,
        version=version,
        message="excel ingestion completed",
    )


@router.post("/platforms/{platform}/oses/{os}/versions/{version}/train/pdf", response_model=IngestEnvelope, status_code=200)
def train_pdf(
    platform: str,
    os: str,
    version: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> IngestEnvelope:
    _ensure_context_exists(db, platform, os, version)
    return _run_file_ingestion(
        file=file,
        default_suffix=".pdf",
        ingest_fn=ingest_pdf,
        platform=platform,
        os_name=os,
        version=version,
        message="pdf ingestion completed",
    )
