from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.engine import Connection
from sqlalchemy.schema import CreateSchema

from sidera_agents.postgres.models import Base

config = context.config
target_metadata = Base.metadata


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
