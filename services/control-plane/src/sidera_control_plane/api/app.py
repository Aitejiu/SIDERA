from collections.abc import AsyncIterator, Callable
from contextlib import AbstractContextManager, asynccontextmanager, nullcontext
from pathlib import Path
from uuid import UUID

from alembic.script import ScriptDirectory
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from sidera_control_plane.api.agent_context_queries import (
    router as agent_context_queries_router,
)
from sidera_control_plane.api.artifact_content import (
    router as artifact_content_router,
)
from sidera_control_plane.api.assignment_queries import (
    router as assignment_queries_router,
)
from sidera_control_plane.api.collaboration_queries import (
    router as collaboration_queries_router,
)
from sidera_control_plane.api.command_queries import router as command_queries_router
from sidera_control_plane.api.context_package_queries import (
    router as context_package_queries_router,
)
from sidera_control_plane.api.errors import (
    install_error_handlers,
    install_native_http_error_handler,
    native_http_error_response,
)
from sidera_control_plane.api.execution_queries import (
    router as execution_queries_router,
)
from sidera_control_plane.api.gateway_queries import router as gateway_queries_router
from sidera_control_plane.api.handoff_queries import router as handoff_queries_router
from sidera_control_plane.api.module_queries import router as module_queries_router
from sidera_control_plane.api.native_worker_queries import (
    router as native_worker_queries_router,
)
from sidera_control_plane.api.notification_delivery import (
    router as notification_delivery_router,
)
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
from sidera_control_plane.api.result_input_queries import (
    router as result_input_queries_router,
)
from sidera_control_plane.api.result_queries import router as result_queries_router
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
from sidera_control_plane.application.request_authority import (
    ControlPlaneRequestAuthorizer,
    ControlPlaneRequestTarget,
    require_command_authority,
    require_request_identity,
    require_request_scope,
)
from sidera_control_plane.config import Settings
from sidera_control_plane.demo.api import create_demo_status_router
from sidera_control_plane.demo.manifest import load_manifest, validate_manifest
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.migration_resources import migration_config

