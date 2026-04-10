"""Admin API with pragmatic, local helpers."""

import os
import tempfile
from collections.abc import Callable
from uuid import UUID

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Security, UploadFile
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import require_admin_token
from ingestion import IngestSummary, ingest_bulk_urls, ingest_excel, ingest_pdf, ingest_single_url
from config.database import get_db
from models.context import OSModel, PlatformModel, VersionModel
from models.ingestion_records import IngestionUsageModel
from settings import normalize_vector_collection
from schema.requests import (
    ArchiveStatePatchRequest,
    IdentityInput,
    IdentityEnvelope,
    IdentityOutput,
    TrainBulkURLsInput,
    TrainURLInput,
    UpdateIngestionUsageRequest,
)
from schema.ingestion import IngestEnvelope, IngestResponse

router = APIRouter(dependencies=[Security(require_admin_token)])
_ALLOWED_USAGE_STATUS = frozenset({"not_started", "in_progress", "completed", "failed"})


def _vector_collection_form_or_400(raw: str) -> str:
    try:
        return normalize_vector_collection(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _parse_uuid_or_400(raw_uuid: str) -> UUID:
    try:
        return UUID(raw_uuid)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid UUID format") from exc


def _get_platform_by_uuid_or_404(db: Session, platform_uuid: str) -> PlatformModel:
    parsed = _parse_uuid_or_400(platform_uuid)
    row = db.query(PlatformModel).filter(PlatformModel.uuid == parsed).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Platform not found")
    return row


def _get_os_by_uuid_or_404(db: Session, platform_id: int, os_uuid: str) -> OSModel:
    parsed = _parse_uuid_or_400(os_uuid)
    row = db.query(OSModel).filter(OSModel.platform_id == platform_id, OSModel.uuid == parsed).first()
    if row is None:
        raise HTTPException(status_code=404, detail="OS not found under platform")
    return row


def _get_version_by_uuid_or_404(db: Session, os_id: int, version_uuid: str) -> VersionModel:
    parsed = _parse_uuid_or_400(version_uuid)
    row = db.query(VersionModel).filter(VersionModel.os_id == os_id, VersionModel.uuid == parsed).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Version not found under platform and OS")
    return row


def _resolve_context_or_404(
    db: Session, platform_uuid: str, os_uuid: str, version_uuid: str
) -> tuple[PlatformModel, OSModel, VersionModel]:
    platform_row = _get_platform_by_uuid_or_404(db, platform_uuid)
    os_row = _get_os_by_uuid_or_404(db, platform_row.id, os_uuid)
    version_row = _get_version_by_uuid_or_404(db, os_row.id, version_uuid)
    return platform_row, os_row, version_row


def _get_usage_by_uuid_or_404(db: Session, usage_uuid: str) -> IngestionUsageModel:
    parsed = _parse_uuid_or_400(usage_uuid)
    row = db.query(IngestionUsageModel).filter(IngestionUsageModel.uuid == parsed).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Ingestion usage not found")
    return row


def _insert_platform(db: Session, identity: str) -> PlatformModel:
    try:
        row = PlatformModel(identity=identity)
        db.add(row)
        db.commit()
        db.refresh(row)
        return row
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Platform already exists") from exc


def _insert_os(db: Session, platform_id: int, identity: str) -> OSModel:
    try:
        row = OSModel(platform_id=platform_id, identity=identity)
        db.add(row)
        db.commit()
        db.refresh(row)
        return row
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="OS already exists under platform") from exc


def _insert_version(db: Session, os_id: int, identity: str) -> VersionModel:
    try:
        row = VersionModel(os_id=os_id, identity=identity)
        db.add(row)
        db.commit()
        db.refresh(row)
        return row
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Version already exists under platform and OS") from exc


def set_archive_state(db: Session, row: object, value: bool):
    if getattr(row, "is_archived") == value:
        return row
    setattr(row, "is_archived", value)
    db.commit()
    db.refresh(row)
    return row


