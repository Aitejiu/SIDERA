from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from sidera_model_connections.postgres.models import SCHEMA_NAME, Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata
PACKAGE_TABLES = {table.name for table in target_metadata.tables.values()}


def _database_url() -> str:
    production_url = os.environ.get("SIDERA_CONFIGURATION_DATABASE_URL")
    test_url = os.environ.get("SIDERA_CONFIGURATION_TEST_DATABASE_URL")
    if production_url and test_url:
        if make_url(production_url) != make_url(test_url):
            raise RuntimeError("conflicting configuration database URL targets")
        return production_url

    value = production_url or test_url
    if not value:
        raise RuntimeError("an explicit configuration database URL is required")
    make_url(value)
    return value


def _include_name(name: str | None, type_: str, parent_names: dict[str, str]) -> bool:
    if type_ == "schema":
        return name == SCHEMA_NAME
    if type_ == "table":
        return (
            parent_names.get("schema_name") == SCHEMA_NAME
            and name in PACKAGE_TABLES
        )
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
        include_name=_include_name,
        version_table_schema=SCHEMA_NAME,
    )
    context.execute(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA_NAME}"')
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_database_url(), poolclass=NullPool)
    try:
        with engine.connect() as connection:
            connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA_NAME}"'))
            connection.commit()
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                include_schemas=True,
                include_name=_include_name,
                version_table_schema=SCHEMA_NAME,
                compare_type=True,
            )
            with context.begin_transaction():
                context.run_migrations()
                if not context.get_context().get_current_heads():
                    connection.exec_driver_sql(
                        f'DROP TABLE IF EXISTS "{SCHEMA_NAME}"."alembic_version"'
                    )
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
