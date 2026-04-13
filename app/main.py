from fastapi import FastAPI

from app.vectorstore import ensure_pgvector_tables_and_collections
from config import database
from models import context, ingestion_records  # noqa: F401
from routers.admin import router as flow_router
from routers.health import router as health_router
from settings import validate_embedding_configuration

app = FastAPI(title="Ingestion API", version="1.0.0")


@app.on_event("startup")
def on_startup() -> None:
    validate_embedding_configuration()
    database.Base.metadata.create_all(bind=database.engine)
    ensure_pgvector_tables_and_collections()


app.include_router(health_router)
app.include_router(flow_router)
