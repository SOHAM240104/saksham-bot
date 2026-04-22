"""Admin API routes."""

from uuid import uuid4

import boto3
from fastapi import APIRouter, Depends, File, Form, HTTPException, Security, UploadFile
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from fastapi import BackgroundTasks
from app.auth import require_admin_token
from app.config.base import get_db
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
from app.models.chatbot.context import OSModel, PlatformModel, VersionModel
from app.models.chatbot.ingestion_records import IngestionUsageModel, ScamIngestionModel, TechFile
from app.settings import TECH_S3_BUCKET, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION_NAME
from app.schema.ingestion import IngestEnvelope, IngestResponse, ScamIngestionEnvelope, ScamIngestionItem,PDFUploadInitEnvelope,PDFUploadInitResponse
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
    soft_delete_and_refresh,
    to_identity_output,
    to_ingest_response,
    with_temp_upload_file,
)

router = APIRouter(dependencies=[Security(require_admin_token)])

s3_kwargs = {}
if AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY:
    s3_kwargs["aws_access_key_id"] = AWS_ACCESS_KEY_ID
    s3_kwargs["aws_secret_access_key"] = AWS_SECRET_ACCESS_KEY
if AWS_REGION_NAME:
    s3_kwargs["region_name"] = AWS_REGION_NAME

s3 = boto3.client("s3", **s3_kwargs)


def _to_usage_response(row: IngestionUsageModel) -> IngestResponse:
    return IngestResponse(
        uuid=row.uuid,
        status=row.status,
        tokens_used=row.tokens_used,
        cost_usd=row.cost_usd,
        created=row.created.isoformat() if row.created else None,
        modified=row.modified.isoformat() if row.modified else None,
    )


def _normalize_identity(value: str) -> str:
    return value.strip().lower()


def _get_platform_or_404(db: Session, platform: str) -> PlatformModel:
    normalized = _normalize_identity(platform)
    row = db.query(PlatformModel).filter(PlatformModel.identity == normalized).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Platform not found")
    return row


def _get_operating_system_or_404(db: Session, platform_row: PlatformModel, operating_system: str) -> OSModel:
    normalized = _normalize_identity(operating_system)
    row = db.query(OSModel).filter(OSModel.platform_id == platform_row.id, OSModel.identity == normalized).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Operating system not found")
    return row


def _get_version_or_404(db: Session,platform_row: PlatformModel, operating_system_row: OSModel, version: str) -> VersionModel:
    normalized = _normalize_identity(version)
    row = db.query(VersionModel).filter(VersionModel.os_id == operating_system_row.id,VersionModel.platform_id == platform_row.id,VersionModel.identity == normalized).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Version not found")
    return row


def _get_ingestion_usage_by_url_or_404(db: Session, url: str) -> IngestionUsageModel:
    row = (
        db.query(IngestionUsageModel)
        .filter(IngestionUsageModel.url == url)
        .order_by(IngestionUsageModel.modified.desc())
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Ingestion usage not found")
    return row


def _resolve_context_strict(
    db: Session, platform: str, operating_system: str, version: str
) -> tuple[PlatformModel, OSModel, VersionModel]:
    platform_row = _get_platform_or_404(db, platform)
    operating_system_row = _get_operating_system_or_404(db, platform_row, operating_system)
    version_row = _get_version_or_404(db, platform_row, operating_system_row, version)
    return platform_row, operating_system_row, version_row


@router.post("/platforms/cud", response_model=IdentityEnvelope, status_code=201)
def create_platform(payload: IdentityInput, db: Session = Depends(get_db)) -> IdentityEnvelope:
    try:
        row = PlatformModel(identity=_normalize_identity(payload.identity))
        db.add(row)
        db.commit()
        db.refresh(row)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=f"{exc}") from exc
    return IdentityEnvelope(status_code=201, data=[to_identity_output(row)])


@router.get("/platforms/list", response_model=IdentityEnvelope, status_code=200)
def list_platforms(include_deleted: bool = False, db: Session = Depends(get_db)) -> IdentityEnvelope:
    query = db.query(PlatformModel)
    if not include_deleted:
        query = query.filter(PlatformModel.is_deleted.is_(False))
    rows = query.order_by(PlatformModel.created.desc()).all()
    return IdentityEnvelope(status_code=200, data=[to_identity_output(row) for row in rows])


