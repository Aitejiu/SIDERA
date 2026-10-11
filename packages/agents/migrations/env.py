from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.engine import Connection
from sqlalchemy.schema import CreateSchema

from sidera_agents.postgres.models import Base

config = context.config
target_metadata = Base.metadata


def _include_name(name: str | None, type_: str, parent_names: dict[str, str]) -> bool:
    if type_ == "schema":
        return name == "agents"
    if type_ == "table":
        return (
            parent_names.get("schema_name") == "agents"
            and parent_names.get("schema_qualified_table_name")
            in target_metadata.tables
        )
    return parent_names.get("schema_name") == "agents"


def _include_object(
    obj: object,
    name: str,
    type_: str,
    reflected: bool,
    compare_to: object | None,
) -> bool:
    table = obj if type_ == "table" else getattr(obj, "table", None)
    return (
        table is not None
        and table.schema == "agents"
        and table.key in target_metadata.tables
    )


def _run_migrations(connection: Connection) -> None:
    if connection.in_transaction():
        connection.execute(CreateSchema("agents", if_not_exists=True))
    else:
        with connection.begin():
            connection.execute(CreateSchema("agents", if_not_exists=True))
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_schemas=True,
        version_table_schema="agents",
        compare_type=True,
        include_name=_include_name,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _run_migrations(connection)
        return

    database_url = config.get_main_option("sqlalchemy.url")
    if not database_url:
        raise RuntimeError("An explicit Alembic connection or URL is required.")
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            _run_migrations(connection)
    finally:
        engine.dispose()


run_migrations_online()
