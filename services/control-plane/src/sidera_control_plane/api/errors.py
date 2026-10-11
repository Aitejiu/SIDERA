from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from sidera_control_plane.application.artifact_content import (
    ArtifactRangeNotSatisfiable,
)
from sidera_control_plane.application.errors import (
    ApplicationError,
    Conflict,
    IdempotencyConflict,
    NotFound,
    ServiceUnavailable,
)
from sidera_control_plane.domain.errors import (
    DomainError,
    ValidationError,
)


def native_http_error_response(exc: HTTPException) -> JSONResponse:
    messages = {
        "authentication_required": "authentication required",
        "permission_denied": "permission denied",
        "validation_error": "request validation failed",
        "control_plane_closed": "control plane is closed",
    }
    code = exc.detail.get("code") if isinstance(exc.detail, dict) else None
    if not isinstance(code, str) or code not in messages:
        code = {
            401: "authentication_required",
            403: "permission_denied",
            422: "validation_error",
            503: "service_unavailable",
        }.get(exc.status_code, "request_rejected")
    message = messages.get(
        code, "service unavailable" if exc.status_code == 503 else "request rejected"
    )
    return _error_response(exc.status_code, code, message, {})


def install_native_http_error_handler(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def handle_native_http_error(
        _request: Request, exc: HTTPException
    ) -> JSONResponse:
        return native_http_error_response(exc)

    @app.exception_handler(DomainError)
    async def handle_native_domain_error(
        _request: Request, exc: DomainError
    ) -> JSONResponse:
        if exc.code in {
            "assignment_forbidden",
            "permission_denied",
            "grant_revoked",
            "grant_denied",
            "grant_required",
        }:
            return _error_response(403, exc.code, "permission denied", {})
        return _domain_error_response(exc)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        issues = [
            {"location": list(error["loc"]), "type": error["type"]}
            for error in exc.errors()
        ]
        return _error_response(
            422,
            "validation_error",
            "request validation failed",
            {"issues": issues},
        )

    @app.exception_handler(ApplicationError)
    async def handle_application_error(
        _request: Request, exc: ApplicationError
    ) -> JSONResponse:
        if isinstance(exc, NotFound):
            status_code = 404
        elif isinstance(exc, ArtifactRangeNotSatisfiable):
            status_code = 416
        elif isinstance(exc, ServiceUnavailable):
            status_code = 503
        elif isinstance(exc, Conflict | IdempotencyConflict):
            status_code = 409
        else:
            status_code = 422
        return _error_response(status_code, exc.code, str(exc), exc.details)

    @app.exception_handler(DomainError)
    async def handle_domain_error(
        _request: Request, exc: DomainError
    ) -> JSONResponse:
        return _domain_error_response(exc)


def _domain_error_response(exc: DomainError) -> JSONResponse:
    status_code = (
        422
        if exc.code
        in {
            ValidationError.code,
            "adapter_workflow_requires_loopback",
            "demo_runtime_binding_forbidden",
            "result_inputs_disabled",
            "result_cursor_invalid",
        }
        else 409
    )
    return _error_response(status_code, exc.code, str(exc), exc.details)


def _error_response(
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any],
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details,
            }
        },
    )
