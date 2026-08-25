from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from sidera_knowledge.api.commands import router as command_router
from sidera_knowledge.api.errors import (
    api_error_response,
    domain_error_response,
    safe_error_response,
    validation_error_response,
)
from sidera_knowledge.api.queries import router as query_router
from sidera_knowledge.application.command_service import KnowledgeCommandService
from sidera_knowledge.application.knowledge_commands import knowledge_command_handlers
from sidera_knowledge.application.knowledge_queries import KnowledgeQueryService
from sidera_knowledge.config import KnowledgeSettings
from sidera_knowledge.persistence.database import Database
from sidera_knowledge.persistence.query_store import (
    PostgresRetrievalAdapter,
    SqlAlchemyKnowledgeQueryStore,
)
from sidera_knowledge.persistence.unit_of_work import SqlAlchemyUnitOfWork


def create_app(settings: KnowledgeSettings | None = None) -> FastAPI:
    resolved_settings = settings or KnowledgeSettings()  # pyright: ignore[reportCallIssue]

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database = Database(resolved_settings.database_url)
        app.state.database = database
        app.state.command_service = KnowledgeCommandService(
            lambda: SqlAlchemyUnitOfWork(database), knowledge_command_handlers()
        )
        query_store = SqlAlchemyKnowledgeQueryStore(database)
        app.state.query_service = KnowledgeQueryService(query_store)
        app.state.retrieval_adapter = PostgresRetrievalAdapter(database)
        try:
            yield
        finally:
            database.dispose()

    app = FastAPI(
        title="SIDERA Knowledge Service",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.include_router(command_router)
    app.include_router(query_router)

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(
        request: Request, exc: RequestValidationError
    ):
        return validation_error_response(exc)

    @app.exception_handler(HTTPException)
    async def handle_http_exception(request: Request, exc: HTTPException):
        code = exc.detail if isinstance(exc.detail, str) else "request_failed"
        return api_error_response(code, status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def handle_known_exception(request: Request, exc: Exception):
        if isinstance(exc, SQLAlchemyError):
            return api_error_response("temporarily_unavailable", status_code=503)
        from sidera_knowledge.domain.common import DomainValidationError

        if isinstance(exc, DomainValidationError):
            return domain_error_response(exc)
        raise exc

    @app.get("/health/live")
    def health_live() -> dict[str, str]:
        return {"status": "ok", "service": "sidera-knowledge"}

    @app.get("/health/ready")
    def health_ready(request: Request):
        database: Database = request.app.state.database
        try:
            with database.engine.connect() as connection:
                connection.execute(text("SELECT 1")).scalar_one()
        except SQLAlchemyError:
            return safe_error_response("database_unavailable")

        try:
            with database.engine.connect() as connection:
                versions = tuple(
                    connection.scalars(
                        text("SELECT version_num FROM alembic_version")
                    ).all()
                )
        except SQLAlchemyError:
            return safe_error_response("schema_not_current")

        if versions != (resolved_settings.alembic_head,):
            return safe_error_response("schema_not_current")
        return {
            "status": "ok",
            "service": "sidera-knowledge",
            "schema_head": resolved_settings.alembic_head,
        }

    return app


app = create_app()
