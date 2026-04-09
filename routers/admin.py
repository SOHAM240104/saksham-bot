import os
import tempfile
from collections.abc import Callable
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Security, UploadFile
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import require_admin_token
from app.ingestion import IngestSummary, ingest_bulk_urls, ingest_excel, ingest_pdf, ingest_single_url
from config.database import get_db
from models.context import OSModel, PlatformModel, VersionModel
from models.ingestion_records import IngestionUsageModel
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
        uuid=summary.uuid,
        status=summary.status,
        created=None,
        modified=None,
        processed=summary.processed_sources,
        skipped=summary.skipped_duplicates + summary.skipped_invalid,
        failed=summary.failed_sources,
        chunks=summary.chunks_inserted,
        tokens_used=summary.tokens_used,
        cost_usd=summary.cost_usd,
    )


def _usage_to_response(row: IngestionUsageModel) -> IngestResponse:
    return IngestResponse(
        uuid=row.uuid,
        status=row.status,
        created=row.created.isoformat() if row.created else None,
        modified=row.modified.isoformat() if row.modified else None,
        processed=row.processed,
        skipped=row.skipped,
        failed=row.failed,
        chunks=row.chunks,
        tokens_used=row.tokens_used,
        cost_usd=row.cost_usd,
    )


def _wrap_ingest(summary: IngestSummary, status_code: int = 200) -> IngestEnvelope:
    return IngestEnvelope(
        status_code=status_code,
        data=[_to_response(summary)],
    )


def _wrap_ingest_rows(rows: list[IngestionUsageModel], status_code: int = 200) -> IngestEnvelope:
    return IngestEnvelope(
        status_code=status_code,
        data=[_usage_to_response(row) for row in rows],
    )


def _wrap_name(row: PlatformModel | OSModel | VersionModel, status_code: int = 201) -> NameEnvelope:
    return NameEnvelope(
        status_code=status_code,
        data=[
            NameOutput(
                id=row.id,
                uuid=row.uuid,
                status="completed",
                name=row.name,
                created=row.created.isoformat() if row.created else None,
                modified=row.modified.isoformat() if row.modified else None,
                is_archived=row.is_archived,
                is_deleted=row.is_deleted,
            )
        ],
    )


def _wrap_name_rows(rows: list[PlatformModel | OSModel | VersionModel], status_code: int = 200, status: str = "completed") -> NameEnvelope:
    return NameEnvelope(
        status_code=status_code,
        data=[
            NameOutput(
                id=row.id,
                uuid=row.uuid,
                status=status,
                name=row.name,
                created=row.created.isoformat() if row.created else None,
                modified=row.modified.isoformat() if row.modified else None,
                is_archived=row.is_archived,
                is_deleted=row.is_deleted,
            )
            for row in rows
        ],
    )


def _parse_uuid_or_400(raw_uuid: str) -> UUID:
    try:
        return UUID(raw_uuid)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid UUID format") from exc


def _get_platform_or_404(db: Session, platform: str) -> PlatformModel:
    platform_row = (
        db.query(PlatformModel)
        .filter(PlatformModel.name == platform, PlatformModel.is_deleted.is_(False))
        .first()
    )
    if platform_row is None:
        raise HTTPException(status_code=404, detail="Platform not found")
    return platform_row


def _get_platform_by_uuid_or_404(db: Session, platform_uuid: str) -> PlatformModel:
    parsed = _parse_uuid_or_400(platform_uuid)
    platform_row = db.query(PlatformModel).filter(PlatformModel.uuid == parsed).first()
    if platform_row is None:
        raise HTTPException(status_code=404, detail="Platform not found")
    return platform_row


def _get_os_or_404(db: Session, platform_id: int, os_name: str) -> OSModel:
    os_row = (
        db.query(OSModel)
        .filter(OSModel.platform_id == platform_id, OSModel.name == os_name, OSModel.is_deleted.is_(False))
        .first()
    )
    if os_row is None:
        raise HTTPException(status_code=404, detail="OS not found under platform")
    return os_row


def _get_os_by_uuid_or_404(db: Session, platform_id: int, os_uuid: str) -> OSModel:
    parsed = _parse_uuid_or_400(os_uuid)
    os_row = db.query(OSModel).filter(OSModel.platform_id == platform_id, OSModel.uuid == parsed).first()
    if os_row is None:
        raise HTTPException(status_code=404, detail="OS not found under platform")
    return os_row


