"""Admin API with pragmatic, local helpers."""

import os
import tempfile
from collections.abc import Callable
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Security, UploadFile
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import require_admin_token
from ingestion import (
    IngestSummary,
    ingest_bulk_urls,
    ingest_excel,
    ingest_pdf,
    ingest_scam_bulk_urls,
    ingest_scam_excel,
    ingest_scam_pdf,
    ingest_scam_single_url,
    ingest_single_url,
)
from config.database import get_db
from models.context import OSModel, PlatformModel, VersionModel
from models.ingestion_records import IngestionUsageModel, ScamIngestionModel
from schema.requests import (
    IdentityInput,
    IdentityEnvelope,
    IdentityOutput,
    ScamTrainBulkURLsInput,
    ScamTrainURLInput,
    TrainBulkURLsInput,
    TrainURLInput,
    UpdateIngestionUsageRequest,
)
from schema.ingestion import (
    IngestEnvelope,
    IngestResponse,
    ScamIngestionEnvelope,
    ScamIngestionItem,
)

router = APIRouter(dependencies=[Security(require_admin_token)])


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


def _get_operating_system_by_uuid_or_404(db: Session, platform_id: int, operating_system_uuid: str) -> OSModel:
    parsed = _parse_uuid_or_400(operating_system_uuid)
    row = db.query(OSModel).filter(OSModel.platform_id == platform_id, OSModel.uuid == parsed).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Operating system not found under platform")
    return row


def _get_version_by_uuid_or_404(db: Session, operating_system_id: int, version_uuid: str) -> VersionModel:
    parsed = _parse_uuid_or_400(version_uuid)
    row = db.query(VersionModel).filter(VersionModel.os_id == operating_system_id, VersionModel.uuid == parsed).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Version not found under platform and operating system")
    return row


def _resolve_context_or_404(
    db: Session, platform_uuid: str, operating_system_uuid: str, version_uuid: str
) -> tuple[PlatformModel, OSModel, VersionModel]:
    platform_row = _get_platform_by_uuid_or_404(db, platform_uuid)
    operating_system = _get_operating_system_by_uuid_or_404(db, platform_row.id, operating_system_uuid)
    version_row = _get_version_by_uuid_or_404(db, operating_system.id, version_uuid)
    return platform_row, operating_system, version_row


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


def _insert_operating_system(db: Session, platform_id: int, identity: str) -> OSModel:
    try:
        row = OSModel(platform_id=platform_id, identity=identity)
        db.add(row)
        db.commit()
        db.refresh(row)
        return row
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Operating system already exists under platform") from exc


def _insert_version(db: Session, operating_system_id: int, identity: str) -> VersionModel:
    try:
        row = VersionModel(os_id=operating_system_id, identity=identity)
        db.add(row)
        db.commit()
        db.refresh(row)
        return row
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Version already exists under platform and operating system") from exc


def set_deleted_state(db: Session, row: object, value: bool = True):
    if getattr(row, "is_deleted") == value:
        return row
    setattr(row, "is_deleted", value)
    db.commit()
    db.refresh(row)
    return row


def _patch_usage(db: Session, row: IngestionUsageModel, payload: UpdateIngestionUsageRequest) -> IngestionUsageModel:
    changed = False
    if payload.status is not None:
        value = payload.status.value
        if row.status != value:
            row.status = value
            changed = True
    if changed:
        db.commit()
        db.refresh(row)
    return row


def _identity_status_for_row(row: PlatformModel | OSModel | VersionModel) -> str:
    if row.is_deleted:
        return "deleted"
    return "completed"


def run_scam_training_file_ingestion(
    file: UploadFile,
    default_suffix: str,
    ingest_fn: Callable[..., IngestSummary],
) -> IngestEnvelope:
    suffix = os.path.splitext(file.filename or "")[1] or default_suffix
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            temp_path = tmp.name
            tmp.write(file.file.read())
        summary = ingest_fn(temp_path)
        return _wrap_ingest(summary)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


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
                is_deleted=row.is_deleted,
            )
            for row in rows
        ],
    )


def _scam_row_to_item(row: ScamIngestionModel) -> ScamIngestionItem:
    return ScamIngestionItem(
        uuid=row.uuid,
        source_key=row.source_key,
        source_type=row.source_type,
        status=row.status,
        processed=row.processed,
        skipped=row.skipped,
        failed=row.failed,
        chunks=row.chunks,
        tokens_used=row.tokens_used,
        cost_usd=row.cost_usd,
        created=row.created.isoformat() if row.created else None,
        modified=row.modified.isoformat() if row.modified else None,
        is_deleted=row.is_deleted,
    )


