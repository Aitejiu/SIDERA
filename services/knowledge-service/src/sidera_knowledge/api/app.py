from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from sidera_knowledge.api.errors import safe_error_response
from sidera_knowledge.config import KnowledgeSettings
from sidera_knowledge.persistence.database import Database


def create_app(settings: KnowledgeSettings | None = None) -> FastAPI:
    resolved_settings = settings or KnowledgeSettings()  # pyright: ignore[reportCallIssue]

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database = Database(resolved_settings.database_url)
        app.state.database = database
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