@router.get("/platforms/list/{platform}", response_model=IdentityEnvelope, status_code=200)
def get_platform(platform: str, db: Session = Depends(get_db)) -> IdentityEnvelope:
    row = _get_platform_or_404(db, platform)
    return IdentityEnvelope(status_code=200, data=[to_identity_output(row)])


@router.delete("/platforms/cud/{platform}", response_model=IdentityEnvelope, status_code=200)
def delete_platform(platform: str, db: Session = Depends(get_db)) -> IdentityEnvelope:
    row = _get_platform_or_404(db, platform)
    status = soft_delete_and_refresh(db, row)
    return IdentityEnvelope(status_code=200, data=[to_identity_output(row, status=status)])


@router.post("/platforms/{platform}/operating-systems/cud", response_model=IdentityEnvelope, status_code=201)
def create_operating_system(
    platform: str, payload: IdentityInput, db: Session = Depends(get_db)
) -> IdentityEnvelope:
    platform_row = _get_platform_or_404(db, platform)
    try:
        row = OSModel(platform_id=platform_row.id, identity=_normalize_identity(payload.identity))
        db.add(row)
        db.commit()
        db.refresh(row)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=f"{exc}") from exc
    return IdentityEnvelope(status_code=201, data=[to_identity_output(row)])


@router.get("/platforms/{platform}/operating-systems/list", response_model=IdentityEnvelope, status_code=200)
def list_operating_systems(
    platform: str, include_deleted: bool = False, db: Session = Depends(get_db)
) -> IdentityEnvelope:
    platform_row = _get_platform_or_404(db, platform)
    query = db.query(OSModel).filter(OSModel.platform_id == platform_row.id)
    if not include_deleted:
        query = query.filter(OSModel.is_deleted.is_(False))
    rows = query.order_by(OSModel.created.desc()).all()
    return IdentityEnvelope(status_code=200, data=[to_identity_output(row) for row in rows])


@router.get("/platforms/{platform}/operating-systems/{operating_system}", response_model=IdentityEnvelope)
def get_operating_system(
    platform: str, operating_system: str, db: Session = Depends(get_db)
) -> IdentityEnvelope:
    platform_row = _get_platform_or_404(db, platform)
    row = _get_operating_system_or_404(db, platform_row, operating_system)
    return IdentityEnvelope(status_code=200, data=[to_identity_output(row)])


@router.delete("/platforms/{platform}/operating-systems/cud/{operating_system}", response_model=IdentityEnvelope)
def delete_operating_system(
    platform: str, operating_system: str, db: Session = Depends(get_db)
) -> IdentityEnvelope:
    platform_row = _get_platform_or_404(db, platform)
    row = _get_operating_system_or_404(db, platform_row, operating_system)
    status = soft_delete_and_refresh(db, row)
    return IdentityEnvelope(status_code=200, data=[to_identity_output(row, status=status)])


@router.post(
    "/platforms/{platform}/operating-systems/{operating_system}/versions/cud",
    response_model=IdentityEnvelope,
    status_code=201,
)
def create_version(
    platform: str, operating_system: str, payload: IdentityInput, db: Session = Depends(get_db)
) -> IdentityEnvelope:
    platform_row = _get_platform_or_404(db, platform)
    operating_system_row = _get_operating_system_or_404(db, platform_row, operating_system)
    try:
        row = VersionModel(os_id=operating_system_row.id,platform_id=platform_row.id,
                            identity=_normalize_identity(payload.identity))
        db.add(row)
        db.commit()
        db.refresh(row)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=f'{exc}') from exc
    return IdentityEnvelope(status_code=201, data=[to_identity_output(row)])


@router.get(
    "/platforms/{platform}/operating-systems/{operating_system}/versions/list",
    response_model=IdentityEnvelope,
)
def list_versions(
    platform: str, operating_system: str, include_deleted: bool = False, db: Session = Depends(get_db)
) -> IdentityEnvelope:
    platform_row = _get_platform_or_404(db, platform)
    operating_system_row = _get_operating_system_or_404(db, platform_row, operating_system)
    query = db.query(VersionModel).filter(VersionModel.os_id == operating_system_row.id,VersionModel.platform_id == platform_row.id)
    if not include_deleted:
        query = query.filter(VersionModel.is_deleted.is_(False))
    rows = query.order_by(VersionModel.created.desc()).all()
    return IdentityEnvelope(status_code=200, data=[to_identity_output(row) for row in rows])


