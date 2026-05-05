import logging

from fastapi import FastAPI

from app.routers.admin import router as flow_router
from app.routers.health import router as health_router
from wati.router import router as wati_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

app = FastAPI(title="Saksham Bot API", version="1.0.0")
app.include_router(health_router)
app.include_router(flow_router)
app.include_router(wati_router)



@app.get("/")
async def root() -> dict:
    return {"message": "Saksham bot running"}