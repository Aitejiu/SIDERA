from __future__ import annotations

import hmac
import os
from pathlib import Path

import pytest
import sqlalchemy
from alembic import command
from alembic.config import Config
from sqlalchemy import JSON, inspect
from sqlalchemy.engine import URL, make_url

from sidera_model_connections.postgres.models import SCHEMA_NAME, Base


def _alembic_config() -> Config:
    package_root = Path(__file__).resolve().parents[2]
    return Config(str(package_root / "alembic.ini"))


def _serialized_url_is_preserved(name: str, url: URL) -> bool:
    actual = os.environ.get(name, "")
    expected = url.render_as_string(hide_password=False)
    return hmac.compare_digest(actual, expected)


def test_metadata_has_five_secret_free_postgres_tables() -> None:
    names = {table.name for table in Base.metadata.tables.values()}
    assert names == {
        "connections",
        "connection_revisions",
        "credentials",
        "credential_writes",
        "credential_events",
    }
    assert all(table.schema == SCHEMA_NAME for table in Base.metadata.tables.values())
    for table in Base.metadata.tables.values():
        for column in table.columns:
            assert not any(
                token in column.name.lower()
                for token in ("secret", "hash", "request", "payload", "body")
            )
            assert not isinstance(column.type, JSON)


def test_owned_database_migration_upgrade_downgrade_upgrade(engine) -> None:
    command.upgrade(_alembic_config(), "head")
    sentinel_name = "cfg2_task2_migration_sentinel"
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                f"CREATE TABLE IF NOT EXISTS public.{sentinel_name} (id integer PRIMARY KEY)"
            )

        command.downgrade(_alembic_config(), "base")
        with engine.connect() as connection:
            names = set(inspect(connection).get_table_names(schema=SCHEMA_NAME))
            assert names == set()
            assert connection.exec_driver_sql(
                "SELECT to_regnamespace('sidera_configuration') IS NOT NULL"
            ).scalar_one()
            assert inspect(connection).has_table(sentinel_name, schema="public")

        command.upgrade(_alembic_config(), "head")
        with engine.connect() as connection:
            names = set(inspect(connection).get_table_names(schema=SCHEMA_NAME))
            assert names == {
                "alembic_version",
                "connections",
                "connection_revisions",
                "credentials",
                "credential_writes",
                "credential_events",
            }
            assert inspect(connection).has_table(sentinel_name, schema="public")
        command.check(_alembic_config())
    finally:
        with engine.begin() as connection:
            connection.exec_driver_sql(f"DROP TABLE IF EXISTS public.{sentinel_name}")


def test_migration_refuses_conflicting_database_urls_before_connect(
    monkeypatch: pytest.MonkeyPatch,
    database_target: tuple[URL, str],
) -> None:
    owned_url, owned_name = database_target
    production_url = owned_url.set(
        database=f"{owned_name}_production_probe",
        password="cfg2-synthetic-production-password",
    )
    test_url = owned_url.set(
        database=f"{owned_name}_test_probe",
        password="cfg2-synthetic-test-password",
    )
    engine_calls: list[URL] = []

    class EngineConstructionObserved(RuntimeError):
        pass

    def engine_spy(url, *args, **kwargs):
        engine_calls.append(make_url(url))
        raise EngineConstructionObserved("engine spy stopped before connect")

    monkeypatch.setattr(sqlalchemy, "create_engine", engine_spy)
    monkeypatch.setenv(
        "SIDERA_CONFIGURATION_DATABASE_URL",
        production_url.render_as_string(hide_password=False),
    )
    monkeypatch.setenv(
        "SIDERA_CONFIGURATION_TEST_DATABASE_URL",
        test_url.render_as_string(hide_password=False),
    )

    assert _serialized_url_is_preserved(
        "SIDERA_CONFIGURATION_DATABASE_URL", production_url
    )
    assert _serialized_url_is_preserved(
        "SIDERA_CONFIGURATION_TEST_DATABASE_URL", test_url
    )

    with pytest.raises(RuntimeError) as conflict:
        command.upgrade(_alembic_config(), "head")

    assert str(conflict.value) == "conflicting configuration database URL targets"
    assert engine_calls == []


@pytest.mark.parametrize(
    ("production_selected", "test_selected"),
    ((True, False), (False, True), (True, True)),
    ids=("production-only", "test-only", "identical-both"),
)
def test_migration_accepts_single_or_identical_database_url(
    monkeypatch: pytest.MonkeyPatch,
    database_target: tuple[URL, str],
    production_selected: bool,
    test_selected: bool,
) -> None:
    owned_url, _owned_name = database_target
    synthetic_url = owned_url.set(password="cfg2-synthetic-identical-password")
    engine_calls: list[URL] = []

    class EngineConstructionObserved(RuntimeError):
        pass

    def engine_spy(url, *args, **kwargs):
        engine_calls.append(make_url(url))
        raise EngineConstructionObserved("engine spy stopped before connect")

    monkeypatch.setattr(sqlalchemy, "create_engine", engine_spy)
    if production_selected:
        monkeypatch.setenv(
            "SIDERA_CONFIGURATION_DATABASE_URL",
            synthetic_url.render_as_string(hide_password=False),
        )
    else:
        monkeypatch.delenv("SIDERA_CONFIGURATION_DATABASE_URL", raising=False)
    if test_selected:
        monkeypatch.setenv(
            "SIDERA_CONFIGURATION_TEST_DATABASE_URL",
            synthetic_url.render_as_string(hide_password=False),
        )
    else:
        monkeypatch.delenv("SIDERA_CONFIGURATION_TEST_DATABASE_URL", raising=False)

    if production_selected:
        assert _serialized_url_is_preserved(
            "SIDERA_CONFIGURATION_DATABASE_URL", synthetic_url
        )
    if test_selected:
        assert _serialized_url_is_preserved(
            "SIDERA_CONFIGURATION_TEST_DATABASE_URL", synthetic_url
        )

    with pytest.raises(EngineConstructionObserved):
        command.upgrade(_alembic_config(), "head")

    assert len(engine_calls) == 1 and engine_calls[0] == synthetic_url
