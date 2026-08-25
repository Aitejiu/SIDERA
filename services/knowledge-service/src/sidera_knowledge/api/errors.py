from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from sidera_knowledge.application.knowledge_queries import KnowledgeMaterializationError
from sidera_knowledge.domain.common import DomainValidationError
from sidera_knowledge.domain.knowledge_errors import CommandFailure


def safe_error_response(error_code: str) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"status": "error", "error_code": error_code},
    )


_CONFLICT_CODES = {
    "idempotency_conflict",
    "expected_version_conflict",
    "candidate_stale",
    "hash_mismatch",
}
_NOT_FOUND_CODES = {"not_found"}
_PERMISSION_CODES = {"permission_denied"}
_TEMPORARY_CODES = {"temporarily_unavailable", "database_unavailable"}


def status_for_error(code: str) -> int:
    if code in _CONFLICT_CODES:
        return 409
    if code in _NOT_FOUND_CODES:
        return 404
    if code in _PERMISSION_CODES:
        return 403
    if code in _TEMPORARY_CODES:
        return 503
    return 422


def api_error_response(
    code: str,
    *,
    status_code: int | None = None,
    details: Mapping[str, Any] | None = None,
    message: str | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code if status_code is not None else status_for_error(code),
        content={
            "error": {
                "code": code,
                "message": message or code.replace("_", " "),
                "details": dict(details or {}),
            }
        },
    )


def validation_error_response(exc: RequestValidationError) -> JSONResponse:
    fields = [
        ".".join(str(part) for part in error.get("loc", ()) if part != "body")
        for error in exc.errors()
    ]
    return api_error_response(
        "validation_error",
        status_code=422,
        details={"fields": sorted(set(fields))},
        message="request validation failed",
    )


def domain_error_response(exc: DomainValidationError) -> JSONResponse:
    if isinstance(exc, (CommandFailure, KnowledgeMaterializationError)):
        code = exc.code
        details = getattr(exc, "details", {})
    else:
        code = "validation_error"
        details = {}
    return api_error_response(code, details=details)
