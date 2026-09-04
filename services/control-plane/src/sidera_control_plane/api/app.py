from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from sidera_control_plane.api.artifact_content import (
    router as artifact_content_router,
)
from sidera_control_plane.api.collaboration_queries import (
    router as collaboration_queries_router,
)
from sidera_control_plane.api.command_queries import router as command_queries_router
from sidera_control_plane.api.context_package_queries import (
    router as context_package_queries_router,
)
from sidera_control_plane.api.errors import install_error_handlers
from sidera_control_plane.api.execution_queries import (
    router as execution_queries_router,
)
from sidera_control_plane.api.gateway_queries import router as gateway_queries_router
from sidera_control_plane.api.handoff_queries import router as handoff_queries_router
from sidera_control_plane.api.module_queries import router as module_queries_router
from sidera_control_plane.api.notification_queries import (
    router as notification_queries_router,
)
from sidera_control_plane.api.plan_queries import router as plan_queries_router
from sidera_control_plane.api.plan_transition_queries import (
    router as plan_transition_queries_router,
)
from sidera_control_plane.api.planning_queries import router as planning_queries_router
from sidera_control_plane.api.projection_stream import (
    router as projection_stream_router,
)
from sidera_control_plane.api.runtime_queries import router as runtime_queries_router
from sidera_control_plane.api.subtask_queries import (
    router as subtask_queries_router,
)
from sidera_control_plane.api.task_commands import router as task_commands_router
from sidera_control_plane.api.task_first_queries import (
    router as task_first_queries_router,
)
from sidera_control_plane.api.task_queries import router as task_queries_router
from sidera_control_plane.api.work_context_queries import (
    router as work_context_queries_router,
)
from sidera_control_plane.api.worker_item_queries import (
    router as worker_item_queries_router,
)
from sidera_control_plane.api.worker_queries import router as worker_queries_router
from sidera_control_plane.api.worker_trace_queries import (
    router as worker_trace_queries_router,
)
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
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[resolved_settings.web_origin],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Last-Event-ID", "Range"],
        expose_headers=["Content-Range", "Accept-Ranges", "Content-Disposition"],
        allow_credentials=False,
    )
    app.state.settings = resolved_settings
    install_error_handlers(app)
    app.include_router(task_commands_router)
    app.include_router(command_queries_router)
    app.include_router(collaboration_queries_router)
    app.include_router(context_package_queries_router)
    app.include_router(task_first_queries_router)
    app.include_router(subtask_queries_router)
    app.include_router(task_queries_router)
    app.include_router(plan_queries_router)
    app.include_router(plan_transition_queries_router)
    app.include_router(planning_queries_router)
    app.include_router(runtime_queries_router)
    app.include_router(projection_stream_router)
    app.include_router(worker_queries_router)
    app.include_router(module_queries_router)
    app.include_router(notification_queries_router)
    app.include_router(worker_item_queries_router)
    app.include_router(worker_trace_queries_router)
    app.include_router(work_context_queries_router)
    app.include_router(execution_queries_router)
    app.include_router(gateway_queries_router)
    app.include_router(handoff_queries_router)
    app.include_router(artifact_content_router)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "sidera-control-plane"}

    return app
