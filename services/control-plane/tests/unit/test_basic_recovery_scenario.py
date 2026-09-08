from __future__ import annotations

from uuid import uuid4

from tests.e2e.basic_acceptance.contracts import RecoverySnapshot, RuntimeLimitEvidence
from tests.e2e.basic_acceptance.evidence import EvidenceBuilder
from tests.e2e.basic_acceptance.recovery import (
    build_recovery_evidence,
    build_runtime_limit_evidence,
)


def _builder(scenario: str = "recovery") -> EvidenceBuilder:
    return EvidenceBuilder(
        run_id=uuid4(),
        scenario=scenario,
        evidence_kind="real_user_flow",
        code_commit="a" * 40,
        runtime_fingerprint="b" * 64,
    )


def _snapshot(**overrides: object) -> RecoverySnapshot:
    candidate = "c" * 64
    base: dict[str, object] = {
        "run_id": uuid4(),
        "root_task_id": uuid4(),
        "command_id": uuid4(),
        "operation_id": uuid4(),
        "operation_id_after_restart": None,
        "restarted_components": ("main-agent-gateway", "result-worker"),
        "same_run_identity": True,
        "plan_status_before": "active",
        "plan_status_after": "active",
        "candidate_hash_before": candidate,
        "candidate_hash_after": candidate,
        "pending_acceptance_before": True,
        "pending_acceptance_after": True,
        "unknown_outcome_observed": True,
        "unknown_reconciled": True,
        "duplicate_execution_count": 1,
        "provider_identity_proven": True,
        "replacement_started": False,
        "execution_count": 1,
        "execution_limit": 5,
        "elapsed_seconds": 120,
        "deadline_seconds": 600,
        "state_after": "reconciled",
    }
    base["operation_id_after_restart"] = base["operation_id"]
    base.update(overrides)
    return RecoverySnapshot.model_validate(base)


def _limits(**overrides: object) -> RuntimeLimitEvidence:
    base: dict[str, object] = {
        "run_id": uuid4(),
        "configured_worker_capacity": 2,
        "observed_peak_worker_capacity": 2,
        "configured_retry_limit": 1,
        "observed_retry_count": 1,
        "session_deadline_seconds": 600,
        "observed_elapsed_seconds": 480,
        "deadline_preserved_across_restart": True,
        "unknown_relaunch_blocked": True,
        "usage_status": "unavailable",
    }
    base.update(overrides)
    return RuntimeLimitEvidence.model_validate(base)


def test_recovery_preserves_operation_and_pending_acceptance() -> None:
    report = build_recovery_evidence(_builder(), _snapshot())

    assert report.status == "PASS"
    assert "unknown_outcome_reconciled" in report.check_codes


def test_unknown_result_cannot_start_a_replacement() -> None:
    report = build_recovery_evidence(
        _builder(),
        _snapshot(replacement_started=True, provider_identity_proven=False),
    )

    assert report.status == "FAIL"
    assert report.failure_code == "unknown_outcome_relaunched"


def test_unreconciled_unknown_result_is_incomplete() -> None:
    report = build_recovery_evidence(
        _builder(),
        _snapshot(unknown_reconciled=False),
    )

    assert report.status == "INCOMPLETE"
    assert report.failure_code == "unknown_outcome_requires_reconciliation"


def test_runtime_limits_report_unavailable_usage_without_inventing_cost() -> None:
    report = build_runtime_limit_evidence(
        _builder("runtime-limits"),
        _limits(),
    )

    assert report.status == "INCOMPLETE"
    assert report.failure_code == "usage_unavailable"
    assert "worker_capacity_respected" in report.check_codes


def test_worker_capacity_overrun_fails() -> None:
    report = build_runtime_limit_evidence(
        _builder("runtime-limits"),
        _limits(observed_peak_worker_capacity=3),
    )

    assert report.status == "FAIL"
    assert report.failure_code == "worker_capacity_exceeded"
