import importlib.util
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


def owned_storage():
    assert importlib.util.find_spec("sidera_product.postgres") is not None, (
        "N17 must provide its owned product store and migration resources"
    )
    from sidera_product.postgres import PostgresProductStore
    from sidera_product.postgres.models import Base

    return PostgresProductStore, Base


def test_owned_schema_has_no_foundation_rows_or_cross_schema_foreign_keys():
    _, base = owned_storage()
    assert set(base.metadata.tables) == {
        "product.product_defaults",
        "product.browser_sessions",
        "product.request_state",
    }
    defaults = base.metadata.tables["product.product_defaults"]
    assert set(defaults.columns.keys()) == {
        "workspace_id",
        "main_profile_id",
        "planner_profile_id",
        "worker_profile_id",
        "version",
    }
    assert all(not table.foreign_keys for table in base.metadata.tables.values())


def test_owned_store_is_inert_and_disposes_its_engine_once(
    settings_values, no_database_connections, monkeypatch
):
    store_class, _ = owned_storage()
    store = store_class(str(settings_values["product_database_url"]))
    disposed = []
    original = store.engine.dispose

    def dispose(*args, **kwargs):
        disposed.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(store.engine, "dispose", dispose)
    store.close()
    store.close()
    assert disposed == [True]


def test_owned_forward_migration_resources_match_readiness_head():
    store_class, _ = owned_storage()
    config_path = store_class.alembic_config_path()
    config = Config(str(config_path))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == ["0001_product_defaults_sessions"]
    assert Path(config_path).is_file()