@router.get(
    "/platforms/{platform}/operating-systems/{operating_system}/versions/{version}",
    response_model=IdentityEnvelope,
)
def get_version(
    platform: str, operating_system: str, version: str, db: Session = Depends(get_db)
) -> IdentityEnvelope:
    platform_row = _get_platform_or_404(db, platform)
    operating_system_row = _get_operating_system_or_404(db, platform_row, operating_system)
    row = _get_version_or_404(db, platform_row, operating_system_row, version)
    return IdentityEnvelope(status_code=200, data=[to_identity_output(row)])


@router.delete(
    "/platforms/{platform}/operating-systems/{operating_system}/versions/cud/{version}",
    response_model=IdentityEnvelope,
)
def delete_version(
    platform: str, operating_system: str, version: str, db: Session = Depends(get_db)
) -> IdentityEnvelope:
    platform_row = _get_platform_or_404(db, platform)
    operating_system_row = _get_operating_system_or_404(db, platform_row, operating_system)
    row = _get_version_or_404(db, platform_row, operating_system_row, version)
    status = soft_delete_and_refresh(db, row)
    return IdentityEnvelope(status_code=200, data=[to_identity_output(row, status=status)])


@router.post(
    "/train/cud/url",
    response_model=IngestEnvelope,
)
def train_single_url(
    payload: TrainURLInput,
    db: Session = Depends(get_db),
) -> IngestEnvelope:
    platform_row, operating_system, version_row = _resolve_context_strict(
        db,
        platform=payload.platform,
        operating_system=payload.operating_system,
        version=payload.version,
    )
    summary = ingest_single_url(
        url=payload.url,
        platform=platform_row.identity,
        operating_system=operating_system.identity,
        version=version_row.identity,
        source_type=payload.source_type or "url",
    )
    return IngestEnvelope(status_code=200, data=[to_ingest_response(summary)])


