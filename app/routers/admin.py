"""Admin API routes."""

from fastapi import APIRouter, Depends, File, HTTPException, Security, UploadFile
from sqlalchemy import and_, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import require_admin_token
from app.config.database import get_db
from app.ingestion import (
    ingest_bulk_urls,
    ingest_excel,
    ingest_pdf,
    ingest_scam_bulk_urls,
    ingest_scam_excel,
    ingest_scam_pdf,
    ingest_scam_single_url,
    ingest_single_url,
)
from app.models.context import OSModel, PlatformModel, VersionModel
from app.models.ingestion_records import IngestionUsageModel, ScamIngestionModel
from app.schema.ingestion import IngestEnvelope, IngestResponse, ScamIngestionEnvelope, ScamIngestionItem
from app.schema.requests import (
    IdentityEnvelope,
    IdentityInput,
    ScamTrainBulkURLsInput,
    ScamTrainURLInput,
    TrainBulkURLsInput,
    TrainURLInput,
    UpdateIngestionUsageRequest,
)

from .common import (
    parse_uuid_or_bad_request,
    resolve_platform_os_version_or_404,
    resolve_platform_or_404,
    resolve_platform_os_or_404,
    soft_delete_and_refresh,
    to_identity_output,
    to_ingest_response,
    with_temp_upload_file,
)

router = APIRouter(dependencies=[Security(require_admin_token)])


def _to_usage_response(row: IngestionUsageModel) -> IngestResponse:
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


@router.post("/platforms/cud", response_model=IdentityEnvelope, status_code=201)
def create_platform(payload: IdentityInput, db: Session = Depends(get_db)) -> IdentityEnvelope:
    try:
        row = PlatformModel(identity=payload.identity)
        db.add(row)
        db.commit()
        db.refresh(row)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Platform already exists") from exc
    return IdentityEnvelope(status_code=201, data=[to_identity_output(row)])


@router.get("/platforms/list", response_model=IdentityEnvelope, status_code=200)
def list_platforms(include_deleted: bool = False, db: Session = Depends(get_db)) -> IdentityEnvelope:
    query = db.query(PlatformModel)
    if not include_deleted:
        query = query.filter(PlatformModel.is_deleted.is_(False))
    rows = query.order_by(PlatformModel.created.desc()).all()
    return IdentityEnvelope(status_code=200, data=[to_identity_output(row) for row in rows])


@router.get("/platforms/list/{platform_uuid}", response_model=IdentityEnvelope, status_code=200)
def get_platform(platform_uuid: str, db: Session = Depends(get_db)) -> IdentityEnvelope:
    row = resolve_platform_or_404(db, platform_uuid)
    return IdentityEnvelope(status_code=200, data=[to_identity_output(row)])


@router.delete("/platforms/cud/{platform_uuid}", response_model=IdentityEnvelope, status_code=200)
def delete_platform(platform_uuid: str, db: Session = Depends(get_db)) -> IdentityEnvelope:
    row = resolve_platform_or_404(db, platform_uuid)
    status = soft_delete_and_refresh(db, row)
    return IdentityEnvelope(status_code=200, data=[to_identity_output(row, status=status)])


@router.post("/platforms/{platform_uuid}/operating-systems/cud", response_model=IdentityEnvelope, status_code=201)
def create_operating_system(
    platform_uuid: str, payload: IdentityInput, db: Session = Depends(get_db)
) -> IdentityEnvelope:
    platform_row = resolve_platform_or_404(db, platform_uuid)
    try:
        row = OSModel(platform_id=platform_row.id, identity=payload.identity)
        db.add(row)
        db.commit()
        db.refresh(row)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Operating system already exists under platform") from exc
    return IdentityEnvelope(status_code=201, data=[to_identity_output(row)])


@router.get("/platforms/{platform_uuid}/operating-systems/list", response_model=IdentityEnvelope, status_code=200)
def list_operating_systems(
    platform_uuid: str, include_deleted: bool = False, db: Session = Depends(get_db)
) -> IdentityEnvelope:
    platform_row = resolve_platform_or_404(db, platform_uuid)
    query = db.query(OSModel).filter(OSModel.platform_id == platform_row.id)
    if not include_deleted:
        query = query.filter(OSModel.is_deleted.is_(False))
    rows = query.order_by(OSModel.created.desc()).all()
    return IdentityEnvelope(status_code=200, data=[to_identity_output(row) for row in rows])