def _get_version_by_uuid_or_404(db: Session, os_id: int, version_uuid: str) -> VersionModel:
    parsed = _parse_uuid_or_400(version_uuid)
    version_row = db.query(VersionModel).filter(VersionModel.os_id == os_id, VersionModel.uuid == parsed).first()
    if version_row is None:
        raise HTTPException(status_code=404, detail="Version not found under platform and OS")
    return version_row


def _resolve_context_or_404(db: Session, platform: str, os_name: str, version: str) -> tuple[PlatformModel, OSModel, VersionModel]:
    platform_row = _get_platform_or_404(db, platform)
    os_row = _get_os_or_404(db, platform_row.id, os_name)
    version_row = (
        db.query(VersionModel)
        .filter(VersionModel.os_id == os_row.id, VersionModel.name == version, VersionModel.is_deleted.is_(False))
        .first()
    )
    if version_row is None:
        raise HTTPException(status_code=404, detail="Version not found under platform and OS")
    return platform_row, os_row, version_row


def _get_usage_by_uuid_or_404(db: Session, usage_uuid: str) -> IngestionUsageModel:
    parsed = _parse_uuid_or_400(usage_uuid)
    row = db.query(IngestionUsageModel).filter(IngestionUsageModel.uuid == parsed).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Ingestion usage not found")
    return row


def _run_file_ingestion(
    file: UploadFile,
    default_suffix: str,
    ingest_fn: Callable[[str, str, str, str], IngestSummary],
    platform: str,
    os_name: str,
    version: str,
) -> IngestEnvelope:
    suffix = os.path.splitext(file.filename or "")[1] or default_suffix
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            temp_path = tmp.name
            tmp.write(file.file.read())
        summary = ingest_fn(temp_path, platform=platform, os=os_name, version=version)
        return _wrap_ingest(summary, status_code=200)
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
        db.refresh(row)
        return _wrap_name(row)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Platform already exists") from exc


@router.get("/platforms", response_model=NameEnvelope, status_code=200)
def list_platforms(
    include_archived: bool = False,
    include_deleted: bool = False,
    db: Session = Depends(get_db),
) -> NameEnvelope:
    query = db.query(PlatformModel)
    if not include_archived:
        query = query.filter(PlatformModel.is_archived.is_(False))
    if not include_deleted:
        query = query.filter(PlatformModel.is_deleted.is_(False))
    rows = query.order_by(PlatformModel.created.desc()).all()
    return _wrap_name_rows(rows, status_code=200)


@router.get("/platforms/{platform_uuid}", response_model=NameEnvelope, status_code=200)
def get_platform(platform_uuid: str, db: Session = Depends(get_db)) -> NameEnvelope:
    row = _get_platform_by_uuid_or_404(db, platform_uuid)
    return _wrap_name_rows([row], status_code=200)


@router.patch("/platforms/{platform_uuid}", response_model=NameEnvelope, status_code=200)
def update_platform(platform_uuid: str, payload: NameInput, db: Session = Depends(get_db)) -> NameEnvelope:
    row = _get_platform_by_uuid_or_404(db, platform_uuid)
    row.name = payload.name
    db.commit()
    db.refresh(row)
    return _wrap_name_rows([row], status_code=200)


@router.post("/platforms/{platform_uuid}/archive", response_model=NameEnvelope, status_code=200)
def archive_platform(platform_uuid: str, db: Session = Depends(get_db)) -> NameEnvelope:
    row = _get_platform_by_uuid_or_404(db, platform_uuid)
    row.is_archived = True
    db.commit()
    db.refresh(row)
    return _wrap_name_rows([row], status_code=200, status="archived")


@router.post("/platforms/{platform_uuid}/unarchive", response_model=NameEnvelope, status_code=200)
def unarchive_platform(platform_uuid: str, db: Session = Depends(get_db)) -> NameEnvelope:
    row = _get_platform_by_uuid_or_404(db, platform_uuid)
    row.is_archived = False
    db.commit()
    db.refresh(row)
    return _wrap_name_rows([row], status_code=200, status="completed")


@router.delete("/platforms/{platform_uuid}", response_model=NameEnvelope, status_code=200)
def delete_platform(platform_uuid: str, db: Session = Depends(get_db)) -> NameEnvelope:
    row = _get_platform_by_uuid_or_404(db, platform_uuid)
    row.is_deleted = True
    db.commit()
    db.refresh(row)
    return _wrap_name_rows([row], status_code=200, status="deleted")