@router.post(
    "/train/cud/urls",
    response_model=IngestEnvelope,
)
def train_bulk_urls(
    payload: TrainBulkURLsInput,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> IngestEnvelope:
    platform_row, operating_system, version_row = _resolve_context_strict(
        db,
        platform=payload.platform,
        operating_system=payload.operating_system,
        version=payload.version,
    )
    background_tasks.add_task(ingest_bulk_urls,
        urls=payload.urls,
        platform=platform_row.identity,
        operating_system=operating_system.identity,
        version=version_row.identity,
        source_type=payload.source_type or "url",
    )

    return IngestEnvelope(
    status_code=200,
    data=[
        IngestResponse(
            status="queued"
        )
    ]
)


@router.post(
    "/train/cud/excel",
    response_model=IngestEnvelope,
)
def train_excel(
    file: UploadFile = File(...),
    platform: str = Form(...),
    operating_system: str = Form(...),
    version: str = Form(...),
    db: Session = Depends(get_db),
) -> IngestEnvelope:
    platform_row, operating_system_row, version_row = _resolve_context_strict(
        db,
        platform=platform,
        operating_system=operating_system,
        version=version,
    )
    with with_temp_upload_file(file, ".xlsx") as temp_path:
        summary = ingest_excel(
            temp_path,
            platform=platform_row.identity,
            operating_system=operating_system_row.identity,
            version=version_row.identity,
        )
    return IngestEnvelope(status_code=200, data=[to_ingest_response(summary)])


@router.post(
    "/train/cud/pdf",
    response_model=PDFUploadInitEnvelope,
)
def train_pdf(
    file: UploadFile = File(...),
    platform: str = Form(...),
    operating_system: str = Form(...),
    version: str = Form(...),
    db: Session = Depends(get_db),
) -> PDFUploadInitEnvelope:
    platform_row, operating_system_row, version_row = _resolve_context_strict(
        db,
        platform=platform,
        operating_system=operating_system,
        version=version,
    )

    if not TECH_S3_BUCKET:
        raise HTTPException(
            status_code=500,
            detail="TECH_S3_BUCKET is not configured"
        )

    # generate unique file key
    file_key = f"uploads/{uuid4().hex}"

    try:
        s3.upload_fileobj(

            file.file,   
            TECH_S3_BUCKET,
            file_key,
            ExtraArgs={"ContentType": file.content_type},
    )
    except Exception as exc:
         raise HTTPException(500, f"S3 upload failed: {exc}")
   

    #  create File entry
    file_row = TechFile(identity=file_key)
    db.add(file_row)
    db.commit()
    db.refresh(file_row)

    # create ingestion entry
    ingestion = IngestionUsageModel(
        file_id=file_row.id,
        source_type="pdf",
        platform_id=platform_row.id,
        os_id=operating_system_row.id,
        version_id=version_row.id,
        status="pending",
    )

    db.add(ingestion)
    db.commit()
    db.refresh(ingestion)

    ingestion_result = ingest_pdf(
        s3_key=file_key,
        platform=platform_row.identity,
        operating_system=operating_system_row.identity,
        version=version_row.identity,
        ingestion_id=ingestion.id,
    )

    db.refresh(ingestion)

    # return response (upload + ingestion info)
    return PDFUploadInitEnvelope(
        status_code=200,
        data=[
            PDFUploadInitResponse(
                file_id=file_row.id,
                upload_url="",
                file_key=file_key,
                ingestion_id=ingestion.id,
                status=ingestion_result.get("status", ingestion.status),
            )
        ],
    )


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
                url=row.url,
                source_type=row.source_type,
                status=row.status,
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
    summary = ingest_scam_single_url(url=payload.url, source_type=payload.source_type or "url")
    return IngestEnvelope(status_code=200, data=[to_ingest_response(summary)])


@router.post("/scam/train/cud/urls", response_model=IngestEnvelope, status_code=200)
def train_scam_bulk_urls(
    payload: ScamTrainBulkURLsInput,
    background_tasks: BackgroundTasks,
) -> IngestEnvelope:
    background_tasks.add_task(
        ingest_scam_bulk_urls,
        urls=payload.urls,
        source_type=payload.source_type or "url",
    )
    return IngestEnvelope(
        status_code=200,
        data=[
            IngestResponse(
                status="queued",
            )
        ],
    )


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


@router.get("/ingestion-usage/list", response_model=IngestEnvelope, status_code=200)
def list_ingestion_usage(include_deleted: bool = False, db: Session = Depends(get_db)) -> IngestEnvelope:
    query = db.query(IngestionUsageModel)
    if not include_deleted:
        query = query.filter(IngestionUsageModel.is_deleted.is_(False))
    rows = query.order_by(IngestionUsageModel.created.desc()).all()
    return IngestEnvelope(status_code=200, data=[_to_usage_response(row) for row in rows])


@router.get("/ingestion-usage/list/{url:path}", response_model=IngestEnvelope, status_code=200)
def get_ingestion_usage(url: str, db: Session = Depends(get_db)) -> IngestEnvelope:
    row = _get_ingestion_usage_by_url_or_404(db, url)
    return IngestEnvelope(status_code=200, data=[_to_usage_response(row)])


@router.patch("/ingestion-usage/cud/{url:path}", response_model=IngestEnvelope, status_code=200)
def patch_ingestion_usage(
    url: str, payload: UpdateIngestionUsageRequest, db: Session = Depends(get_db)
) -> IngestEnvelope:
    row = _get_ingestion_usage_by_url_or_404(db, url)
    if payload.status is not None and row.status != payload.status.value:
        row.status = payload.status.value
        db.commit()
        db.refresh(row)
    return IngestEnvelope(status_code=200, data=[_to_usage_response(row)])


@router.delete("/ingestion-usage/cud/{url:path}", response_model=IngestEnvelope, status_code=200)
def delete_ingestion_usage(url: str, db: Session = Depends(get_db)) -> IngestEnvelope:
    row = _get_ingestion_usage_by_url_or_404(db, url)
    soft_delete_and_refresh(db, row)
    return IngestEnvelope(status_code=200, data=[_to_usage_response(row)])
