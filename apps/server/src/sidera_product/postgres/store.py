from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from threading import Lock
from uuid import UUID

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .models import Base, ProductDefaultsRow


class ProductStoreError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ProductDefaults:
    workspace_id: UUID
    main_profile_id: UUID | None
    planner_profile_id: UUID | None
    worker_profile_id: UUID | None
    version: int


def _defaults(row: ProductDefaultsRow) -> ProductDefaults:
    return ProductDefaults(
        workspace_id=row.workspace_id,
        main_profile_id=row.main_profile_id,
        planner_profile_id=row.planner_profile_id,
        worker_profile_id=row.worker_profile_id,
        version=row.version,
    )


class PostgresProductStore:
    def __init__(self, database_url: str) -> None:
        self.engine = create_engine(
            database_url, pool_pre_ping=True, echo=False, hide_parameters=True
        )
        self._close_lock = Lock()
        self._closed = False

    @staticmethod
    def alembic_config_path() -> Path:
        packaged = Path(str(files("sidera_product"))) / "alembic.ini"
        if packaged.is_file():
            return packaged
        return Path(__file__).resolve().parents[3] / "alembic.ini"

    def migrate(self) -> None:
        try:
            config = Config(str(self.alembic_config_path()))
            with self.engine.begin() as connection:
                config.attributes["connection"] = connection
                command.upgrade(config, "head")
        except SQLAlchemyError:
            raise ProductStoreError("product_migration_failed") from None

    def readiness(self) -> bool:
        if self._closed:
            return False
        config = Config(str(self.alembic_config_path()))
        expected = set(ScriptDirectory.from_config(config).get_heads())
        try:
            with self.engine.connect() as connection:
                inspector = inspect(connection)
                required = {table.name for table in Base.metadata.tables.values()}
                if not required.issubset(inspector.get_table_names(schema="product")):
                    return False
                heads = set(
                    connection.execute(
                        text("SELECT version_num FROM product.alembic_version")
                    ).scalars()
                )
                return heads == expected
        except SQLAlchemyError:
            return False

    def get_defaults(self, workspace_id: UUID) -> ProductDefaults | None:
        try:
            with Session(self.engine) as session:
                row = session.get(ProductDefaultsRow, workspace_id)
                return None if row is None else _defaults(row)
        except SQLAlchemyError:
            raise ProductStoreError("product_store_unavailable") from None

    def write_defaults(
        self,
        workspace_id: UUID,
        *,
        expected_version: int,
        main_profile_id: UUID | None,
        planner_profile_id: UUID | None,
        worker_profile_id: UUID | None,
    ) -> ProductDefaults:
        if expected_version < 0:
            raise ProductStoreError("invalid_expected_version")
        values = {
            "workspace_id": workspace_id,
            "main_profile_id": main_profile_id,
            "planner_profile_id": planner_profile_id,
            "worker_profile_id": worker_profile_id,
            "version": expected_version + 1,
        }
        statement = (
            insert(ProductDefaultsRow).values(**values).on_conflict_do_nothing()
            if expected_version == 0
            else update(ProductDefaultsRow)
            .where(
                ProductDefaultsRow.workspace_id == workspace_id,
                ProductDefaultsRow.version == expected_version,
            )
            .values(**values)
        )
        try:
            with Session(self.engine) as session, session.begin():
                row = session.execute(
                    statement.returning(ProductDefaultsRow)
                ).scalar_one_or_none()
                if not isinstance(row, ProductDefaultsRow):
                    raise ProductStoreError("version_conflict")
                return _defaults(row)
        except SQLAlchemyError:
            raise ProductStoreError("product_store_unavailable") from None

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self.engine.dispose()
            self._closed = True