@router.post("/platforms/{platform}/oses", response_model=NameEnvelope, status_code=201)
def create_os(platform: str, payload: NameInput, db: Session = Depends(get_db)) -> NameEnvelope:
    platform_row = _get_platform_or_404(db, platform)
    try:
        row = OSModel(platform_id=platform_row.id, name=payload.name)
        db.add(row)
        db.commit()
        db.refresh(row)
        return _wrap_name(row)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="OS already exists under platform") from exc


@router.get("/platforms/{platform_uuid}/oses", response_model=NameEnvelope, status_code=200)
def list_oses(
    platform_uuid: str,
    include_archived: bool = False,
    include_deleted: bool = False,
    db: Session = Depends(get_db),
) -> NameEnvelope:
    platform_row = _get_platform_by_uuid_or_404(db, platform_uuid)
    query = db.query(OSModel).filter(OSModel.platform_id == platform_row.id)
    if not include_archived:
        query = query.filter(OSModel.is_archived.is_(False))
    if not include_deleted:
        query = query.filter(OSModel.is_deleted.is_(False))
    rows = query.order_by(OSModel.created.desc()).all()
    return _wrap_name_rows(rows, status_code=200)


@router.get("/platforms/{platform_uuid}/oses/{os_uuid}", response_model=NameEnvelope, status_code=200)
def get_os(platform_uuid: str, os_uuid: str, db: Session = Depends(get_db)) -> NameEnvelope:
    platform_row = _get_platform_by_uuid_or_404(db, platform_uuid)
    row = _get_os_by_uuid_or_404(db, platform_row.id, os_uuid)
    return _wrap_name_rows([row], status_code=200)


@router.patch("/platforms/{platform_uuid}/oses/{os_uuid}", response_model=NameEnvelope, status_code=200)
def update_os(platform_uuid: str, os_uuid: str, payload: NameInput, db: Session = Depends(get_db)) -> NameEnvelope:
    platform_row = _get_platform_by_uuid_or_404(db, platform_uuid)
    row = _get_os_by_uuid_or_404(db, platform_row.id, os_uuid)
    row.name = payload.name
    db.commit()
    db.refresh(row)
    return _wrap_name_rows([row], status_code=200)


@router.post("/platforms/{platform_uuid}/oses/{os_uuid}/archive", response_model=NameEnvelope, status_code=200)
def archive_os(platform_uuid: str, os_uuid: str, db: Session = Depends(get_db)) -> NameEnvelope:
    platform_row = _get_platform_by_uuid_or_404(db, platform_uuid)
    row = _get_os_by_uuid_or_404(db, platform_row.id, os_uuid)
    row.is_archived = True
    db.commit()
    db.refresh(row)
    return _wrap_name_rows([row], status_code=200, status="archived")


@router.post("/platforms/{platform_uuid}/oses/{os_uuid}/unarchive", response_model=NameEnvelope, status_code=200)
def unarchive_os(platform_uuid: str, os_uuid: str, db: Session = Depends(get_db)) -> NameEnvelope:
    platform_row = _get_platform_by_uuid_or_404(db, platform_uuid)
    row = _get_os_by_uuid_or_404(db, platform_row.id, os_uuid)
    row.is_archived = False
    db.commit()
    db.refresh(row)
    return _wrap_name_rows([row], status_code=200, status="completed")


@router.delete("/platforms/{platform_uuid}/oses/{os_uuid}", response_model=NameEnvelope, status_code=200)
def delete_os(platform_uuid: str, os_uuid: str, db: Session = Depends(get_db)) -> NameEnvelope:
    platform_row = _get_platform_by_uuid_or_404(db, platform_uuid)
    row = _get_os_by_uuid_or_404(db, platform_row.id, os_uuid)
    row.is_deleted = True
    db.commit()
    db.refresh(row)
    return _wrap_name_rows([row], status_code=200, status="deleted")


@router.post("/platforms/{platform}/oses/{os}/versions", response_model=NameEnvelope, status_code=201)
def create_version(platform: str, os: str, payload: NameInput, db: Session = Depends(get_db)) -> NameEnvelope:
    platform_row = _get_platform_or_404(db, platform)
    os_row = _get_os_or_404(db, platform_row.id, os)
    try:
        row = VersionModel(os_id=os_row.id, name=payload.name)
        db.add(row)
        db.commit()
        db.refresh(row)
        return _wrap_name(row)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Version already exists under platform and OS") from exc


