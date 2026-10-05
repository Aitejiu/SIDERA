from functools import partial

import anyio
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from sidera_product.chat.contracts import error_envelope
from sidera_product.postgres.store import ProductStoreError

from .local_session import SESSION_COOKIE, LocalSessionError, LocalSessions


class LocalSessionMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, *, sessions: LocalSessions) -> None:
        super().__init__(app)
        self.sessions = sessions

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        try:
            if SESSION_COOKIE in request.cookies:
                await anyio.to_thread.run_sync(
                    partial(self.sessions.authenticate_request, request)
                )
        except LocalSessionError as error:
            return JSONResponse(
                error_envelope(error.code)
                if request.url.path.startswith("/main-agent/v1/")
                else {"detail": {"code": error.code}},
                status_code=error.status_code,
            )
        except ProductStoreError:
            return JSONResponse(
                error_envelope("product_store_unavailable", retryable=True)
                if request.url.path.startswith("/main-agent/v1/")
                else {"detail": {"code": "product_store_unavailable"}},
                status_code=503,
            )
        return await call_next(request)