def set_deleted_state(db: Session, row: object, value: bool = True):
    if getattr(row, "is_deleted") == value:
        return row
    setattr(row, "is_deleted", value)
    db.commit()
    db.refresh(row)
    return row


def _patch_usage(db: Session, row: IngestionUsageModel, payload: UpdateIngestionUsageRequest) -> IngestionUsageModel:
    changed = False
    if payload.is_archived is not None and row.is_archived != payload.is_archived:
        row.is_archived = payload.is_archived
        changed = True
    if payload.status is not None:
        if payload.status not in _ALLOWED_USAGE_STATUS:
            raise HTTPException(status_code=400, detail=f"Invalid status. Allowed: {sorted(_ALLOWED_USAGE_STATUS)}")
        if row.status != payload.status:
            row.status = payload.status
            changed = True
    if changed:
        db.commit()
        db.refresh(row)
    return row


def _identity_status_for_row(row: PlatformModel | OSModel | VersionModel) -> str:
    if row.is_deleted:
        return "deleted"
    if row.is_archived:
        return "archived"
    return "completed"


def _to_ingest_response(summary: IngestSummary) -> IngestResponse:
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


def _wrap_ingest(summary: IngestSummary, status_code: int = 200) -> IngestEnvelope:
    return IngestEnvelope(status_code=status_code, data=[_to_ingest_response(summary)])


def _run_file_ingestion(
    file: UploadFile,
    default_suffix: str,
    ingest_fn: Callable[..., IngestSummary],
    platform: str,
    os_name: str,
    version: str,
    vector_collection: str,
) -> IngestEnvelope:
    suffix = os.path.splitext(file.filename or "")[1] or default_suffix
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            temp_path = tmp.name
            tmp.write(file.file.read())
        summary = ingest_fn(
            temp_path,
            platform=platform,
            os=os_name,
            version=version,
            vector_collection=vector_collection,
        )
        return _wrap_ingest(summary)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


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


def _wrap_ingest_rows(rows: list[IngestionUsageModel], status_code: int = 200) -> IngestEnvelope:
    return IngestEnvelope(
        status_code=status_code,
        data=[_usage_to_response(row) for row in rows],
    )


def _wrap_identity(row: PlatformModel | OSModel | VersionModel, status_code: int = 201) -> IdentityEnvelope:
    return IdentityEnvelope(
        status_code=status_code,
        data=[
            IdentityOutput(
                id=row.id,
                uuid=row.uuid,
                status="completed",
                identity=row.identity,
                created=row.created.isoformat() if row.created else None,
                modified=row.modified.isoformat() if row.modified else None,
                is_archived=row.is_archived,
                is_deleted=row.is_deleted,
            )
        ],
    )


def _wrap_identity_rows(
    rows: list[PlatformModel | OSModel | VersionModel], status_code: int = 200, status: str = "completed"
) -> IdentityEnvelope:
    return IdentityEnvelope(
        status_code=status_code,
        data=[
            IdentityOutput(
                id=row.id,
                uuid=row.uuid,
                status=status,
                identity=row.identity,
                created=row.created.isoformat() if row.created else None,
                modified=row.modified.isoformat() if row.modified else None,
                is_archived=row.is_archived,
                is_deleted=row.is_deleted,
            )
            for row in rows
        ],
    )


@router.post("/platforms", response_model=IdentityEnvelope, status_code=201)
def create_platform(payload: IdentityInput, db: Session = Depends(get_db)) -> IdentityEnvelope:
    row = _insert_platform(db, payload.identity)
    return _wrap_identity(row)


@router.get("/platforms", response_model=IdentityEnvelope, status_code=200)
def list_platforms(
    include_archived: bool = False,
    include_deleted: bool = False,
    db: Session = Depends(get_db),
) -> IdentityEnvelope:
    query = db.query(PlatformModel)
    if not include_archived:
        query = query.filter(PlatformModel.is_archived.is_(False))
    if not include_deleted:
        query = query.filter(PlatformModel.is_deleted.is_(False))
    rows = query.order_by(PlatformModel.created.desc()).all()
    return _wrap_identity_rows(rows, status_code=200)


