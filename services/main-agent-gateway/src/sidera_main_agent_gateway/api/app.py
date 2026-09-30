from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sidera_runtime_guard import RuntimeBudgetGuard

from sidera_main_agent_gateway.acceptance_guard import load_acceptance_guard
from sidera_main_agent_gateway.adapters.akashic import AkashicAdapter
from sidera_main_agent_gateway.adapters.demo import DemoMainAgentAdapter
from sidera_main_agent_gateway.api.routes import router
from sidera_main_agent_gateway.config import Settings
from sidera_main_agent_gateway.contracts import MainAgentAdapter
from sidera_main_agent_gateway.errors import (
    AcceptanceGuardRejected,
    GatewayError,
    ProviderProtocolError,
    ProviderUnavailable,
    ThreadAccessDenied,
    ThreadBusy,
    ThreadNotFound,
    TurnNotFound,
    TurnStreamUnavailable,
)
from sidera_main_agent_gateway.relay import (
    RelayAlreadyActive,
    TurnRelayRegistry,
)


def _error_response(
    status_code: int,
    code: str,
    message: str,
    retryable: bool,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "retryable": retryable,
            }
        },
    )


def _gateway_error(exc: GatewayError) -> tuple[int, str, str, bool]:
    if isinstance(exc, AcceptanceGuardRejected):
        if exc.code in {"budget_exhausted", "deadline_exceeded"}:
            return 429, exc.code, "acceptance budget is unavailable", False
        return 409, exc.code, "acceptance operation was not sent", False
    if isinstance(exc, (ThreadNotFound, ThreadAccessDenied)):
        return 404, "thread_not_found", "thread was not found", False
    if isinstance(exc, TurnNotFound):
        return 404, "turn_not_found", "turn was not found", False
    if isinstance(exc, ThreadBusy):
        return 409, "thread_busy", "thread already has an active turn", True
    if isinstance(exc, (TurnStreamUnavailable, RelayAlreadyActive)):
        return 409, "turn_stream_unavailable", "turn stream is unavailable", True
    if isinstance(exc, ProviderUnavailable):
        return 503, "provider_unavailable", "main agent provider is unavailable", True
    if isinstance(exc, ProviderProtocolError):
        return 502, "provider_protocol_error", "main agent provider returned an invalid response", False
    return 500, "gateway_error", "main agent gateway error", False


def create_app(
    settings: Settings | None = None,
    *,
    adapter: MainAgentAdapter | None = None,
    relay_registry: TurnRelayRegistry | None = None,
    acceptance_guard: RuntimeBudgetGuard | None = None,
) -> FastAPI:
    resolved_settings = settings or Settings()  # pyright: ignore[reportCallIssue]
    resolved_guard = (
        load_acceptance_guard()
        if acceptance_guard is None
        else acceptance_guard
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if adapter is not None:
            resolved_adapter = adapter
        elif resolved_settings.adapter_kind == "interview-demo":
            if resolved_guard is None:
                resolved_adapter = DemoMainAgentAdapter(resolved_settings)
            else:
                resolved_adapter = DemoMainAgentAdapter(
                    resolved_settings,
                    acceptance_guard=resolved_guard,
                )
        else:
            if resolved_guard is None:
                resolved_adapter = AkashicAdapter(resolved_settings)
            else:
                resolved_adapter = AkashicAdapter(
                    resolved_settings,
                    acceptance_guard=resolved_guard,
                )
        resolved_relays = relay_registry or TurnRelayRegistry(
            event_limit=resolved_settings.relay_event_limit
        )
        app.state.main_agent_adapter = resolved_adapter
        app.state.turn_relays = resolved_relays
        try:
            yield
        finally:
            await resolved_relays.shutdown()
            await resolved_adapter.close()

    app = FastAPI(
        title="SIDERA Main Agent Gateway",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings

    @app.middleware("http")
    async def reject_restore_writes(request: Request, call_next):
        if resolved_settings.restore_read_only and request.method not in {
            "GET",
            "HEAD",
            "OPTIONS",
        }:
            return _error_response(
                409,
                "restore_read_only",
                "restored runtime is read-only",
                False,
            )
        return await call_next(request)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[str(resolved_settings.web_origin).rstrip("/")],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Last-Event-ID"],
        allow_credentials=False,
    )

    @app.exception_handler(GatewayError)
    async def gateway_error_handler(
        _request: Request,
        exc: GatewayError,
    ) -> JSONResponse:
        status, code, message, retryable = _gateway_error(exc)
        return _error_response(status, code, message, retryable)

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(
        _request: Request,
        _exc: RequestValidationError,
    ) -> JSONResponse:
        return _error_response(422, "invalid_request", "invalid request", False)

    app.include_router(router)
    return app
