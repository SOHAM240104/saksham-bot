import logging

from fastapi import FastAPI

from wati.router import router as wati_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

app = FastAPI(title="WATI Webhook Backend")
app.include_router(wati_router)


@app.get("/")
async def root() -> dict:
    return {"message": "WATI bot running"}
