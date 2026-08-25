from __future__ import annotations

from fastapi.responses import JSONResponse


def safe_error_response(error_code: str) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"status": "error", "error_code": error_code},
    )
