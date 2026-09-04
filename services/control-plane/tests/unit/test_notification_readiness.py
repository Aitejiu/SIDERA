from __future__ import annotations

from sidera_control_plane.notification_readiness import (
    NotificationReadinessReport,
    evaluate_notification_readiness,
)


def test_readiness_accepts_web_only_workspace_without_external_route() -> None:
    report = evaluate_notification_readiness(
        migration_ready=True,
        notification_enabled=True,
        policy_version=1,
        baseline_required=True,
        baseline_complete=True,
        cursor_lag=0,
        active_external_routes=0,
    )

    assert isinstance(report, NotificationReadinessReport)
    assert report.ready is True
    assert report.checks[-1].code == "web_only_ready"


def test_readiness_fails_closed_for_disabled_policy_baseline_and_lag() -> None:
    report = evaluate_notification_readiness(
        migration_ready=False,
        notification_enabled=False,
        policy_version=2,
        baseline_required=True,
        baseline_complete=False,
        cursor_lag=4,
        active_external_routes=1,
    )

    assert report.ready is False
    assert [check.code for check in report.checks] == [
        "notification_migration_unready",
        "notification_disabled",
        "notification_baseline_required",
        "notification_cursor_lagging",
        "notification_channel_ready",
        "notification_external_route_configured",
    ]


def test_readiness_report_does_not_include_route_or_secret_details() -> None:
    report = evaluate_notification_readiness(
        migration_ready=True,
        notification_enabled=True,
        policy_version=1,
        baseline_required=False,
        baseline_complete=False,
        cursor_lag=0,
        active_external_routes=1,
    )

    payload = report.to_payload()

    assert payload["status"] == "ready"
    assert "route" not in str(payload["checks"][-1]["summary"]).lower()
    assert "secret" not in str(payload).lower()


def test_readiness_reports_stable_channel_cursor_and_delivery_diagnostics() -> None:
    report = evaluate_notification_readiness(
        migration_ready=True,
        notification_enabled=True,
        policy_version=1,
        baseline_required=False,
        baseline_complete=False,
        cursor_lag=3,
        cursor_last_error="notification_observation_invalid",
        active_external_routes=1,
        unavailable_channels=1,
        incompatible_channels=1,
        delivery_backlog=4,
        exhausted_deliveries=2,
    )

    assert report.ready is False
    assert [check.code for check in report.checks] == [
        "notification_migration_ready",
        "notification_policy_ready",
        "notification_baseline_ready",
        "notification_cursor_error",
        "notification_channel_degraded",
        "notification_delivery_exhausted",
    ]
    payload = report.to_payload()
    assert "notification_observation_invalid" in str(payload)
    assert "secret" not in str(payload).lower()
    assert "token" not in str(payload).lower()