@router.get("/platforms/{platform_uuid}", response_model=IdentityEnvelope, status_code=200)
def get_platform(platform_uuid: str, db: Session = Depends(get_db)) -> IdentityEnvelope:
    row = _get_platform_by_uuid_or_404(db, platform_uuid)
    return _wrap_identity_rows([row], status_code=200)


@router.patch("/platforms/{platform_uuid}", response_model=IdentityEnvelope, status_code=200)
def patch_platform(
    platform_uuid: str, payload: ArchiveStatePatchRequest, db: Session = Depends(get_db)
) -> IdentityEnvelope:
    row = _get_platform_by_uuid_or_404(db, platform_uuid)
    row = set_archive_state(db, row, payload.is_archived) if payload.is_archived is not None else row
    return _wrap_identity_rows([row], status_code=200, status=_identity_status_for_row(row))


@router.delete("/platforms/{platform_uuid}", response_model=IdentityEnvelope, status_code=200)
def delete_platform(platform_uuid: str, db: Session = Depends(get_db)) -> IdentityEnvelope:
    row = _get_platform_by_uuid_or_404(db, platform_uuid)
    row = set_deleted_state(db, row, True)
    return _wrap_identity_rows([row], status_code=200, status=_identity_status_for_row(row))


@router.post("/platforms/{platform_uuid}/oses", response_model=IdentityEnvelope, status_code=201)
def create_os(platform_uuid: str, payload: IdentityInput, db: Session = Depends(get_db)) -> IdentityEnvelope:
    platform_row = _get_platform_by_uuid_or_404(db, platform_uuid)
    row = _insert_os(db, platform_row.id, payload.identity)
    return _wrap_identity(row)


@router.get("/platforms/{platform_uuid}/oses", response_model=IdentityEnvelope, status_code=200)
def list_oses(
    platform_uuid: str,
    include_archived: bool = False,
    include_deleted: bool = False,
    db: Session = Depends(get_db),
) -> IdentityEnvelope:
    platform_row = _get_platform_by_uuid_or_404(db, platform_uuid)
    query = db.query(OSModel).filter(OSModel.platform_id == platform_row.id)
    if not include_archived:
        query = query.filter(OSModel.is_archived.is_(False))
    if not include_deleted:
        query = query.filter(OSModel.is_deleted.is_(False))
    rows = query.order_by(OSModel.created.desc()).all()
    return _wrap_identity_rows(rows, status_code=200)


@router.get("/platforms/{platform_uuid}/oses/{os_uuid}", response_model=IdentityEnvelope, status_code=200)
def get_os(platform_uuid: str, os_uuid: str, db: Session = Depends(get_db)) -> IdentityEnvelope:
    platform_row = _get_platform_by_uuid_or_404(db, platform_uuid)
    row = _get_os_by_uuid_or_404(db, platform_row.id, os_uuid)
    return _wrap_identity_rows([row], status_code=200)


@router.patch("/platforms/{platform_uuid}/oses/{os_uuid}", response_model=IdentityEnvelope, status_code=200)
def patch_os(
    platform_uuid: str, os_uuid: str, payload: ArchiveStatePatchRequest, db: Session = Depends(get_db)
) -> IdentityEnvelope:
    platform_row = _get_platform_by_uuid_or_404(db, platform_uuid)
    row = _get_os_by_uuid_or_404(db, platform_row.id, os_uuid)
    row = set_archive_state(db, row, payload.is_archived) if payload.is_archived is not None else row
    return _wrap_identity_rows([row], status_code=200, status=_identity_status_for_row(row))


@router.delete("/platforms/{platform_uuid}/oses/{os_uuid}", response_model=IdentityEnvelope, status_code=200)
def delete_os(platform_uuid: str, os_uuid: str, db: Session = Depends(get_db)) -> IdentityEnvelope:
    platform_row = _get_platform_by_uuid_or_404(db, platform_uuid)
    row = _get_os_by_uuid_or_404(db, platform_row.id, os_uuid)
    row = set_deleted_state(db, row, True)
    return _wrap_identity_rows([row], status_code=200, status=_identity_status_for_row(row))