def _wrap_scam_rows(rows: list[ScamIngestionModel], status_code: int = 200) -> ScamIngestionEnvelope:
    return ScamIngestionEnvelope(status_code=status_code, data=[_scam_row_to_item(r) for r in rows])


@router.post("/platforms", response_model=IdentityEnvelope, status_code=201)
def create_platform(payload: IdentityInput, db: Session = Depends(get_db)) -> IdentityEnvelope:
    row = _insert_platform(db, payload.identity)
    return _wrap_identity(row)


@router.get("/platforms", response_model=IdentityEnvelope, status_code=200)
def list_platforms(
    include_deleted: bool = False,
    db: Session = Depends(get_db),
) -> IdentityEnvelope:
    query = db.query(PlatformModel)
    if not include_deleted:
        query = query.filter(PlatformModel.is_deleted.is_(False))
    rows = query.order_by(PlatformModel.created.desc()).all()
    return _wrap_identity_rows(rows, status_code=200)


@router.get("/platforms/{platform_uuid}", response_model=IdentityEnvelope, status_code=200)
def get_platform(platform_uuid: str, db: Session = Depends(get_db)) -> IdentityEnvelope:
    row = _get_platform_by_uuid_or_404(db, platform_uuid)
    return _wrap_identity_rows([row], status_code=200)


@router.delete("/platforms/{platform_uuid}", response_model=IdentityEnvelope, status_code=200)
def delete_platform(platform_uuid: str, db: Session = Depends(get_db)) -> IdentityEnvelope:
    row = _get_platform_by_uuid_or_404(db, platform_uuid)
    row = set_deleted_state(db, row, True)
    return _wrap_identity_rows([row], status_code=200, status=_identity_status_for_row(row))


@router.post("/platforms/{platform_uuid}/operating-systems", response_model=IdentityEnvelope, status_code=201)
def create_operating_system(
    platform_uuid: str, payload: IdentityInput, db: Session = Depends(get_db)
) -> IdentityEnvelope:
    platform_row = _get_platform_by_uuid_or_404(db, platform_uuid)
    row = _insert_operating_system(db, platform_row.id, payload.identity)
    return _wrap_identity(row)


@router.get("/platforms/{platform_uuid}/operating-systems", response_model=IdentityEnvelope, status_code=200)
def list_operating_systems(
    platform_uuid: str,
    include_deleted: bool = False,
    db: Session = Depends(get_db),
) -> IdentityEnvelope:
    platform_row = _get_platform_by_uuid_or_404(db, platform_uuid)
    query = db.query(OSModel).filter(OSModel.platform_id == platform_row.id)
    if not include_deleted:
        query = query.filter(OSModel.is_deleted.is_(False))
    rows = query.order_by(OSModel.created.desc()).all()
    return _wrap_identity_rows(rows, status_code=200)


@router.get(
    "/platforms/{platform_uuid}/operating-systems/{operating_system_uuid}",
    response_model=IdentityEnvelope,
    status_code=200,
)
def get_operating_system(
    platform_uuid: str, operating_system_uuid: str, db: Session = Depends(get_db)
) -> IdentityEnvelope:
    platform_row = _get_platform_by_uuid_or_404(db, platform_uuid)
    row = _get_operating_system_by_uuid_or_404(db, platform_row.id, operating_system_uuid)
    return _wrap_identity_rows([row], status_code=200)


@router.delete(
    "/platforms/{platform_uuid}/operating-systems/{operating_system_uuid}",
    response_model=IdentityEnvelope,
    status_code=200,
)
def delete_operating_system(
    platform_uuid: str, operating_system_uuid: str, db: Session = Depends(get_db)
) -> IdentityEnvelope:
    platform_row = _get_platform_by_uuid_or_404(db, platform_uuid)
    row = _get_operating_system_by_uuid_or_404(db, platform_row.id, operating_system_uuid)
    row = set_deleted_state(db, row, True)
    return _wrap_identity_rows([row], status_code=200, status=_identity_status_for_row(row))


