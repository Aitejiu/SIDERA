from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

from sqlalchemy import text
from sqlalchemy.engine import Connection, create_engine

from .contracts import DemoManifest


class DemoDatabaseError(ValueError):
    """Raised when a demo database target is not local and owned."""


_ADMIN_CONNECT_ARGS = {
    "connect_timeout": 3,
    "options": "-c statement_timeout=8000",
}


@dataclass(frozen=True)
class DemoDatabaseTarget:
    database_name: str
    host: str
    port: int
    user: str
    url: str


def database_url_for(manifest: DemoManifest, *, knowledge: bool = False) -> str:
    database_name = (
        manifest.knowledge_database_name if knowledge else manifest.database_name
    )
    return (
        f"postgresql+psycopg://{quote(manifest.pg_user, safe='')}@"
        f"{manifest.pg_host}:{manifest.pg_port}/{database_name}"
    )


def validate_database_url(manifest: DemoManifest, url: str) -> DemoDatabaseTarget:
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise DemoDatabaseError("invalid database port") from exc
    if port is None:
        raise DemoDatabaseError("database port is required")
    database_name = parsed.path.removeprefix("/")
    if (
        parsed.scheme not in {"postgresql", "postgresql+psycopg"}
        or parsed.hostname != manifest.pg_host
        or port != manifest.pg_port
        or parsed.username != manifest.pg_user
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not database_name
        or "/" in database_name
        or database_name
        not in {manifest.database_name, manifest.knowledge_database_name}
    ):
        raise DemoDatabaseError("database URL is not an owned local target")
    return DemoDatabaseTarget(
        database_name=database_name,
        host=manifest.pg_host,
        port=port,
        user=manifest.pg_user,
        url=url,
    )


def database_marker(manifest: DemoManifest) -> str:
    return f"sidera-interview-demo:{manifest.run_id}"


def assert_database_ownership(
    manifest: DemoManifest,
    database_name: str,
    comment: str | None,
    *,
    owner: str | None = None,
) -> None:
    if database_name not in {
        manifest.database_name,
        manifest.knowledge_database_name,
    }:
        raise DemoDatabaseError("database name is not owned by this run")
    if comment != database_marker(manifest):
        raise DemoDatabaseError("database ownership marker mismatch")
    if owner is not None and owner != manifest.pg_user:
        raise DemoDatabaseError("database owner mismatch")


def assert_owned_database(manifest: DemoManifest, database_name: str) -> None:
    if database_name not in {
        manifest.database_name,
        manifest.knowledge_database_name,
    }:
        raise DemoDatabaseError("database name is not owned by this run")
    admin_url = _admin_database_url(manifest)
    engine = create_engine(
        admin_url,
        pool_pre_ping=True,
        connect_args=dict(_ADMIN_CONNECT_ARGS),
    )
    try:
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT datname, pg_catalog.shobj_description(oid, 'pg_database'), "
                    "pg_catalog.pg_get_userbyid(datdba) "
                    "FROM pg_catalog.pg_database WHERE datname = :database_name"
                ),
                {"database_name": database_name},
            ).one_or_none()
        if row is None:
            raise DemoDatabaseError("owned demo database does not exist")
        assert_database_ownership(
            manifest, str(row[0]), row[1], owner=str(row[2])
        )
    finally:
        engine.dispose()


def create_demo_databases(manifest: DemoManifest) -> None:
    engine = create_engine(
        _admin_database_url(manifest),
        pool_pre_ping=True,
        connect_args=dict(_ADMIN_CONNECT_ARGS),
    )
    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            for database_name in (
                manifest.database_name,
                manifest.knowledge_database_name,
            ):
                row = _database_metadata(connection, database_name)
                if row is None:
                    connection.exec_driver_sql(
                        f'CREATE DATABASE "{_quote_identifier(database_name)}" '
                        f'OWNER "{_quote_identifier(manifest.pg_user)}"'
                    )
                    connection.exec_driver_sql(
                        f"COMMENT ON DATABASE \"{_quote_identifier(database_name)}\" "
                        f"IS '{_quote_literal(database_marker(manifest))}'"
                    )
                else:
                    assert_database_ownership(
                        manifest, str(row[0]), row[1], owner=str(row[2])
                    )
    finally:
        engine.dispose()
    assert_owned_database(manifest, manifest.database_name)
    assert_owned_database(manifest, manifest.knowledge_database_name)


def _admin_database_url(manifest: DemoManifest) -> str:
    configured = os.environ.get("SIDERA_INTERVIEW_DEMO_ADMIN_DATABASE_URL")
    if configured:
        parsed = urlsplit(configured)
        try:
            port = parsed.port
        except ValueError as exc:
            raise DemoDatabaseError("invalid admin database port") from exc
        if (
            parsed.scheme not in {"postgresql", "postgresql+psycopg"}
            or parsed.hostname != manifest.pg_host
            or port != manifest.pg_port
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not parsed.path.removeprefix("/")
        ):
            raise DemoDatabaseError("admin database URL is not local")
        return configured
    return (
        f"postgresql+psycopg://{quote(manifest.pg_user, safe='')}@"
        f"{manifest.pg_host}:{manifest.pg_port}/postgres"
    )


def _database_metadata(connection: Connection, database_name: str):
    return connection.execute(
        text(
            "SELECT datname, pg_catalog.shobj_description(oid, 'pg_database'), "
            "pg_catalog.pg_get_userbyid(datdba) "
            "FROM pg_catalog.pg_database WHERE datname = :database_name"
        ),
        {"database_name": database_name},
    ).one_or_none()


def _quote_identifier(value: str) -> str:
    return value.replace('"', '""')


def _quote_literal(value: str) -> str:
    return value.replace("'", "''")
