"""Resolve CP-owned schema resources from source or a noneditable wheel."""

from importlib.resources import files
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import Base


def migration_config() -> Config:
    package_root = Path(str(files("sidera_control_plane")))
    config_path = package_root / "alembic.ini"
    if not config_path.is_file():
        config_path = Path(__file__).resolve().parents[3] / "alembic.ini"
    if not config_path.is_file() or not (config_path.parent / "migrations").is_dir():
        raise RuntimeError("control_plane_migration_resources_unavailable")
    config = Config(str(config_path))
    config.set_main_option("prepend_sys_path", "")
    return config


def migrate_database(database: Database) -> None:
    config = migration_config()
    with database.engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


def schema_ready(database: Database) -> bool:
    try:
        expected = tuple(
            sorted(ScriptDirectory.from_config(migration_config()).get_heads())
        )
        if not expected:
            return False
        with database.engine.connect() as connection:
            actual = tuple(
                connection.execute(
                    text("SELECT version_num FROM alembic_version ORDER BY version_num")
                )
                .scalars()
                .all()
            )
            if actual != expected:
                return False
            columns = set(
                connection.execute(
                    text(
                        "SELECT table_name, column_name FROM information_schema.columns "
                        "WHERE table_schema = 'public'"
                    )
                ).all()
            )
        required = {
            (table.name, column.name)
            for table in Base.metadata.tables.values()
            for column in table.columns
        }
        return bool(required) and required.issubset(columns)
    except Exception:  # noqa: BLE001 - Readiness must not disclose driver/DSN details.
        return False
