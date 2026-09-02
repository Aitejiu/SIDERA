from __future__ import annotations

from sidera_control_plane.collaboration_readiness import (
    CollaborationReadiness,
    ReadinessCheck,
)


def test_readiness_reports_stable_codes_and_redacts_sensitive_summaries() -> None:
    readiness = CollaborationReadiness(
        checks=(
            lambda: ReadinessCheck(
                name="database",
                ready=False,
                code="database_unavailable",
                summary="connection failed for token=sk-test-secret",
            ),
            lambda: ReadinessCheck(
                name="reference_runtime",
                ready=True,
                code="ok",
                summary="reference runtime is available",
            ),
        )
    )

    report = readiness.check()
    payload = report.to_payload()

    assert report.ready is False
    assert payload["status"] == "not_ready"
    assert payload["checks"][0] == {
        "name": "database",
        "ready": False,
        "code": "database_unavailable",
        "summary": "connection failed for [secret redacted]",
    }
    assert "sk-test-secret" not in str(payload)


def test_readiness_preserves_check_order() -> None:
    readiness = CollaborationReadiness(
        checks=(
            lambda: ReadinessCheck(
                name="z_last",
                ready=True,
                code="ok",
                summary="last",
            ),
            lambda: ReadinessCheck(
                name="a_first",
                ready=True,
                code="ok",
                summary="first",
            ),
        )
    )

    report = readiness.check()

    assert [check.name for check in report.checks] == ["z_last", "a_first"]
