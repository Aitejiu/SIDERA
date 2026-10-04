from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import anyio
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from sidera_control_plane.composition.native_runtime import ControlPlaneIdentity

from .composition import build_product_runtime
from .config import ProductSettings
from .lifecycle import ProductRuntime


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

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        await anyio.to_thread.run_sync(owner.start)
        try:
            yield
        finally:
            complete = await anyio.to_thread.run_sync(owner.close)
            if not complete:
                raise RuntimeError("product_cleanup_pending")

    app = FastAPI(title="SIDERA", lifespan=lifespan)
    app.state.product_runtime = owner
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[owner.settings.web_origin],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Content-Type", "Authorization"],
    )

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

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

    @app.api_route("/agents/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    @app.api_route(
        "/main-agent/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE"]
    )
    @app.api_route("/product/v1/task-tools/mcp", methods=["GET", "POST", "DELETE"])
    @app.api_route(
        "/product/v1/workspaces/{path:path}", methods=["GET", "POST", "PUT", "DELETE"]
    )
    async def protected_unavailable(request: Request) -> None:
        await anyio.to_thread.run_sync(authenticated, request)
        raise HTTPException(503, detail={"code": "not_implemented"})

    app.mount("/", owner.control_plane.app)
    return app
