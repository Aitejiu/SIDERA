from __future__ import annotations

from datetime import UTC, datetime

from sidera_control_plane import collaboration_readiness as readiness_module
from sidera_control_plane.api.schemas import CollaborationReadinessResponse
from sidera_control_plane.collaboration_readiness import (
    CollaborationReadiness,
    ReadinessCheck,
    ReadinessReport,
    build_collaboration_readiness,
)
from sidera_control_plane.config import Settings


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


def test_disabled_governance_is_reported_without_an_extra_gate(
    monkeypatch,
) -> None:
    ready = lambda *_args, **_kwargs: ReadinessCheck(
        name="dependency", ready=True, code="ok", summary="ready"
    )
    monkeypatch.setattr(readiness_module, "_database_check", ready)
    monkeypatch.setattr(readiness_module, "_alembic_check", ready)
    monkeypatch.setattr(readiness_module, "_reference_runtime_check", ready)
    monkeypatch.setattr(readiness_module, "_capability_check", ready)
    monkeypatch.setattr(readiness_module, "_result_input_reader_check", ready)
    monkeypatch.setattr(readiness_module, "_result_input_runtime_check", ready)
    monkeypatch.setattr(
        readiness_module, "_result_input_worker_adapter_check", ready
    )
    monkeypatch.setattr(readiness_module, "_result_input_binding_check", ready)
    monkeypatch.setattr(readiness_module, "_result_input_artifact_store_check", ready)

    settings = Settings(
        database_url="postgresql+psycopg://test:test@localhost/test",
        result_inputs_enabled=True,
    )
    report = build_collaboration_readiness(settings, object()).check()

    assert report.ready is True
    assert report.checks[-1] == ReadinessCheck(
        name="governance",
        ready=True,
        code="governance_disabled",
        summary="human release governance is disabled; automatic collaboration remains active",
    )


def test_enabled_governance_adds_schema_and_process_gates(monkeypatch) -> None:
    ready = lambda *_args, **_kwargs: ReadinessCheck(
        name="dependency", ready=True, code="ok", summary="ready"
    )
    monkeypatch.setattr(readiness_module, "_database_check", ready)
    monkeypatch.setattr(readiness_module, "_alembic_check", ready)
    monkeypatch.setattr(readiness_module, "_reference_runtime_check", ready)
    monkeypatch.setattr(readiness_module, "_capability_check", ready)
    monkeypatch.setattr(readiness_module, "_result_input_reader_check", ready)
    monkeypatch.setattr(readiness_module, "_result_input_runtime_check", ready)
    monkeypatch.setattr(
        readiness_module, "_result_input_worker_adapter_check", ready
    )
    monkeypatch.setattr(readiness_module, "_result_input_binding_check", ready)
    monkeypatch.setattr(readiness_module, "_result_input_artifact_store_check", ready)
    monkeypatch.setattr(
        readiness_module,
        "_governance_schema_check",
        lambda *_args, **_kwargs: ReadinessCheck(
            name="governance_schema", ready=True, code="ok", summary="ready"
        ),
    )
    monkeypatch.setattr(
        readiness_module,
        "_governance_handler_check",
        lambda: ReadinessCheck(
            name="governance_handler", ready=True, code="ok", summary="ready"
        ),
    )
    monkeypatch.setattr(
        readiness_module,
        "_collaboration_worker_check",
        lambda: ReadinessCheck(
            name="collaboration_worker", ready=True, code="ok", summary="ready"
        ),
    )

    settings = Settings(
        database_url="postgresql+psycopg://test:test@localhost/test",
        collaboration_governance_enabled=True,
        result_inputs_enabled=True,
    )
    report = build_collaboration_readiness(settings, object()).check()

    assert report.ready is True
    assert [check.name for check in report.checks] == [
        "dependency",
        "dependency",
        "dependency",
        "dependency",
        "result_input_writes",
        "result_input_capabilities",
        "governance_schema",
        "governance_handler",
        "collaboration_worker",
    ]


def test_readiness_response_is_typed_and_keeps_safe_check_fields() -> None:
    report = ReadinessReport(
        component="sidera-collaboration-worker",
        as_of=datetime(2026, 9, 4, tzinfo=UTC),
        checks=(
            ReadinessCheck(
                name="governance",
                ready=False,
                code="governance_disabled",
                summary="token=secret should be redacted",
            ),
        ),
    )

    response = CollaborationReadinessResponse.from_report(report)

    assert response.model_dump(mode="json") == {
        "component": "sidera-collaboration-worker",
        "as_of": "2026-09-04T00:00:00Z",
        "status": "not_ready",
        "result_input_writes_enabled": False,
        "result_input_capabilities_ready": False,
        "checks": [
            {
                "name": "governance",
                "ready": False,
                "code": "governance_disabled",
                "summary": "[secret redacted] should be redacted",
            }
        ],
    }
