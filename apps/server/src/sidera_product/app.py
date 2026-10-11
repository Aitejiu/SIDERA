from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

import anyio
from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SecretStr,
    StrictInt,
    ValidationError,
)
from sidera_agents.contracts import ProfileRef
from sidera_agents.errors import AgentError
from sidera_control_plane.composition.native_runtime import (
    ControlPlaneIdentity,
    WorkspaceSetupRequest,
)
from sidera_control_plane.domain.errors import DomainError
from sidera_control_plane.domain.planning_profiles import PlanningBudgetPolicy
from sidera_control_plane.domain.result_integration import CheckSpecV1
from sidera_model_connections.connections import ConnectionService
from sidera_model_connections.credentials import CredentialService
from sidera_model_connections.errors import (
    ConfigurationError as CredentialConfigurationError,
)
from sidera_model_connections.http import ManagementPrincipal, create_management_router

from .agents.routes import agents_router, product_management_router
from .agents.schemas import BootstrapSessionView, LocalSessionView, LogoutView
from .auth.local_session import (
    CSRF_COOKIE,
    CSRF_HEADER,
    SESSION_COOKIE,
    BootstrapTokenBody,
    LocalSessionError,
)
from .auth.middleware import LocalSessionMiddleware
from .chat.contracts import error_envelope
from .chat.routes import chat_router
from .chat.service import ChatError
from .composition import build_product_runtime
from .config import ProductSettings
from .lifecycle import ProductRuntime
from .postgres.store import ProductStoreError
from .task_tools.receipts import ToolReceipts
from .task_tools.server import TaskToolServer
from .task_tools.tools import ProductTaskTools