_PROJECT_ROOT = Path(__file__).resolve().parents[5]


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the control-plane HTTP application."""
    resolved_settings = settings or Settings()  # pyright: ignore[reportCallIssue]
    return _build_app(resolved_settings)


def _build_app(
    resolved_settings: Settings,
    *,
    database: Database | None = None,
    native: bool = False,
    request_authorizer: ControlPlaneRequestAuthorizer | None = None,
    admission_check: Callable[[Request], None] | None = None,
    request_admission: Callable[[Request], AbstractContextManager[None]] | None = None,
) -> FastAPI:

    async def authorize_native_target(request: Request, route_path: str) -> None:
        authorizer = request_authorizer
        if authorizer is None:
            raise HTTPException(401, detail={"code": "authentication_required"})
        identity = require_request_identity(request.state.control_plane_identity)
        path_parameters = request.path_params
        command = None
        if request.method == "POST" and (
            route_path == "/api/v1/commands"
            or "worker_transition_id" in path_parameters
        ):
            from sidera_control_plane.api.schemas import CommandRequest

            try:
                command = CommandRequest.model_validate(
                    await request.json()
                ).to_envelope()
            except (ValueError, PydanticValidationError):
                raise HTTPException(422, detail={"code": "validation_error"}) from None
        workspace_values = [
            value
            for key, value in request.query_params.multi_items()
            if key == "workspace_id"
        ]
        if "workspace_id" in path_parameters:
            workspace_values.append(path_parameters["workspace_id"])
        if command is not None:
            workspace_values.append(str(command.workspace_id))
        try:
            workspaces = {UUID(str(value)) for value in workspace_values}
        except (TypeError, ValueError):
            raise HTTPException(403, detail={"code": "permission_denied"}) from None
        if len(workspaces) != 1:
            raise HTTPException(403, detail={"code": "permission_denied"})
        target = ControlPlaneRequestTarget(
            workspaces.pop(),
            identity.principal_id,
            request.method,
            route_path,
            tuple((key, str(value)) for key, value in path_parameters.items()),
            tuple(request.query_params.multi_items()),
            command,
        )
        if command is not None:
            transition = path_parameters.get("worker_transition_id")
            if transition is not None and (
                str(command.target_id) != str(transition)
                or command.target_type != "worker_transition"
                or command.command_type
                != {
                    "reconcile": "ReconcileWorkerTransition",
                    "confirm-stopped": "ConfirmWorkerTransitionStopped",
                    "recover": "RecoverWorkerTransition",
                }.get(route_path.rsplit("/", 1)[-1])
            ):
                raise HTTPException(403, detail={"code": "permission_denied"})
            await run_in_threadpool(
                require_command_authority,
                authorizer,
                identity,
                target,
                resolved_settings,
                command,
            )
            request.state.control_plane_command_preflight = lambda envelope: (
                require_command_authority(
                    authorizer, identity, target, resolved_settings, envelope
                )
            )
        else:
            action = (
                "control_plane.query"
                if request.method in {"GET", "HEAD"}
                else "control_plane.mutate"
            )
            await run_in_threadpool(
                require_request_scope, authorizer, identity, action, target
            )

    async def protect_native_target(request: Request) -> AsyncIterator[None]:
        if request.url.path == "/healthz" and request.method in {"GET", "HEAD"}:
            yield
            return
        route = request.scope.get("route")
        if route is None:
            raise HTTPException(403, detail={"code": "permission_denied"})
        await authorize_native_target(request, route.path)
        with nullcontext() if request_admission is None else request_admission(request):
            yield

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned_database = database or Database(resolved_settings.database_url)
        app.state.database = owned_database
        try:
            if (
                resolved_settings.restore_read_only
                and resolved_settings.restore_schema_validation
            ):
                _validate_restore_schema(owned_database)
            yield
        finally:
            if database is None:
                owned_database.dispose()

    app = FastAPI(
        title="SIDERA Control Plane",
        version="0.1.0",
        lifespan=lifespan,
        dependencies=[Depends(protect_native_target, scope="request")]
        if native
        else None,
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
    if database is not None:
        app.state.database = database

    async def protect_native_request(request: Request, call_next):
        if request.url.path == "/healthz" and request.method in {"GET", "HEAD"}:
            return await call_next(request)
        if request.method == "OPTIONS":
            return await call_next(request)
        try:
            if request_authorizer is None:
                raise HTTPException(401, detail={"code": "authentication_required"})
            try:
                authenticated = await run_in_threadpool(
                    request_authorizer.authenticate_request, request
                )
            except Exception:  # noqa: BLE001 - Authentication failures are redacted.
                authenticated = None
            request.state.control_plane_identity = require_request_identity(
                authenticated
            )
            if admission_check is not None:
                admission_check(request)
            if request.url.path in {
                "/docs",
                "/docs/oauth2-redirect",
                "/redoc",
                "/openapi.json",
            }:
                await authorize_native_target(request, request.url.path)
        except HTTPException as exc:
            return native_http_error_response(exc)
        return await call_next(request)

    @app.middleware("http")
    async def reject_restore_writes(request: Request, call_next):
        if resolved_settings.restore_read_only and request.method not in {
            "GET",
            "HEAD",
            "OPTIONS",
        }:
            return JSONResponse(
                status_code=409,
                content={
                    "error": {
                        "code": "restore_read_only",
                        "message": "restored runtime is read-only",
                        "details": {"restore_mode": "read_only"},
                    }
                },
            )
        return await call_next(request)

    if native:
        app.middleware("http")(protect_native_request)

    install_error_handlers(app)
    if native:
        install_native_http_error_handler(app)
    app.include_router(agent_context_queries_router)
    app.include_router(assignment_queries_router)
    app.include_router(native_worker_queries_router)
    app.include_router(task_commands_router)
    app.include_router(command_queries_router)
    app.include_router(collaboration_queries_router)
    app.include_router(result_input_queries_router)
    app.include_router(result_queries_router)
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
    app.include_router(notification_delivery_router)
    app.include_router(worker_item_queries_router)
    app.include_router(worker_trace_queries_router)
    app.include_router(work_context_queries_router)
    app.include_router(execution_queries_router)
    app.include_router(gateway_queries_router)
    app.include_router(handoff_queries_router)
    app.include_router(artifact_content_router)

    if resolved_settings.interview_demo_manifest is not None:
        manifest_path = resolved_settings.interview_demo_manifest.expanduser()
        manifest = load_manifest(manifest_path)
        validate_manifest(manifest, _PROJECT_ROOT)
        app.include_router(create_demo_status_router(manifest))

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "sidera-control-plane"}

    return app


def _validate_restore_schema(database: Database) -> None:
    config = migration_config()
    expected_heads = tuple(sorted(ScriptDirectory.from_config(config).get_heads()))
    try:
        with database.engine.connect() as connection:
            actual_heads = tuple(
                connection.scalars(
                    text("SELECT version_num FROM alembic_version ORDER BY version_num")
                ).all()
            )
    except SQLAlchemyError as exc:
        raise RuntimeError("restore_schema_not_current") from exc
    if not expected_heads or actual_heads != expected_heads:
        raise RuntimeError("restore_schema_not_current")