@router.post(
    "/platforms/{platform_uuid}/operating-systems/{operating_system_uuid}/versions",
    response_model=IdentityEnvelope,
    status_code=201,
)
def create_version(
    platform_uuid: str,
    operating_system_uuid: str,
    payload: IdentityInput,
    db: Session = Depends(get_db),
) -> IdentityEnvelope:
    platform_row = _get_platform_by_uuid_or_404(db, platform_uuid)
    operating_system = _get_operating_system_by_uuid_or_404(db, platform_row.id, operating_system_uuid)
    row = _insert_version(db, operating_system.id, payload.identity)
    return _wrap_identity(row)


@router.get(
    "/platforms/{platform_uuid}/operating-systems/{operating_system_uuid}/versions",
    response_model=IdentityEnvelope,
    status_code=200,
)
def list_versions(
    platform_uuid: str,
    operating_system_uuid: str,
    include_deleted: bool = False,
    db: Session = Depends(get_db),
) -> IdentityEnvelope:
    platform_row = _get_platform_by_uuid_or_404(db, platform_uuid)
    operating_system = _get_operating_system_by_uuid_or_404(db, platform_row.id, operating_system_uuid)
    query = db.query(VersionModel).filter(VersionModel.os_id == operating_system.id)
    if not include_deleted:
        query = query.filter(VersionModel.is_deleted.is_(False))
    rows = query.order_by(VersionModel.created.desc()).all()
    return _wrap_identity_rows(rows, status_code=200)


@router.get(
    "/platforms/{platform_uuid}/operating-systems/{operating_system_uuid}/versions/{version_uuid}",
    response_model=IdentityEnvelope,
    status_code=200,
)
def get_version(
    platform_uuid: str,
    operating_system_uuid: str,
    version_uuid: str,
    db: Session = Depends(get_db),
) -> IdentityEnvelope:
    platform_row = _get_platform_by_uuid_or_404(db, platform_uuid)
    operating_system = _get_operating_system_by_uuid_or_404(db, platform_row.id, operating_system_uuid)
    row = _get_version_by_uuid_or_404(db, operating_system.id, version_uuid)
    return _wrap_identity_rows([row], status_code=200)


@router.delete(
    "/platforms/{platform_uuid}/operating-systems/{operating_system_uuid}/versions/{version_uuid}",
    response_model=IdentityEnvelope,
    status_code=200,
)
def delete_version(
    platform_uuid: str,
    operating_system_uuid: str,
    version_uuid: str,
    db: Session = Depends(get_db),
) -> IdentityEnvelope:
    platform_row = _get_platform_by_uuid_or_404(db, platform_uuid)
    operating_system = _get_operating_system_by_uuid_or_404(db, platform_row.id, operating_system_uuid)
    row = _get_version_by_uuid_or_404(db, operating_system.id, version_uuid)
    row = set_deleted_state(db, row, True)
    return _wrap_identity_rows([row], status_code=200, status=_identity_status_for_row(row))


@router.post(
    "/platforms/{platform_uuid}/operating-systems/{operating_system_uuid}/versions/{version_uuid}/train/url",
    response_model=IngestEnvelope,
    status_code=200,
)
def train_single_url(
    platform_uuid: str,
    operating_system_uuid: str,
    version_uuid: str,
    payload: TrainURLInput,
    db: Session = Depends(get_db),
) -> IngestEnvelope:
    platform_row, operating_system, version_row = _resolve_context_or_404(
        db, platform_uuid, operating_system_uuid, version_uuid
    )
    summary = ingest_single_url(
        url=payload.url,
        platform=platform_row.identity,
        os=operating_system.identity,
        version=version_row.identity,
        source_type=payload.source_type,
    )
    return _wrap_ingest(summary)


@router.post(
    "/platforms/{platform_uuid}/operating-systems/{operating_system_uuid}/versions/{version_uuid}/train/urls",
    response_model=IngestEnvelope,
    status_code=200,
)
def train_bulk_urls(
    platform_uuid: str,
    operating_system_uuid: str,
    version_uuid: str,
    payload: TrainBulkURLsInput,
    db: Session = Depends(get_db),
) -> IngestEnvelope:
    platform_row, operating_system, version_row = _resolve_context_or_404(
        db, platform_uuid, operating_system_uuid, version_uuid
    )
    summary = ingest_bulk_urls(
        urls=payload.urls,
        platform=platform_row.identity,
        os=operating_system.identity,
        version=version_row.identity,
        source_type=payload.source_type,
    )
    return _wrap_ingest(summary)


