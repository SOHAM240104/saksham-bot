"""Router exports."""

from .admin import router as admin_router
from .health import router as health_router

__all__ = ["admin_router", "health_router"]