@router.post("/platforms/{platform_uuid}/oses/{os_uuid}/versions", response_model=IdentityEnvelope, status_code=201)
def create_version(platform_uuid: str, os_uuid: str, payload: IdentityInput, db: Session = Depends(get_db)) -> IdentityEnvelope:
    platform_row = _get_platform_by_uuid_or_404(db, platform_uuid)
    os_row = _get_os_by_uuid_or_404(db, platform_row.id, os_uuid)
    row = _insert_version(db, os_row.id, payload.identity)
    return _wrap_identity(row)


@router.get("/platforms/{platform_uuid}/oses/{os_uuid}/versions", response_model=IdentityEnvelope, status_code=200)
def list_versions(
    platform_uuid: str,
    os_uuid: str,
    include_archived: bool = False,
    include_deleted: bool = False,
    db: Session = Depends(get_db),
) -> IdentityEnvelope:
    platform_row = _get_platform_by_uuid_or_404(db, platform_uuid)
    os_row = _get_os_by_uuid_or_404(db, platform_row.id, os_uuid)
    query = db.query(VersionModel).filter(VersionModel.os_id == os_row.id)
    if not include_archived:
        query = query.filter(VersionModel.is_archived.is_(False))
    if not include_deleted:
        query = query.filter(VersionModel.is_deleted.is_(False))
    rows = query.order_by(VersionModel.created.desc()).all()
    return _wrap_identity_rows(rows, status_code=200)


@router.get("/platforms/{platform_uuid}/oses/{os_uuid}/versions/{version_uuid}", response_model=IdentityEnvelope, status_code=200)
def get_version(platform_uuid: str, os_uuid: str, version_uuid: str, db: Session = Depends(get_db)) -> IdentityEnvelope:
    platform_row = _get_platform_by_uuid_or_404(db, platform_uuid)
    os_row = _get_os_by_uuid_or_404(db, platform_row.id, os_uuid)
    row = _get_version_by_uuid_or_404(db, os_row.id, version_uuid)
    return _wrap_identity_rows([row], status_code=200)


@router.patch(
    "/platforms/{platform_uuid}/oses/{os_uuid}/versions/{version_uuid}",
    response_model=IdentityEnvelope,
    status_code=200,
)
def patch_version(
    platform_uuid: str,
    os_uuid: str,
    version_uuid: str,
    payload: ArchiveStatePatchRequest,
    db: Session = Depends(get_db),
) -> IdentityEnvelope:
    platform_row = _get_platform_by_uuid_or_404(db, platform_uuid)
    os_row = _get_os_by_uuid_or_404(db, platform_row.id, os_uuid)
    row = _get_version_by_uuid_or_404(db, os_row.id, version_uuid)
    row = set_archive_state(db, row, payload.is_archived) if payload.is_archived is not None else row
    return _wrap_identity_rows([row], status_code=200, status=_identity_status_for_row(row))


@router.delete(
    "/platforms/{platform_uuid}/oses/{os_uuid}/versions/{version_uuid}",
    response_model=IdentityEnvelope,
    status_code=200,
)
def delete_version(platform_uuid: str, os_uuid: str, version_uuid: str, db: Session = Depends(get_db)) -> IdentityEnvelope:
    platform_row = _get_platform_by_uuid_or_404(db, platform_uuid)
    os_row = _get_os_by_uuid_or_404(db, platform_row.id, os_uuid)
    row = _get_version_by_uuid_or_404(db, os_row.id, version_uuid)
    row = set_deleted_state(db, row, True)
    return _wrap_identity_rows([row], status_code=200, status=_identity_status_for_row(row))


