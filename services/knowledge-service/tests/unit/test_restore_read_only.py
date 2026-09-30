import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault(
    "SIDERA_KNOWLEDGE_DATABASE_URL",
    "postgresql+psycopg://test",
)

import sidera_knowledge.api.app as api_app
from sidera_knowledge.api.app import create_app
from sidera_knowledge.config import KnowledgeSettings


def test_restore_read_only_allows_health_but_rejects_commands() -> None:
    settings = KnowledgeSettings(
        database_url="postgresql+psycopg://test",
        restore_read_only=True,
    )

    with TestClient(create_app(settings)) as client:
        health = client.get("/health/live")
        command = client.post("/api/v1/commands", json={})

    assert health.status_code == 200
    assert command.status_code == 409
    assert command.json()["error"]["code"] == "restore_read_only"
    assert command.json()["error"]["details"] == {"restore_mode": "read_only"}


def test_restore_read_only_validates_schema_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(
        api_app,
        "_validate_restore_schema",
        lambda database, head: calls.append((database, head)),
    )
    settings = KnowledgeSettings(
        database_url="postgresql+psycopg://test",
        restore_read_only=True,
        restore_schema_validation=True,
    )

    with TestClient(create_app(settings)) as client:
        assert client.get("/health/live").status_code == 200

    assert len(calls) == 1