@router.post(
    "/platforms/{platform_uuid}/operating-systems/{operating_system_uuid}/versions/{version_uuid}/train/excel",
    response_model=IngestEnvelope,
    status_code=200,
)
def train_excel(
    platform_uuid: str,
    operating_system_uuid: str,
    version_uuid: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> IngestEnvelope:
    platform_row, operating_system, version_row = _resolve_context_or_404(
        db, platform_uuid, operating_system_uuid, version_uuid
    )
    return _run_file_ingestion(
        file=file,
        default_suffix=".xlsx",
        ingest_fn=ingest_excel,
        platform=platform_row.identity,
        os_name=operating_system.identity,
        version=version_row.identity,
    )


@router.post(
    "/platforms/{platform_uuid}/operating-systems/{operating_system_uuid}/versions/{version_uuid}/train/pdf",
    response_model=IngestEnvelope,
    status_code=200,
)
def train_pdf(
    platform_uuid: str,
    operating_system_uuid: str,
    version_uuid: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> IngestEnvelope:
    platform_row, operating_system, version_row = _resolve_context_or_404(
        db, platform_uuid, operating_system_uuid, version_uuid
    )
    return _run_file_ingestion(
        file=file,
        default_suffix=".pdf",
        ingest_fn=ingest_pdf,
        platform=platform_row.identity,
        os_name=operating_system.identity,
        version=version_row.identity,
    )


@router.get("/scam/ingestions", response_model=ScamIngestionEnvelope, status_code=200)
def list_scam_ingestions(
    skip: int = 0,
    limit: int = 100,
    include_deleted: bool = False,
    db: Session = Depends(get_db),
) -> ScamIngestionEnvelope:
    query = db.query(ScamIngestionModel)
    if not include_deleted:
        query = query.filter(ScamIngestionModel.is_deleted.is_(False))
    rows = (
        query.order_by(ScamIngestionModel.created.desc())
        .offset(max(0, skip))
        .limit(min(500, max(1, limit)))
        .all()
    )
    return _wrap_scam_rows(rows, status_code=200)


@router.post("/scam/train/url", response_model=IngestEnvelope, status_code=200)
def train_scam_single_url(payload: ScamTrainURLInput) -> IngestEnvelope:
    summary = ingest_scam_single_url(url=payload.url, source_type=payload.source_type)
    return _wrap_ingest(summary)


@router.post("/scam/train/urls", response_model=IngestEnvelope, status_code=200)
def train_scam_bulk_urls(payload: ScamTrainBulkURLsInput) -> IngestEnvelope:
    summary = ingest_scam_bulk_urls(urls=payload.urls, source_type=payload.source_type)
    return _wrap_ingest(summary)


@router.post("/scam/train/excel", response_model=IngestEnvelope, status_code=200)
def train_scam_excel(file: UploadFile = File(...)) -> IngestEnvelope:
    return run_scam_training_file_ingestion(file=file, default_suffix=".xlsx", ingest_fn=ingest_scam_excel)


@router.post("/scam/train/pdf", response_model=IngestEnvelope, status_code=200)
def train_scam_pdf(file: UploadFile = File(...)) -> IngestEnvelope:
    return run_scam_training_file_ingestion(file=file, default_suffix=".pdf", ingest_fn=ingest_scam_pdf)


@router.get("/ingestion-usage", response_model=IngestEnvelope, status_code=200)
def list_ingestion_usage(
    include_deleted: bool = False,
    db: Session = Depends(get_db),
) -> IngestEnvelope:
    query = db.query(IngestionUsageModel)
    if not include_deleted:
        query = query.filter(IngestionUsageModel.is_deleted.is_(False))
    rows = query.order_by(IngestionUsageModel.created.desc()).all()
    return _wrap_ingest_rows(rows, status_code=200)


@router.get("/ingestion-usage/{usage_uuid}", response_model=IngestEnvelope, status_code=200)
def get_ingestion_usage(usage_uuid: str, db: Session = Depends(get_db)) -> IngestEnvelope:
    row = _get_usage_by_uuid_or_404(db, usage_uuid)
    return _wrap_ingest_rows([row], status_code=200)


@router.patch("/ingestion-usage/{usage_uuid}", response_model=IngestEnvelope, status_code=200)
def patch_ingestion_usage(
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