@router.post(
    "/platforms/{platform_uuid}/oses/{os_uuid}/versions/{version_uuid}/train/url",
    response_model=IngestEnvelope,
    status_code=200,
)
def train_single_url(
    platform_uuid: str,
    os_uuid: str,
    version_uuid: str,
    payload: TrainURLInput,
    db: Session = Depends(get_db),
) -> IngestEnvelope:
    platform_row, os_row, version_row = _resolve_context_or_404(db, platform_uuid, os_uuid, version_uuid)
    summary = ingest_single_url(
        url=payload.url,
        platform=platform_row.identity,
        os=os_row.identity,
        version=version_row.identity,
        source_type=payload.source_type,
        vector_collection=payload.vector_collection,
    )
    return _wrap_ingest(summary)


@router.post(
    "/platforms/{platform_uuid}/oses/{os_uuid}/versions/{version_uuid}/train/urls",
    response_model=IngestEnvelope,
    status_code=200,
)
def train_bulk_urls(
    platform_uuid: str,
    os_uuid: str,
    version_uuid: str,
    payload: TrainBulkURLsInput,
    db: Session = Depends(get_db),
) -> IngestEnvelope:
    platform_row, os_row, version_row = _resolve_context_or_404(db, platform_uuid, os_uuid, version_uuid)
    summary = ingest_bulk_urls(
        urls=payload.urls,
        platform=platform_row.identity,
        os=os_row.identity,
        version=version_row.identity,
        source_type=payload.source_type,
        vector_collection=payload.vector_collection,
    )
    return _wrap_ingest(summary)


@router.post(
    "/platforms/{platform_uuid}/oses/{os_uuid}/versions/{version_uuid}/train/excel",
    response_model=IngestEnvelope,
    status_code=200,
)
def train_excel_endpoint(
    platform_uuid: str,
    os_uuid: str,
    version_uuid: str,
    file: UploadFile = File(...),
    vector_collection: Annotated[str, Form()] = "tech",
    db: Session = Depends(get_db),
) -> IngestEnvelope:
    platform_row, os_row, version_row = _resolve_context_or_404(db, platform_uuid, os_uuid, version_uuid)
    vc = _vector_collection_form_or_400(vector_collection)
    return _run_file_ingestion(
        file=file,
        default_suffix=".xlsx",
        ingest_fn=ingest_excel,
        platform=platform_row.identity,
        os_name=os_row.identity,
        version=version_row.identity,
        vector_collection=vc,
    )


@router.post(
    "/platforms/{platform_uuid}/oses/{os_uuid}/versions/{version_uuid}/train/pdf",
    response_model=IngestEnvelope,
    status_code=200,
)
def train_pdf_endpoint(
    platform_uuid: str,
    os_uuid: str,
    version_uuid: str,
    file: UploadFile = File(...),
    vector_collection: Annotated[str, Form()] = "tech",
    db: Session = Depends(get_db),
) -> IngestEnvelope:
    platform_row, os_row, version_row = _resolve_context_or_404(db, platform_uuid, os_uuid, version_uuid)
    vc = _vector_collection_form_or_400(vector_collection)
    return _run_file_ingestion(
        file=file,
        default_suffix=".pdf",
        ingest_fn=ingest_pdf,
        platform=platform_row.identity,
        os_name=os_row.identity,
        version=version_row.identity,
        vector_collection=vc,
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


@router.patch("/ingestion-usage/{usage_uuid}", response_model=IngestEnvelope, status_code=200)
def patch_ingestion_usage_endpoint(
    usage_uuid: str, payload: UpdateIngestionUsageRequest, db: Session = Depends(get_db)
) -> IngestEnvelope:
    row = _get_usage_by_uuid_or_404(db, usage_uuid)
    row = _patch_usage(db, row, payload)
    return _wrap_ingest_rows([row], status_code=200)


@router.delete("/ingestion-usage/{usage_uuid}", response_model=IngestEnvelope, status_code=200)
def delete_ingestion_usage(usage_uuid: str, db: Session = Depends(get_db)) -> IngestEnvelope:
    row = _get_usage_by_uuid_or_404(db, usage_uuid)
    row = set_deleted_state(db, row, True)
    return _wrap_ingest_rows([row], status_code=200)