@router.get("/platforms/{platform_uuid}/oses/{os_uuid}/versions", response_model=NameEnvelope, status_code=200)
def list_versions(
    platform_uuid: str,
    os_uuid: str,
    include_archived: bool = False,
    include_deleted: bool = False,
    db: Session = Depends(get_db),
) -> NameEnvelope:
    platform_row = _get_platform_by_uuid_or_404(db, platform_uuid)
    os_row = _get_os_by_uuid_or_404(db, platform_row.id, os_uuid)
    query = db.query(VersionModel).filter(VersionModel.os_id == os_row.id)
    if not include_archived:
        query = query.filter(VersionModel.is_archived.is_(False))
    if not include_deleted:
        query = query.filter(VersionModel.is_deleted.is_(False))
    rows = query.order_by(VersionModel.created.desc()).all()
    return _wrap_name_rows(rows, status_code=200)


@router.get("/platforms/{platform_uuid}/oses/{os_uuid}/versions/{version_uuid}", response_model=NameEnvelope, status_code=200)
def get_version(platform_uuid: str, os_uuid: str, version_uuid: str, db: Session = Depends(get_db)) -> NameEnvelope:
    platform_row = _get_platform_by_uuid_or_404(db, platform_uuid)
    os_row = _get_os_by_uuid_or_404(db, platform_row.id, os_uuid)
    row = _get_version_by_uuid_or_404(db, os_row.id, version_uuid)
    return _wrap_name_rows([row], status_code=200)


@router.patch("/platforms/{platform_uuid}/oses/{os_uuid}/versions/{version_uuid}", response_model=NameEnvelope, status_code=200)
def update_version(platform_uuid: str, os_uuid: str, version_uuid: str, payload: NameInput, db: Session = Depends(get_db)) -> NameEnvelope:
    platform_row = _get_platform_by_uuid_or_404(db, platform_uuid)
    os_row = _get_os_by_uuid_or_404(db, platform_row.id, os_uuid)
    row = _get_version_by_uuid_or_404(db, os_row.id, version_uuid)
    row.name = payload.name
    db.commit()
    db.refresh(row)
    return _wrap_name_rows([row], status_code=200)


@router.post("/platforms/{platform_uuid}/oses/{os_uuid}/versions/{version_uuid}/archive", response_model=NameEnvelope, status_code=200)
def archive_version(platform_uuid: str, os_uuid: str, version_uuid: str, db: Session = Depends(get_db)) -> NameEnvelope:
    platform_row = _get_platform_by_uuid_or_404(db, platform_uuid)
    os_row = _get_os_by_uuid_or_404(db, platform_row.id, os_uuid)
    row = _get_version_by_uuid_or_404(db, os_row.id, version_uuid)
    row.is_archived = True
    db.commit()
    db.refresh(row)
    return _wrap_name_rows([row], status_code=200, status="archived")


@router.post("/platforms/{platform_uuid}/oses/{os_uuid}/versions/{version_uuid}/unarchive", response_model=NameEnvelope, status_code=200)
def unarchive_version(platform_uuid: str, os_uuid: str, version_uuid: str, db: Session = Depends(get_db)) -> NameEnvelope:
    platform_row = _get_platform_by_uuid_or_404(db, platform_uuid)
    os_row = _get_os_by_uuid_or_404(db, platform_row.id, os_uuid)
    row = _get_version_by_uuid_or_404(db, os_row.id, version_uuid)
    row.is_archived = False
    db.commit()
    db.refresh(row)
    return _wrap_name_rows([row], status_code=200, status="completed")


@router.delete("/platforms/{platform_uuid}/oses/{os_uuid}/versions/{version_uuid}", response_model=NameEnvelope, status_code=200)
def delete_version(platform_uuid: str, os_uuid: str, version_uuid: str, db: Session = Depends(get_db)) -> NameEnvelope:
    platform_row = _get_platform_by_uuid_or_404(db, platform_uuid)
    os_row = _get_os_by_uuid_or_404(db, platform_row.id, os_uuid)
    row = _get_version_by_uuid_or_404(db, os_row.id, version_uuid)
    row.is_deleted = True
    db.commit()
    db.refresh(row)
    return _wrap_name_rows([row], status_code=200, status="deleted")


@router.post("/platforms/{platform}/oses/{os}/versions/{version}/train/url", response_model=IngestEnvelope, status_code=200)
def train_single_url(platform: str, os: str, version: str, payload: TrainURLInput, db: Session = Depends(get_db)) -> IngestEnvelope:
    _resolve_context_or_404(db, platform, os, version)
    summary = ingest_single_url(
        url=payload.url,
        platform=platform,
        os=os,
        version=version,
    )
    return _wrap_ingest(summary)


