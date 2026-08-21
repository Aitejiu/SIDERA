from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from sidera_control_plane.api.errors import install_error_handlers
from sidera_control_plane.api.execution_queries import (
    router as execution_queries_router,
)
from sidera_control_plane.api.gateway_queries import router as gateway_queries_router
from sidera_control_plane.api.plan_queries import router as plan_queries_router
from sidera_control_plane.api.projection_stream import (
    router as projection_stream_router,
)
from sidera_control_plane.api.task_commands import router as task_commands_router
from sidera_control_plane.api.task_first_queries import (
    router as task_first_queries_router,
)
from sidera_control_plane.api.task_queries import router as task_queries_router
from sidera_control_plane.api.worker_queries import router as worker_queries_router
from sidera_control_plane.config import Settings
from sidera_control_plane.persistence.database import Database


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the control-plane HTTP application."""
    resolved_settings = settings or Settings()  # pyright: ignore[reportCallIssue]

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database = Database(resolved_settings.database_url)
        app.state.database = database
        try:
            yield
        finally:
            database.dispose()

    app = FastAPI(
        title="SIDERA Control Plane",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    install_error_handlers(app)
    app.include_router(task_commands_router)
    app.include_router(task_first_queries_router)
    app.include_router(task_queries_router)
    app.include_router(plan_queries_router)
    app.include_router(projection_stream_router)
    app.include_router(worker_queries_router)
    app.include_router(execution_queries_router)
    app.include_router(gateway_queries_router)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "sidera-control-plane"}

    return app
