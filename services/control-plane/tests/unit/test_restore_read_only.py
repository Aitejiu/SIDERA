import pytest
from fastapi.testclient import TestClient

import sidera_control_plane.api.app as api_app
from sidera_control_plane.api.app import create_app
from sidera_control_plane.config import Settings


def test_restore_read_only_allows_health_but_rejects_commands() -> None:
    settings = Settings(
        database_url="postgresql+psycopg://test",
        restore_read_only=True,
    )

    with TestClient(create_app(settings)) as client:
        health = client.get("/healthz")
        command = client.post("/api/v1/tasks", json={})

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
        lambda database: calls.append(database),
    )
    settings = Settings(
        database_url="postgresql+psycopg://test",
        restore_read_only=True,
        restore_schema_validation=True,
    )

    with TestClient(create_app(settings)) as client:
        assert client.get("/healthz").status_code == 200

    assert len(calls) == 1