@router.post("/platforms/{platform}/oses/{os}/versions/{version}/train/urls", response_model=IngestEnvelope, status_code=200)
def train_bulk_urls(platform: str, os: str, version: str, payload: TrainBulkURLsInput, db: Session = Depends(get_db)) -> IngestEnvelope:
    _resolve_context_or_404(db, platform, os, version)
    summary = ingest_bulk_urls(
        urls=payload.urls,
        platform=platform,
        os=os,
        version=version,
    )
    return _wrap_ingest(summary)


@router.post("/platforms/{platform}/oses/{os}/versions/{version}/train/excel", response_model=IngestEnvelope, status_code=200)
def train_excel(
    platform: str,
    os: str,
    version: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> IngestEnvelope:
    _resolve_context_or_404(db, platform, os, version)
    return _run_file_ingestion(
        file=file,
        default_suffix=".xlsx",
        ingest_fn=ingest_excel,
        platform=platform,
        os_name=os,
        version=version,
    )


@router.post("/platforms/{platform}/oses/{os}/versions/{version}/train/pdf", response_model=IngestEnvelope, status_code=200)
def train_pdf(
    platform: str,
    os: str,
    version: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> IngestEnvelope:
    _resolve_context_or_404(db, platform, os, version)
    return _run_file_ingestion(
        file=file,
        default_suffix=".pdf",
        ingest_fn=ingest_pdf,
        platform=platform,
        os_name=os,
        version=version,
    )


@router.get("/ingestion-usage", response_model=IngestEnvelope, status_code=200)
def list_ingestion_usage(
    include_archived: bool = False,
    include_deleted: bool = False,
    db: Session = Depends(get_db),
) -> IngestEnvelope:
    query = db.query(IngestionUsageModel)
    if not include_archived:
        query = query.filter(IngestionUsageModel.is_archived.is_(False))
    if not include_deleted:
        query = query.filter(IngestionUsageModel.is_deleted.is_(False))
    rows = query.order_by(IngestionUsageModel.created.desc()).all()
    return _wrap_ingest_rows(rows, status_code=200)


@router.get("/ingestion-usage/{usage_uuid}", response_model=IngestEnvelope, status_code=200)
def get_ingestion_usage(usage_uuid: str, db: Session = Depends(get_db)) -> IngestEnvelope:
    row = _get_usage_by_uuid_or_404(db, usage_uuid)
    return _wrap_ingest_rows([row], status_code=200)


@router.patch("/ingestion-usage/{usage_uuid}/status/{status}", response_model=IngestEnvelope, status_code=200)
def update_ingestion_usage_status(usage_uuid: str, status: str, db: Session = Depends(get_db)) -> IngestEnvelope:
    allowed = {"not_started", "in_progress", "completed", "failed"}
    if status not in allowed:
        raise HTTPException(status_code=400, detail=f"Invalid status. Allowed: {sorted(allowed)}")
    row = _get_usage_by_uuid_or_404(db, usage_uuid)
    row.status = status
    db.commit()
    db.refresh(row)
    return _wrap_ingest_rows([row], status_code=200)


@router.post("/ingestion-usage/{usage_uuid}/archive", response_model=IngestEnvelope, status_code=200)
def archive_ingestion_usage(usage_uuid: str, db: Session = Depends(get_db)) -> IngestEnvelope:
    row = _get_usage_by_uuid_or_404(db, usage_uuid)
    row.is_archived = True
    db.commit()
    db.refresh(row)
    return _wrap_ingest_rows([row], status_code=200)


@router.post("/ingestion-usage/{usage_uuid}/unarchive", response_model=IngestEnvelope, status_code=200)
def unarchive_ingestion_usage(usage_uuid: str, db: Session = Depends(get_db)) -> IngestEnvelope:
    row = _get_usage_by_uuid_or_404(db, usage_uuid)
    row.is_archived = False
    db.commit()
    db.refresh(row)
    return _wrap_ingest_rows([row], status_code=200)


@router.delete("/ingestion-usage/{usage_uuid}", response_model=IngestEnvelope, status_code=200)
def delete_ingestion_usage(usage_uuid: str, db: Session = Depends(get_db)) -> IngestEnvelope:
    row = _get_usage_by_uuid_or_404(db, usage_uuid)
    row.is_deleted = True
    db.commit()
    db.refresh(row)
    return _wrap_ingest_rows([row], status_code=200)
