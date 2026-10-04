from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.engine import Connection
from sqlalchemy.schema import CreateSchema

from sidera_product.postgres.models import Base

config = context.config


def run(connection: Connection) -> None:
    connection.execute(CreateSchema("product", if_not_exists=True))
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        include_schemas=True,
        version_table_schema="product",
    )
    with context.begin_transaction():
        context.run_migrations()


connection = config.attributes.get("connection")
if isinstance(connection, Connection):
    run(connection)
else:
    url = config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError("An explicit product migration target is required")
    engine = create_engine(url, hide_parameters=True, echo=False)
    try:
        with engine.begin() as connection:
            run(connection)
    finally:
        engine.dispose()
