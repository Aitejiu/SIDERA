import pytest
from fastapi.testclient import TestClient

from sidera_control_plane.api.app import create_app
from sidera_control_plane.config import Settings


def test_settings_require_database_url(monkeypatch):
    monkeypatch.delenv("SIDERA_DATABASE_URL", raising=False)

    try:
        Settings()  # pyright: ignore[reportCallIssue]
    except ValueError as exc:
        assert "database_url" in str(exc)
    else:
        raise AssertionError("Settings accepted a missing database URL")


def test_healthz_identifies_control_plane():
    settings = Settings(database_url="postgresql+psycopg://test")
    with TestClient(create_app(settings)) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "sidera-control-plane",
    }


def test_scheduler_settings_are_positive():
    settings = Settings(
        database_url="postgresql+psycopg://test",
        assignment_lease_seconds=30,
        worker_heartbeat_timeout_seconds=45,
        scheduler_batch_size=20,
        scheduler_poll_interval_seconds=1.0,
    )

    assert settings.assignment_lease_seconds == 30
    assert settings.worker_heartbeat_timeout_seconds == 45
    assert settings.scheduler_batch_size == 20
    assert settings.scheduler_poll_interval_seconds == 1.0

    with pytest.raises(ValueError):
        Settings(database_url="postgresql+psycopg://test", scheduler_batch_size=0)
