import importlib.util
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import ValidationError
from sqlalchemy import BigInteger
from sqlalchemy.dialects import postgresql


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
        "product.local_bootstraps",
        "product.agent_grants",
        "product.tool_receipts",
        "product.conversations",
        "product.chat_turns",
        "product.configuration_requests",
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
    assert scripts.get_heads() == ["0004_configuration_requests"]
    assert Path(config_path).is_file()


def test_authority_fencing_token_matches_migration_bigint():
    from sidera_product.postgres.models import AgentGrantRow

    assert type(AgentGrantRow.__table__.c.fencing_token.type) is BigInteger


def test_actual_grant_insert_serializes_original_record_column_keys_without_connecting(
    settings_values,
    tmp_path,
    no_database_connections,
    monkeypatch,
):
    from sidera_product.postgres import authority_store as module
    from sidera_product.postgres.models import AgentGrantRow
    from tests.unit.test_product_grants import grant_fixture

    lab = grant_fixture(settings_values, tmp_path)
    record = next(iter(lab.store.rows.values()))
    keys = list(AgentGrantRow.__table__.columns.keys())
    assert keys == [column.key for column in AgentGrantRow.__table__.columns]
    assert len(keys) == 25 and all(isinstance(key, str) for key in keys)
    expected = {key: getattr(record, key) for key in keys if key != "profile_ref"}
    expected["profile_ref"] = record.profile_ref.model_dump(mode="json")
    store_class, _ = owned_storage()
    owner = store_class(str(settings_values["product_database_url"]))
    statements = []

    class BoundarySession:
        def __init__(self, engine):
            assert engine is owner.engine

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def begin(self):
            return nullcontext(self)

        def execute(self, statement):
            values = statement.compile(dialect=postgresql.dialect()).params
            statements.append(values)
            return SimpleNamespace(scalar_one_or_none=lambda: AgentGrantRow(**values))

    monkeypatch.setattr(module, "Session", BoundarySession)
    try:
        saved, winner = module.PostgresAuthorityStore(owner.engine).save_grant(record)
        assert winner is True and saved == record
        assert statements == [expected] and lab.calls == []
    finally:
        owner.close()


@pytest.mark.parametrize("field", ("source", "role", "outcome"))
def test_persisted_authority_literals_keep_actual_validation(
    settings_values,
    tmp_path,
    no_database_connections,
    field,
):
    from sidera_product.postgres import authority_store as module
    from sidera_product.postgres.models import AgentGrantRow, ToolReceiptRow
    from tests.unit.test_product_grants import grant_fixture

    lab = grant_fixture(settings_values, tmp_path)
    record = next(iter(lab.store.rows.values()))
    if field == "outcome":
        row = ToolReceiptRow(
            receipt_id=uuid4(),
            grant_id=record.grant_id,
            run_id=lab.run[0].run_id,
            response_id=uuid4(),
            tool_name="sidera_submit_goal",
            logical_request_id=uuid4(),
            payload_hash="0" * 64,
            command_id=uuid4(),
            intent={},
            outcome="invalid",
            result=None,
            created_at=record.created_at,
            updated_at=record.created_at,
        )
        with pytest.raises(ValidationError):
            module._tool_receipt(row)
    else:
        values = {
            column.key: getattr(record, column.key)
            for column in AgentGrantRow.__table__.columns
            if column.key != "profile_ref"
        }
        values["profile_ref"] = record.profile_ref.model_dump(mode="json")
        values[field] = "invalid"
        with pytest.raises(ValidationError):
            module._agent_grant(AgentGrantRow(**values))
    assert lab.calls == []