@router.get("/platforms/{platform_uuid}/operating-systems/list/{operating_system_uuid}", response_model=IdentityEnvelope)
def get_operating_system(
    platform_uuid: str, operating_system_uuid: str, db: Session = Depends(get_db)
) -> IdentityEnvelope:
    _, row = resolve_platform_os_or_404(db, platform_uuid, operating_system_uuid)
    return IdentityEnvelope(status_code=200, data=[to_identity_output(row)])


@router.delete("/platforms/{platform_uuid}/operating-systems/cud/{operating_system_uuid}", response_model=IdentityEnvelope)
def delete_operating_system(
    platform_uuid: str, operating_system_uuid: str, db: Session = Depends(get_db)
) -> IdentityEnvelope:
    _, row = resolve_platform_os_or_404(db, platform_uuid, operating_system_uuid)
    status = soft_delete_and_refresh(db, row)
    return IdentityEnvelope(status_code=200, data=[to_identity_output(row, status=status)])


@router.post(
    "/platforms/{platform_uuid}/operating-systems/{operating_system_uuid}/versions/cud",
    response_model=IdentityEnvelope,
    status_code=201,
)
def create_version(
    platform_uuid: str, operating_system_uuid: str, payload: IdentityInput, db: Session = Depends(get_db)
) -> IdentityEnvelope:
    _, operating_system = resolve_platform_os_or_404(db, platform_uuid, operating_system_uuid)
    try:
        row = VersionModel(os_id=operating_system.id, identity=payload.identity)
        db.add(row)
        db.commit()
        db.refresh(row)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Version already exists under platform and operating system") from exc
    return IdentityEnvelope(status_code=201, data=[to_identity_output(row)])


@router.get(
    "/platforms/{platform_uuid}/operating-systems/{operating_system_uuid}/versions/list",
    response_model=IdentityEnvelope,
)
def list_versions(
    platform_uuid: str, operating_system_uuid: str, include_deleted: bool = False, db: Session = Depends(get_db)
) -> IdentityEnvelope:
    _, operating_system = resolve_platform_os_or_404(db, platform_uuid, operating_system_uuid)
    query = db.query(VersionModel).filter(VersionModel.os_id == operating_system.id)
    if not include_deleted:
        query = query.filter(VersionModel.is_deleted.is_(False))
    rows = query.order_by(VersionModel.created.desc()).all()
    return IdentityEnvelope(status_code=200, data=[to_identity_output(row) for row in rows])


@router.get(
    "/platforms/{platform_uuid}/operating-systems/{operating_system_uuid}/versions/list/{version_uuid}",
    response_model=IdentityEnvelope,
)
def get_version(
    platform_uuid: str, operating_system_uuid: str, version_uuid: str, db: Session = Depends(get_db)
) -> IdentityEnvelope:
    _, _, row = resolve_platform_os_version_or_404(db, platform_uuid, operating_system_uuid, version_uuid)
    return IdentityEnvelope(status_code=200, data=[to_identity_output(row)])


@router.delete(
    "/platforms/{platform_uuid}/operating-systems/{operating_system_uuid}/versions/cud/{version_uuid}",
    response_model=IdentityEnvelope,
)
def delete_version(
    platform_uuid: str, operating_system_uuid: str, version_uuid: str, db: Session = Depends(get_db)
) -> IdentityEnvelope:
    _, _, row = resolve_platform_os_version_or_404(db, platform_uuid, operating_system_uuid, version_uuid)
    status = soft_delete_and_refresh(db, row)
    return IdentityEnvelope(status_code=200, data=[to_identity_output(row, status=status)])


@router.post(
    "/platforms/{platform_uuid}/operating-systems/{operating_system_uuid}/versions/{version_uuid}/train/url",
    response_model=IngestEnvelope,
)
def train_single_url(
    platform_uuid: str,
    operating_system_uuid: str,
    version_uuid: str,
    payload: TrainURLInput,
    db: Session = Depends(get_db),
) -> IngestEnvelope:
    platform_row, operating_system, version_row = resolve_platform_os_version_or_404(
        db, platform_uuid, operating_system_uuid, version_uuid
    )
    summary = ingest_single_url(
        url=payload.url,
        platform=platform_row.identity,
        operating_system=operating_system.identity,
        version=version_row.identity,
        source_type=payload.source_type,
    )
    return IngestEnvelope(status_code=200, data=[to_ingest_response(summary)])


