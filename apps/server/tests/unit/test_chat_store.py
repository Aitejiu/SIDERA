import importlib.util
from contextlib import contextmanager
from threading import RLock
from types import SimpleNamespace
from uuid import UUID

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import ValidationError
from sidera_agents.contracts import ProfileRef
from sqlalchemy.dialects import postgresql

from sidera_product.postgres.models import Base
from sidera_product.postgres.store import PostgresProductStore


def chat_contracts():
    assert importlib.util.find_spec("sidera_product.chat") is not None, (
        "N19 requires the owned persistent chat contract"
    )
    from sidera_product.chat import contracts

    return contracts


def test_empty_create_accepts_exact_profile_and_legacy_aliases():
    contracts = chat_contracts()
    ref = ProfileRef(
        workspace_id=UUID("29220940-526b-4503-a8db-0e005b690ee0"),
        profile_id=UUID("a1574ea4-0b60-4737-80ea-10733695f507"),
        revision=7,
    )
    camel = contracts.CreateThreadBody.model_validate(
        {
            "title": "Pinned conversation",
            "contextRefs": ["task:original"],
            "profileRef": ref,
        }
    )
    snake = contracts.CreateThreadBody.model_validate(
        {
            "title": "Pinned conversation",
            "context_refs": ["task:original"],
            "profile_ref": ref,
        }
    )
    assert camel == snake
    assert camel.profile_ref == ref
    assert camel.context_refs == ("task:original",)
    assert contracts.CreateThreadBody().profile_ref is None


@pytest.mark.parametrize(
    "payload",
    [
        {"title": "x" * 121},
        {"contextRefs": [" "]},
        {"contextRefs": ["x" * 201]},
        {"contextRefs": ["x"] * 21},
        {"runId": "not-a-client-run-selector"},
        {"profileRef": {"workspace_id": "invalid"}},
    ],
)
def test_thread_body_rejects_unbounded_or_execution_owned_fields(payload):
    contracts = chat_contracts()
    with pytest.raises(ValidationError):
        contracts.CreateThreadBody.model_validate(payload)


@pytest.mark.parametrize(
    "operation_id",
    ["browser-fallback:0001", "7974b8a4-f536-437f-a2bd-15b7252dd649", " x ", "x" * 128],
)
def test_turn_body_preserves_every_opaque_operation_string(operation_id):
    contracts = chat_contracts()
    body = contracts.StartTurnBody.model_validate(
        {"input": "hello", "operationId": operation_id}
    )
    assert body.operation_id == operation_id
    assert body.input == "hello"
    assert contracts.StartTurnBody(input="hello", operation_id=operation_id) == body


@pytest.mark.parametrize(
    "payload",
    [
        {"input": ""},
        {"input": "x" * 100_001},
        {"input": "hello", "operationId": ""},
        {"input": "hello", "operationId": "x" * 129},
        {"input": "hello", "grantId": "not-a-client-grant-selector"},
        {"input": "hello", "runId": "not-a-client-run-selector"},
    ],
)
def test_turn_body_rejects_unbounded_or_foreign_authority_fields(payload):
    contracts = chat_contracts()
    with pytest.raises(ValidationError):
        contracts.StartTurnBody.model_validate(payload)


def test_chat_rows_are_owned_journals_not_a_native_event_or_execution_store():
    assert {"product.conversations", "product.chat_turns"}.issubset(
        Base.metadata.tables
    )
    conversation = Base.metadata.tables["product.conversations"]
    turn = Base.metadata.tables["product.chat_turns"]
    assert {
        "id",
        "workspace_id",
        "principal_id",
        "profile_ref",
        "active_turn_id",
        "grant_id",
        "start_operation_id",
        "run_id",
        "version",
        "stop_operation_id",
    }.issubset(conversation.columns.keys())
    assert {
        "id",
        "conversation_id",
        "workspace_id",
        "operation_id",
        "native_operation_id",
        "request_fingerprint",
        "input_text",
        "request_state",
        "native_request",
        "response_id",
    }.issubset(turn.columns.keys())
    forbidden = {
        "token",
        "bearer",
        "credential",
        "native_events",
        "execution_id",
        "model_config",
        "acp_handle",
    }
    assert forbidden.isdisjoint(conversation.columns.keys())
    assert forbidden.isdisjoint(turn.columns.keys())
    assert all(not table.foreign_keys for table in (conversation, turn))
    assert any(
        constraint.name == "chat_turn_original_operation"
        and {column.key for column in constraint.columns}
        == {"workspace_id", "conversation_id", "operation_id"}
        for constraint in turn.constraints
    )


