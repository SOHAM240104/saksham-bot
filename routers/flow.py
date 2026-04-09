import os
import tempfile

from fastapi import APIRouter, Depends, File, HTTPException, Security, UploadFile
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import require_admin_token
from app.ingestion import IngestSummary, ingest_bulk_urls, ingest_excel, ingest_pdf, ingest_single_url
from config.database import get_db
from models.flow import OSModel, PlatformModel, VersionModel
from schema.flow import (
    NameInput,
    NameOutput,
    TrainBulkURLsInput,
    TrainURLInput,
)
from schema.ingestion import IngestResponse

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


def _ensure_context_exists(db: Session, platform: str, os_name: str, version: str) -> None:
    platform_row = db.query(PlatformModel).filter(PlatformModel.name == platform).first()
    if platform_row is None:
        raise HTTPException(status_code=404, detail="Platform not found")
    os_row = (
        db.query(OSModel)
        .filter(OSModel.platform_id == platform_row.id, OSModel.name == os_name)
        .first()
    )
    if os_row is None:
        raise HTTPException(status_code=404, detail="OS not found under platform")
    version_row = (
        db.query(VersionModel)
        .filter(VersionModel.os_id == os_row.id, VersionModel.name == version)
        .first()
    )
    if version_row is None:
        raise HTTPException(status_code=404, detail="Version not found under platform and OS")


@router.post("/platforms", response_model=NameOutput)
def create_platform(payload: NameInput, db: Session = Depends(get_db)) -> NameOutput:
    try:
        row = PlatformModel(name=payload.name)
        db.add(row)
        db.commit()
        return NameOutput(name=row.name)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Platform already exists") from exc


@router.post("/platforms/{platform}/oses", response_model=NameOutput)
def create_os(platform: str, payload: NameInput, db: Session = Depends(get_db)) -> NameOutput:
    platform_row = db.query(PlatformModel).filter(PlatformModel.name == platform).first()
    if platform_row is None:
        raise HTTPException(status_code=404, detail="Platform not found")
    try:
        row = OSModel(platform_id=platform_row.id, name=payload.name)
        db.add(row)
        db.commit()
        return NameOutput(name=row.name)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="OS already exists under platform") from exc


@router.post("/platforms/{platform}/oses/{os}/versions", response_model=NameOutput)
def create_version(platform: str, os: str, payload: NameInput, db: Session = Depends(get_db)) -> NameOutput:
    platform_row = db.query(PlatformModel).filter(PlatformModel.name == platform).first()
    if platform_row is None:
        raise HTTPException(status_code=404, detail="Platform not found")
    os_row = (
        db.query(OSModel)
        .filter(OSModel.platform_id == platform_row.id, OSModel.name == os)
        .first()
    )
    if os_row is None:
        raise HTTPException(status_code=404, detail="OS not found under platform")
    try:
        row = VersionModel(os_id=os_row.id, name=payload.name)
        db.add(row)
        db.commit()
        return NameOutput(name=row.name)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Version already exists under platform and OS") from exc


@router.post("/platforms/{platform}/oses/{os}/versions/{version}/train/url", response_model=IngestResponse)
def train_single_url(platform: str, os: str, version: str, payload: TrainURLInput, db: Session = Depends(get_db)) -> IngestResponse:
    _ensure_context_exists(db, platform, os, version)
    summary = ingest_single_url(
        url=payload.url,
        platform=platform,
        os=os,
        version=version,
    )
    return _to_response(summary)


@router.post("/platforms/{platform}/oses/{os}/versions/{version}/train/urls", response_model=IngestResponse)
def train_bulk_urls(platform: str, os: str, version: str, payload: TrainBulkURLsInput, db: Session = Depends(get_db)) -> IngestResponse:
    _ensure_context_exists(db, platform, os, version)
    summary = ingest_bulk_urls(
        urls=payload.urls,
        platform=platform,
        os=os,
        version=version,
    )
    return _to_response(summary)


@router.post("/platforms/{platform}/oses/{os}/versions/{version}/train/excel", response_model=IngestResponse)
def train_excel(
    platform: str,
    os: str,
    version: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> IngestResponse:
    _ensure_context_exists(db, platform, os, version)
    suffix = os.path.splitext(file.filename or "")[1] or ".xlsx"
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            temp_path = tmp.name
            tmp.write(file.file.read())
        summary = ingest_excel(temp_path, platform=platform, os=os, version=version)
        return _to_response(summary)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


@router.post("/platforms/{platform}/oses/{os}/versions/{version}/train/pdf", response_model=IngestResponse)
def train_pdf(
    platform: str,
    os: str,
    version: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> IngestResponse:
    _ensure_context_exists(db, platform, os, version)
    suffix = os.path.splitext(file.filename or "")[1] or ".pdf"
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            temp_path = tmp.name
            tmp.write(file.file.read())
        summary = ingest_pdf(temp_path, platform=platform, os=os, version=version)
        return _to_response(summary)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)