@router.post(
    "/platforms/{platform_uuid}/operating-systems/{operating_system_uuid}/versions/{version_uuid}/train/urls",
    response_model=IngestEnvelope,
)
def train_bulk_urls(
    platform_uuid: str,
    operating_system_uuid: str,
    version_uuid: str,
    payload: TrainBulkURLsInput,
    db: Session = Depends(get_db),
) -> IngestEnvelope:
    platform_row, operating_system, version_row = resolve_platform_os_version_or_404(
        db, platform_uuid, operating_system_uuid, version_uuid
    )
    summary = ingest_bulk_urls(
        urls=payload.urls,
        platform=platform_row.identity,
        operating_system=operating_system.identity,
        version=version_row.identity,
        source_type=payload.source_type,
    )
    return IngestEnvelope(status_code=200, data=[to_ingest_response(summary)])


@router.post(
    "/platforms/{platform_uuid}/operating-systems/{operating_system_uuid}/versions/{version_uuid}/train/excel",
    response_model=IngestEnvelope,
)
def train_excel(
    platform_uuid: str,
    operating_system_uuid: str,
    version_uuid: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> IngestEnvelope:
    platform_row, operating_system, version_row = resolve_platform_os_version_or_404(
        db, platform_uuid, operating_system_uuid, version_uuid
    )
    with with_temp_upload_file(file, ".xlsx") as temp_path:
        summary = ingest_excel(
            temp_path,
            platform=platform_row.identity,
            operating_system=operating_system.identity,
            version=version_row.identity,
        )
    return IngestEnvelope(status_code=200, data=[to_ingest_response(summary)])


@router.post(
    "/platforms/{platform_uuid}/operating-systems/{operating_system_uuid}/versions/{version_uuid}/train/pdf",
    response_model=IngestEnvelope,
)
def train_pdf(
    platform_uuid: str,
    operating_system_uuid: str,
    version_uuid: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> IngestEnvelope:
    platform_row, operating_system, version_row = resolve_platform_os_version_or_404(
        db, platform_uuid, operating_system_uuid, version_uuid
    )
    with with_temp_upload_file(file, ".pdf") as temp_path:
        summary = ingest_pdf(
            temp_path,
            platform=platform_row.identity,
            operating_system=operating_system.identity,
            version=version_row.identity,
        )
    return IngestEnvelope(status_code=200, data=[to_ingest_response(summary)])


@router.get("/scam/ingestions/list", response_model=ScamIngestionEnvelope, status_code=200)
def list_scam_ingestions(
    skip: int = 0, limit: int = 100, include_deleted: bool = False, db: Session = Depends(get_db)
) -> ScamIngestionEnvelope:
    query = db.query(ScamIngestionModel)
    if not include_deleted:
        query = query.filter(ScamIngestionModel.is_deleted.is_(False))
    rows = query.order_by(ScamIngestionModel.created.desc()).offset(max(0, skip)).limit(min(500, max(1, limit))).all()
    return ScamIngestionEnvelope(
        status_code=200,
        data=[
            ScamIngestionItem(
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
            for row in rows
        ],
    )


@router.post("/scam/train/cud/url", response_model=IngestEnvelope, status_code=200)
def train_scam_single_url(payload: ScamTrainURLInput) -> IngestEnvelope:
    summary = ingest_scam_single_url(url=payload.url, source_type=payload.source_type)
    return IngestEnvelope(status_code=200, data=[to_ingest_response(summary)])


@router.post("/scam/train/cud/urls", response_model=IngestEnvelope, status_code=200)
def train_scam_bulk_urls(payload: ScamTrainBulkURLsInput) -> IngestEnvelope:
    summary = ingest_scam_bulk_urls(urls=payload.urls, source_type=payload.source_type)
    return IngestEnvelope(status_code=200, data=[to_ingest_response(summary)])


@router.post("/scam/train/cud/excel", response_model=IngestEnvelope, status_code=200)
def train_scam_excel(file: UploadFile = File(...)) -> IngestEnvelope:
    with with_temp_upload_file(file, ".xlsx") as temp_path:
        summary = ingest_scam_excel(temp_path)
    return IngestEnvelope(status_code=200, data=[to_ingest_response(summary)])


@router.post("/scam/train/cud/pdf", response_model=IngestEnvelope, status_code=200)
def train_scam_pdf(file: UploadFile = File(...)) -> IngestEnvelope:
    with with_temp_upload_file(file, ".pdf") as temp_path:
        summary = ingest_scam_pdf(temp_path)
    return IngestEnvelope(status_code=200, data=[to_ingest_response(summary)])


@router.get("/ingestion-usage", response_model=IngestEnvelope, status_code=200)
def list_ingestion_usage(include_deleted: bool = False, db: Session = Depends(get_db)) -> IngestEnvelope:
    query = db.query(IngestionUsageModel)
    if not include_deleted:
        query = query.filter(IngestionUsageModel.is_deleted.is_(False))
    rows = query.order_by(IngestionUsageModel.created.desc()).all()
    return IngestEnvelope(status_code=200, data=[_to_usage_response(row) for row in rows])


@router.get("/ingestion-usage/summary", response_model=IngestEnvelope, status_code=200)
def list_ingestion_usage_summary(include_deleted: bool = False, db: Session = Depends(get_db)) -> IngestEnvelope:
    grouped_query = db.query(
        IngestionUsageModel.source.label("source"),
        IngestionUsageModel.source_type.label("source_type"),
        IngestionUsageModel.platform.label("platform"),
        IngestionUsageModel.os.label("os"),
        IngestionUsageModel.version.label("version"),
        func.max(IngestionUsageModel.modified).label("latest_modified"),
    )
    if not include_deleted:
        grouped_query = grouped_query.filter(IngestionUsageModel.is_deleted.is_(False))
    latest_per_context = grouped_query.group_by(
        IngestionUsageModel.source,
        IngestionUsageModel.source_type,
        IngestionUsageModel.platform,
        IngestionUsageModel.os,
        IngestionUsageModel.version,
    ).subquery()

    rows = (
        db.query(IngestionUsageModel)
        .join(
            latest_per_context,
            and_(
                IngestionUsageModel.source == latest_per_context.c.source,
                IngestionUsageModel.source_type == latest_per_context.c.source_type,
                IngestionUsageModel.platform == latest_per_context.c.platform,
                IngestionUsageModel.os == latest_per_context.c.os,
                IngestionUsageModel.version == latest_per_context.c.version,
                IngestionUsageModel.modified == latest_per_context.c.latest_modified,
            ),
        )
        .order_by(IngestionUsageModel.modified.desc())
        .all()
    )
    return IngestEnvelope(status_code=200, data=[_to_usage_response(row) for row in rows])


@router.get("/ingestion-usage/{usage_uuid}", response_model=IngestEnvelope, status_code=200)
def get_ingestion_usage(usage_uuid: str, db: Session = Depends(get_db)) -> IngestEnvelope:
    parsed = parse_uuid_or_bad_request(usage_uuid)
    row = db.query(IngestionUsageModel).filter(IngestionUsageModel.uuid == parsed).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Ingestion usage not found")
    return IngestEnvelope(status_code=200, data=[_to_usage_response(row)])


@router.patch("/ingestion-usage/{usage_uuid}", response_model=IngestEnvelope, status_code=200)
def patch_ingestion_usage(
    usage_uuid: str, payload: UpdateIngestionUsageRequest, db: Session = Depends(get_db)
) -> IngestEnvelope:
    parsed = parse_uuid_or_bad_request(usage_uuid)
    row = db.query(IngestionUsageModel).filter(IngestionUsageModel.uuid == parsed).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Ingestion usage not found")
    if payload.status is not None and row.status != payload.status.value:
        row.status = payload.status.value
        db.commit()
        db.refresh(row)
    return IngestEnvelope(status_code=200, data=[_to_usage_response(row)])


@router.delete("/ingestion-usage/{usage_uuid}", response_model=IngestEnvelope, status_code=200)
def delete_ingestion_usage(usage_uuid: str, db: Session = Depends(get_db)) -> IngestEnvelope:
    parsed = parse_uuid_or_bad_request(usage_uuid)
    row = db.query(IngestionUsageModel).filter(IngestionUsageModel.uuid == parsed).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Ingestion usage not found")
    soft_delete_and_refresh(db, row)
    return IngestEnvelope(status_code=200, data=[_to_usage_response(row)])