def test_chat_forward_migration_is_after_accepted_local_authority_head():
    config = Config(str(PostgresProductStore.alembic_config_path()))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == ["0003_conversations"]
    assert (
        scripts.get_revision("0003_conversations").down_revision
        == "0002_local_authority_tools"
    )


class JournalBoundary:
    """Control SQL I/O only; real PostgreSQL proves unique admission separately."""

    def __init__(self, engine):
        self.engine = engine
        self.rows = {}
        self.queries = []
        self.open_sessions = 0
        self.transaction_lock = RLock()

    def session(self, engine):
        assert engine is self.engine
        owner = self

        class BoundarySession:
            def __enter__(self):
                owner.open_sessions += 1
                return self

            def __exit__(self, *args):
                owner.open_sessions -= 1

            @contextmanager
            def begin(self):
                with owner.transaction_lock:
                    yield self

            def add(self, row):
                owner.rows[(type(row), row.id)] = row

            def flush(self):
                return None

            def get(self, model, identity, **options):
                return owner.rows.get((model, identity))

            def execute(self, statement):
                values = statement.compile(dialect=postgresql.dialect()).params
                owner.queries.append(values)
                if statement.is_insert or statement.is_update:
                    from sidera_product.postgres.models import ProductDefaultsRow

                    assert statement.table.name == "product_defaults"
                    row = ProductDefaultsRow(
                        **{
                            column.key: values[column.key]
                            for column in ProductDefaultsRow.__table__.columns
                        }
                    )
                    owner.rows[(ProductDefaultsRow, row.workspace_id)] = row
                    return SimpleNamespace(scalar_one_or_none=lambda: row)
                model = statement.column_descriptions[0]["entity"]
                from sidera_product.postgres.models import ToolReceiptRow

                if model is ToolReceiptRow:
                    assert set(values) == {"grant_id_1", "run_id_1", "response_id_1"}
                    rows = [
                        row
                        for (kind, _), row in owner.rows.items()
                        if kind is model
                        and row.grant_id == values["grant_id_1"]
                        and row.run_id == values["run_id_1"]
                        and row.response_id == values["response_id_1"]
                    ]
                    return SimpleNamespace(
                        scalars=lambda: SimpleNamespace(all=lambda: rows)
                    )
                assert "workspace_id_1" in values and "principal_id_1" in values
                rows = [
                    row
                    for (kind, _), row in owner.rows.items()
                    if kind is model
                    and row.workspace_id == values["workspace_id_1"]
                    and row.principal_id == values["principal_id_1"]
                    and ("id_1" not in values or row.id == values["id_1"])
                    and (
                        "conversation_id_1" not in values
                        or row.conversation_id == values["conversation_id_1"]
                    )
                    and (
                        "operation_id_1" not in values
                        or row.operation_id == values["operation_id_1"]
                    )
                ]
                return SimpleNamespace(
                    scalar_one_or_none=lambda: rows[0] if rows else None,
                    scalars=lambda: SimpleNamespace(all=lambda: rows),
                )

        return BoundarySession()


def test_receipt_query_is_read_only_and_scoped_to_all_original_bindings(
    settings_values, monkeypatch, no_database_connections
):
    from datetime import UTC, datetime
    from uuid import uuid4

    from sidera_product.postgres import authority_store
    from sidera_product.postgres.models import ToolReceiptRow

    product = PostgresProductStore(str(settings_values["product_database_url"]))
    boundary = JournalBoundary(product.engine)
    monkeypatch.setattr(authority_store, "Session", boundary.session)
    grant, run, response = uuid4(), uuid4(), uuid4()
    now = datetime(2026, 10, 5, tzinfo=UTC)
    originals = []
    for variant in ("original", "grant", "run", "response"):
        row = ToolReceiptRow(
            receipt_id=uuid4(),
            grant_id=grant if variant != "grant" else uuid4(),
            run_id=run if variant != "run" else uuid4(),
            response_id=response if variant != "response" else uuid4(),
            tool_name="sidera_submit_goal",
            logical_request_id=uuid4(),
            payload_hash="f" * 64,
            command_id=uuid4(),
            intent={},
            outcome="unknown",
            result=None,
            created_at=now,
            updated_at=now,
        )
        boundary.rows[(ToolReceiptRow, row.receipt_id)] = row
        originals.append(row)
    try:
        store = authority_store.PostgresAuthorityStore(product.engine)
        assert hasattr(store, "list_receipts"), (
            "N19 needs exact historical receipt query"
        )
        result = store.list_receipts(grant_id=grant, run_id=run, response_id=response)
        assert [row.receipt_id for row in result] == [originals[0].receipt_id]
        assert boundary.open_sessions == 0 and len(boundary.rows) == 4
        assert boundary.queries == [
            {"grant_id_1": grant, "run_id_1": run, "response_id_1": response}
        ]
    finally:
        product.close()
