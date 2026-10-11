import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from sidera_control_plane.persistence.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

provided_connection = config.attributes.get("connection")
database_url = None
if provided_connection is None:
    database_url = os.environ.get("SIDERA_DATABASE_URL")
    if database_url is None:
        raise RuntimeError("SIDERA_DATABASE_URL is required for migrations")
    # Preserve legacy CLI URL interpolation without reading it for an injected connection.
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    if database_url is None:
        raise RuntimeError("An explicit URL is required for offline migrations")
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def _run_on_connection(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    if provided_connection is not None:
        _run_on_connection(provided_connection)
        return
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        _run_on_connection(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
