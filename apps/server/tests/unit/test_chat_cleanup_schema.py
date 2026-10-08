from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from sidera_product.postgres import PostgresProductStore
from tests.integration import test_chat_api as chat


class CleanupConnection:
    def __init__(self):
        self.rollbacks = []
        self.statements = []

    def rollback(self):
        self.rollbacks.append(True)

    def execute(self, statement):
        statement_text = str(statement)
        self.statements.append(statement_text)
        if statement_text == "SHOW transaction_read_only":
            return SimpleNamespace(scalar_one=lambda: "on")
        assert statement_text.startswith("SELECT ")
        return SimpleNamespace(mappings=lambda: [])


@pytest.fixture
def cleanup_boundary(no_database_connections, monkeypatch):
    def build(selected_heads):
        connection = CleanupConnection()
        product_guard = SimpleNamespace(connection=connection, target=object())
        agents_guard = object()
        row_reads = []

        def verify_objects(actual_connection, target):
            assert actual_connection is connection and target is product_guard.target
            return {"heads": selected_heads}

        def guarded_rows(guard, table, fields, *, where, parameters):
            assert guard is agents_guard
            row_reads.append(table)
            return []

        monkeypatch.setattr(chat, "verify_objects", verify_objects)
        monkeypatch.setattr(chat, "guarded_rows", guarded_rows)
        lab = chat.ChatLab(
            runtime=SimpleNamespace(
                settings=SimpleNamespace(
                    workspace_id=uuid4(), local_principal_id="unit-chat-user"
                )
            ),
            native=SimpleNamespace(harness_id="unit-chat-harness"),
            secrets=None,
            registered=None,
            server=None,
            knowledge=None,
            guards={"product": product_guard, "agents": agents_guard},
            evidence=None,
        )
        return lab, connection, row_reads

    return build


def test_owned_cleanup_reads_empty_rows_at_packaged_product_head(cleanup_boundary):
    heads = ScriptDirectory.from_config(
        Config(str(PostgresProductStore.alembic_config_path()))
    ).get_heads()
    assert heads == ["0004_configuration_requests"]
    lab, connection, row_reads = cleanup_boundary(heads)

    assert lab.owned_cleanup_facts() == {
        "conversations": [],
        "turns": [],
        "native_runs": [],
        "original_stop_events": [],
        "installations": [],
        "reservations": [],
    }
    assert connection.rollbacks == [True, True]
    assert len(connection.statements) == 3
    assert connection.statements[0] == "SHOW transaction_read_only"
    assert "FROM product.conversations" in connection.statements[1]
    assert "FROM product.chat_turns" in connection.statements[2]
    assert row_reads == [
        "agents.runs",
        "agents.run_events",
        "agents.runtime_installations",
        "agents.run_reservations",
    ]


@pytest.mark.parametrize(
    "heads",
    [
        ["0003_conversations"],
        ["9999_future"],
        [],
        ["0004_configuration_requests", "0003_conversations"],
    ],
    ids=["old-head", "unknown-future-head", "empty-heads", "multiple-heads"],
)
def test_owned_cleanup_rejects_unsupported_heads_before_durable_reads(
    cleanup_boundary, heads
):
    lab, connection, row_reads = cleanup_boundary(heads)

    with pytest.raises(AssertionError):
        lab.owned_cleanup_facts()

    assert connection.rollbacks == [True, True]
    assert connection.statements == ["SHOW transaction_read_only"]
    assert row_reads == []
