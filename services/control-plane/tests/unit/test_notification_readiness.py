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
