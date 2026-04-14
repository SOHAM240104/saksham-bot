from fastapi import FastAPI

from app.routers.admin import router as flow_router
from app.routers.health import router as health_router


app = FastAPI(title="Ingestion API", version="1.0.0")

app.include_router(health_router)
app.include_router(flow_router)
