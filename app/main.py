from fastapi import FastAPI
from config import database
from models import context, ingestion_records  # noqa: F401
from routers.admin import router as flow_router
from routers.health import router as health_router

app = FastAPI(title="Ingestion API", version="1.0.0")


@app.on_event("startup")
def on_startup() -> None:
    database.Base.metadata.create_all(bind=database.engine)


app.include_router(health_router)
app.include_router(flow_router)