class WorkspaceSetupBody(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    repository_directory: Path
    expected_version: StrictInt = Field(ge=0)
    planning_budget: dict[str, StrictInt]
    planner_agent_ref: ProfileRef | None = None
    verification_checks: list[dict[str, JsonValue]] | None = None

    def to_request(self, workspace_id: UUID) -> WorkspaceSetupRequest:
        return WorkspaceSetupRequest(
            workspace_id=workspace_id,
            repository_directory=self.repository_directory,
            expected_version=self.expected_version,
            planning_budget=PlanningBudgetPolicy(**self.planning_budget),
            planner_agent_ref=self.planner_agent_ref,
            verification_checks=None
            if self.verification_checks is None
            else tuple(
                CheckSpecV1.from_mapping(check) for check in self.verification_checks
            ),
        )


def create_app(
    settings: ProductSettings | None = None,
    *,
    runtime: ProductRuntime | None = None,
) -> FastAPI:
    if runtime is None:
        runtime = build_product_runtime(settings or ProductSettings.model_validate({}))
    elif settings is not None and settings != runtime.settings:
        raise ValueError("Product settings must match the supplied runtime")
    owner = runtime
    task_tools = ProductTaskTools(
        owner.grants,
        ToolReceipts(owner.grants, owner.authority_store),
        owner.product_store,
        owner.control_plane,
    )
    task_tool_server = TaskToolServer(owner.grants)
    task_tools.register(task_tool_server)
    task_tool_app = task_tool_server.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        async with task_tool_server.session_manager.run():
            try:
                await anyio.to_thread.run_sync(owner.start)
                yield
            finally:
                complete = await anyio.to_thread.run_sync(owner.close)
                if not complete:
                    raise RuntimeError("product_cleanup_pending")

    app = FastAPI(title="SIDERA", lifespan=lifespan)
    app.state.product_runtime = owner
    app.state.task_tool_server = task_tool_server
    app.state.task_tools = task_tools
    app.add_middleware(LocalSessionMiddleware, sessions=owner.local_sessions)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[owner.settings.web_origin],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Content-Type", "Authorization", CSRF_HEADER],
    )

    @app.exception_handler(LocalSessionError)
    async def local_session_error(
        request: Request, error: LocalSessionError
    ) -> JSONResponse:
        return JSONResponse(
            error_envelope(error.code)
            if request.url.path.startswith("/main-agent/v1/")
            else {"detail": {"code": error.code}},
            status_code=error.status_code,
        )

    @app.exception_handler(ProductStoreError)
    async def product_store_error(
        request: Request, error: ProductStoreError
    ) -> JSONResponse:
        return JSONResponse(
            error_envelope("product_store_unavailable", retryable=True)
            if request.url.path.startswith("/main-agent/v1/")
            else {"detail": {"code": "product_store_unavailable"}},
            status_code=503,
        )

    @app.exception_handler(ChatError)
    async def chat_error(request: Request, error: ChatError) -> JSONResponse:
        return JSONResponse(
            error_envelope(error.code, retryable=error.retryable),
            status_code=error.status_code,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        if request.url.path.startswith("/main-agent/v1/"):
            return JSONResponse(error_envelope("validation_error"), status_code=422)
        if request.url.path.startswith("/agents/v1/") or (
            request.url.path.startswith("/product/v1/workspaces/")
            and request.url.path.endswith(("/defaults", "/configuration-requests"))
        ):
            return JSONResponse(
                {"detail": {"code": "validation_error"}},
                status_code=422,
                headers={"Cache-Control": "no-store"},
            )
        return await request_validation_exception_handler(request, error)

    @app.post(
        "/product/v1/session/bootstrap",
        response_model=BootstrapSessionView,
        operation_id="exchangeLocalBootstrap",
    )
    async def exchange_bootstrap(request: Request) -> JSONResponse:
        owner.local_sessions.require_browser_boundary(request)
        if SESSION_COOKIE in request.cookies or "authorization" in request.headers:
            raise LocalSessionError("ambiguous_authentication", status_code=403)
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 4096:
                raise LocalSessionError("bootstrap_invalid", status_code=422)
            body.extend(chunk)
        try:
            payload = BootstrapTokenBody.model_validate_json(bytes(body))
        except ValidationError:
            raise LocalSessionError("bootstrap_invalid", status_code=422) from None
        issue = await anyio.to_thread.run_sync(
            owner.local_sessions.exchange, SecretStr(payload.token)
        )
        reply = JSONResponse(
            {
                "session_id": str(issue.session_id),
                "expires_at": issue.expires_at.isoformat(),
            }
        )
        for name, token, http_only in (
            (SESSION_COOKIE, issue.token, True),
            (CSRF_COOKIE, issue.csrf, False),
        ):
            reply.set_cookie(
                name,
                token.get_secret_value(),
                path="/",
                secure=False,
                httponly=http_only,
                samesite="strict",
                max_age=owner.settings.local_session_ttl_seconds,
            )
        return reply

    def current_session(request: Request) -> ControlPlaneIdentity:
        identity = owner.local_sessions.authenticate_request(request)
        if identity is None:
            raise HTTPException(401, detail={"code": "local_session_required"})
        return identity

    @app.get(
        "/product/v1/session",
        response_model=LocalSessionView,
        operation_id="readLocalSession",
    )
    async def session_status(request: Request) -> dict[str, str]:
        identity = await anyio.to_thread.run_sync(current_session, request)
        row = await anyio.to_thread.run_sync(
            lambda: owner.authority_store.find_session(session_id=identity.grant_id)
        )
        if row is None:
            raise HTTPException(401, detail={"code": "local_session_required"})
        return {
            "session_id": str(row.session_id),
            "workspace_id": str(row.workspace_id),
            "principal_id": row.principal_id,
            "expires_at": row.expires_at.isoformat(),
        }

    @app.post(
        "/product/v1/session/logout",
        response_model=LogoutView,
        operation_id="logoutLocalSession",
    )
    async def logout(request: Request) -> JSONResponse:
        identity = await anyio.to_thread.run_sync(current_session, request)
        await anyio.to_thread.run_sync(owner.local_sessions.revoke, identity.grant_id)
        reply = JSONResponse({"status": "revoked"})
        for name in (SESSION_COOKIE, CSRF_COOKIE):
            reply.delete_cookie(
                name,
                path="/",
                secure=False,
                httponly=name == SESSION_COOKIE,
                samesite="strict",
            )
        return reply

    @app.post("/product/v1/workspaces/{workspace_id}/setup")
    async def setup_workspace(
        workspace_id: UUID, body: WorkspaceSetupBody, request: Request
    ) -> dict[str, JsonValue]:
        identity = await anyio.to_thread.run_sync(current_session, request)
        if workspace_id != owner.settings.workspace_id:
            raise HTTPException(403, detail={"code": "permission_denied"})
        try:
            selected = body.to_request(workspace_id)
        except (DomainError, TypeError, ValueError):
            raise HTTPException(422, detail={"code": "validation_error"}) from None
        try:
            receipt = await anyio.to_thread.run_sync(
                lambda: owner.control_plane.configure_workspace(
                    selected, grant_id=identity.grant_id
                )
            )
        except AgentError as error:
            status = (
                403
                if error.code == "permission_denied"
                else 409
                if error.code in {"revision_conflict", "configuration_mismatch"}
                else 422
            )
            raise HTTPException(status, detail={"code": error.code}) from None
        return {
            "workspace_id": str(receipt.workspace_id),
            "version": receipt.version,
            "status": receipt.status,
            "repository_ref": receipt.repository_ref,
            "planning_policy_ref": {
                "policy_id": str(receipt.planning_policy_ref.policy_id),
                "version": receipt.planning_policy_ref.version,
            },
        }

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/product/v1/workspaces/{workspace_id}/setup")
    async def read_workspace_setup(
        workspace_id: UUID, request: Request
    ) -> dict[str, JsonValue] | None:
        identity = await anyio.to_thread.run_sync(authenticated, request)
        if workspace_id != owner.settings.workspace_id:
            raise HTTPException(403, detail={"code": "permission_denied"})
        try:
            setup = await anyio.to_thread.run_sync(
                lambda: owner.control_plane.get_workspace_setup(
                    workspace_id, grant_id=identity.grant_id
                )
            )
        except AgentError as error:
            raise HTTPException(
                403 if error.code == "permission_denied" else 409,
                detail={"code": error.code},
            ) from None
        if setup is None:
            return None
        return {
            "workspace_id": str(setup.workspace_id),
            "version": setup.version,
            "repository_ref": setup.repository_ref,
            "planning_policy_ref": {
                "policy_id": str(setup.planning_policy_ref.policy_id),
                "version": setup.planning_policy_ref.version,
            },
            "planner_agent_ref": setup.planner_agent_ref.model_dump(mode="json"),
        }

    @app.get("/readyz")
    async def readiness() -> dict[str, object]:
        components = await anyio.to_thread.run_sync(owner.inspect_readiness)
        return {
            "ready": owner.status()["lifecycle"] == "ready"
            and all(components.values()),
            "components": components,
        }

    @app.get("/product/v1/status")
    async def status() -> dict[str, object]:
        return dict(owner.status())

    def authenticated(request: Request) -> ControlPlaneIdentity:
        try:
            identity = owner.request_authorizer.authenticate_request(request)
            if identity is not None:
                current = owner.request_authorizer.identity_for_grant(identity.grant_id)
                if current == identity and identity.actor_type in {"user", "agent"}:
                    return identity
        except Exception:  # noqa: BLE001 - no host/credential details cross the HTTP boundary
            pass
        raise HTTPException(401, detail={"code": "local_session_required"})

    @app.get("/product/v1/config")
    async def configuration(request: Request) -> dict[str, object]:
        await anyio.to_thread.run_sync(authenticated, request)
        return {
            "workspace_id": str(owner.settings.workspace_id),
            "bind_host": owner.settings.bind_host,
            "port": owner.settings.port,
            "result_inputs_enabled": owner.settings.result_inputs_enabled,
            "result_integration_enabled": owner.settings.result_integration_enabled,
            "managed_credentials_enabled": owner.settings.managed_credentials_enabled,
        }

    app.include_router(agents_router(owner))
    app.include_router(product_management_router(owner))

    def authorize_management(request: Request) -> ManagementPrincipal:
        identity = owner.local_sessions.authenticate_request(request)
        if (
            identity is None
            or identity.actor_type != "user"
            or identity.principal_id != owner.settings.local_principal_id
            or owner.local_sessions.identity_for_grant(identity.grant_id) != identity
        ):
            raise CredentialConfigurationError("management_unauthorized")
        return ManagementPrincipal(
            identity.principal_id, frozenset({owner.settings.workspace_id})
        )

    app.include_router(
        create_management_router(
            ConnectionService(owner.credential_catalog),
            CredentialService(owner.credential_catalog, owner.secret_store),
            owner.credential_catalog,
            authorize_management=authorize_management,
        )
    )

    @app.api_route(
        "/product/v1/workspaces/{path:path}",
        methods=["GET", "POST", "PUT", "DELETE"],
        include_in_schema=False,
    )
    async def protected_unavailable(request: Request) -> None:
        await anyio.to_thread.run_sync(authenticated, request)
        raise HTTPException(503, detail={"code": "not_implemented"})

    app.mount("/product/v1/task-tools", task_tool_app)
    app.include_router(chat_router(owner))
    app.mount("/", owner.control_plane.app)
    return app
