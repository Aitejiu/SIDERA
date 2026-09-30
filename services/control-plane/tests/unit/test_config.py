from pathlib import Path
from uuid import UUID

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
        reconciler_batch_size=50,
        reconciler_poll_interval_seconds=2.0,
        gateway_batch_size=15,
        gateway_poll_interval_seconds=1.5,
        plan_transition_batch_size=12,
        plan_transition_poll_interval_seconds=1.25,
        plan_transition_lease_ttl_seconds=45,
        plan_transition_backoff_max_seconds=90,
        opencode_executable="opencode",
    )

    assert settings.assignment_lease_seconds == 30
    assert settings.assignment_lease_renew_interval_seconds == 10.0
    assert settings.worker_heartbeat_timeout_seconds == 45
    assert settings.scheduler_batch_size == 20
    assert settings.scheduler_poll_interval_seconds == 1.0
    assert settings.reconciler_batch_size == 50
    assert settings.reconciler_poll_interval_seconds == 2.0
    assert settings.gateway_batch_size == 15
    assert settings.gateway_poll_interval_seconds == 1.5
    assert settings.plan_transition_batch_size == 12
    assert settings.plan_transition_poll_interval_seconds == 1.25
    assert settings.plan_transition_lease_ttl_seconds == 45
    assert settings.plan_transition_backoff_max_seconds == 90
    assert settings.opencode_executable == "opencode"
    assert settings.opencode_request_timeout_seconds == 60.0
    assert settings.opencode_handoff_request_timeout_seconds == 60.0
    assert settings.opencode_stop_grace_seconds == 5.0

    with pytest.raises(ValueError):
        Settings(database_url="postgresql+psycopg://test", scheduler_batch_size=0)

    with pytest.raises(ValueError, match="less than half"):
        Settings(
            database_url="postgresql+psycopg://test",
            assignment_lease_seconds=30,
            assignment_lease_renew_interval_seconds=15,
        )


def test_workspace_settings_have_safe_defaults_and_validate_retention():
    settings = Settings(database_url="postgresql+psycopg://test")

    assert settings.workspace_root == Path(".sidera/workspaces")
    assert settings.workspace_retention_seconds == 86_400
    assert settings.git_executable == "git"

    with pytest.raises(ValueError):
        Settings(
            database_url="postgresql+psycopg://test",
            workspace_retention_seconds=0,
        )


def test_collaboration_settings_have_bounded_defaults():
    settings = Settings(database_url="postgresql+psycopg://test")

    assert settings.collaboration_governance_enabled is False
    assert settings.collaboration_workspace_id is None
    assert settings.collaboration_batch_size == 20
    assert settings.collaboration_poll_seconds == 1.0
    assert settings.collaboration_lease_ttl_seconds == 60
    assert settings.collaboration_evaluation_timeout_seconds == 30.0
    assert settings.collaboration_max_projection_bytes == 262_144

    with pytest.raises(ValueError, match="evaluation timeout"):
        Settings(
            database_url="postgresql+psycopg://test",
            collaboration_lease_ttl_seconds=30,
            collaboration_evaluation_timeout_seconds=30,
        )

    with pytest.raises(ValueError):
        Settings(
            database_url="postgresql+psycopg://test",
            collaboration_batch_size=201,
        )


def test_collaboration_governance_rollout_switch_can_be_enabled():
    settings = Settings(
        database_url="postgresql+psycopg://test",
        collaboration_governance_enabled=True,
    )

    assert settings.collaboration_governance_enabled is True


def test_collaboration_worker_workspace_scope_comes_from_environment(monkeypatch):
    workspace_id = UUID("6b75d8d9-6042-40d8-a393-c9969037472f")
    monkeypatch.setenv("SIDERA_DATABASE_URL", "postgresql+psycopg://test")
    monkeypatch.setenv("SIDERA_COLLABORATION_WORKSPACE_ID", str(workspace_id))

    settings = Settings()  # pyright: ignore[reportCallIssue]

    assert settings.collaboration_workspace_id == workspace_id


def test_gateway_active_session_sync_workspace_comes_from_environment(monkeypatch):
    workspace_id = UUID("6b75d8d9-6042-40d8-a393-c9969037472f")
    monkeypatch.setenv("SIDERA_DATABASE_URL", "postgresql+psycopg://test")
    monkeypatch.setenv("SIDERA_GATEWAY_SYNC_WORKSPACE_ID", str(workspace_id))

    settings = Settings()  # pyright: ignore[reportCallIssue]

    assert settings.gateway_sync_workspace_id == workspace_id


def test_notification_settings_have_bounded_defaults_and_validation():
    settings = Settings(database_url="postgresql+psycopg://test")

    assert settings.notification_enabled is False
    assert settings.notification_policy_version == 1
    assert settings.notification_blocked_grace_seconds == 300
    assert settings.notification_reconcile_batch_size == 50
    assert settings.notification_poll_seconds == 1.0
    assert settings.notification_cursor_lease_seconds == 30
    assert settings.notification_due_sweep_seconds == 15.0
    assert settings.notification_baseline_required is True
    assert settings.notification_delivery_batch_size == 50
    assert settings.notification_claim_lease_seconds == 30
    assert settings.notification_delivery_max_attempts == 10
    assert settings.notification_delivery_max_window_seconds == 86_400
    assert settings.notification_retry_base_seconds == 2
    assert settings.notification_retry_backoff_max_seconds == 300

    with pytest.raises(ValueError):
        Settings(
            database_url="postgresql+psycopg://test",
            notification_blocked_grace_seconds=59,
        )
    with pytest.raises(ValueError):
        Settings(
            database_url="postgresql+psycopg://test",
            notification_blocked_grace_seconds=86_401,
        )
    with pytest.raises(ValueError):
        Settings(
            database_url="postgresql+psycopg://test",
            notification_reconcile_batch_size=201,
        )
    with pytest.raises(ValueError):
        Settings(
            database_url="postgresql+psycopg://test",
            notification_poll_seconds=0,
        )
    with pytest.raises(ValueError):
        Settings(
            database_url="postgresql+psycopg://test",
            notification_policy_version=2,
        )
    with pytest.raises(ValueError):
        Settings(
            database_url="postgresql+psycopg://test",
            notification_delivery_batch_size=201,
        )
    with pytest.raises(ValueError):
        Settings(
            database_url="postgresql+psycopg://test",
            notification_claim_lease_seconds=4,
        )
    with pytest.raises(ValueError):
        Settings(
            database_url="postgresql+psycopg://test",
            notification_delivery_max_attempts=0,
        )
    with pytest.raises(ValueError):
        Settings(
            database_url="postgresql+psycopg://test",
            notification_delivery_max_window_seconds=0,
        )
