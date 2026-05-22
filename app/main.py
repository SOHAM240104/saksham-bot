import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.routers.admin import router as flow_router
from app.routers.health import router as health_router
from wati.router import router as wati_router
from wati.settings import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start/stop the TechSaathi background poll with the FastAPI app."""
    poll_task = None
    if getattr(settings, "WATI_TECHSAATHI_POLL_ENABLED", True):
        from wati.services.webhook.techsaathi_poll import run_techsaathi_poll_loop

        # Runs for the lifetime of the server — checks WATI for agent-closed chats.
        poll_task = asyncio.create_task(run_techsaathi_poll_loop())
    yield
    if poll_task:
        poll_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poll_task


app = FastAPI(title="Saksham Bot API", version="1.0.0", lifespan=lifespan)
app.include_router(health_router)
app.include_router(flow_router)
app.include_router(wati_router)



@app.get("/")
async def root() -> dict:
    return {"message": "Saksham bot running"}