from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from tests.e2e.basic_acceptance.contracts import InterventionSnapshot
from tests.e2e.basic_acceptance.evidence import EvidenceBuilder
from tests.e2e.basic_acceptance.intervention import (
    build_intervention_evidence,
    run_intervention,
)


def _builder() -> EvidenceBuilder:
    return EvidenceBuilder(
        run_id=uuid4(),
        scenario="intervention",
        evidence_kind="real_user_flow",
        code_commit="a" * 40,
        runtime_fingerprint="b" * 64,
    )


def _snapshot(**overrides: object) -> InterventionSnapshot:
    worker = uuid4()
    sibling = uuid4()
    source_candidate = uuid4()
    replacement = uuid4()
    sibling_inputs = (uuid4(),)
    unaffected_results = (uuid4(),)
    base: dict[str, object] = {
        "run_id": uuid4(),
        "root_task_id": uuid4(),
        "worker_item_id": worker,
        "sibling_worker_item_ids": (sibling,),
        "plan_generation_before": 2,
        "plan_generation_after": 2,
        "input_command_id": uuid4(),
        "input_id": uuid4(),
        "input_target_worker_item_id": worker,
        "input_status": "delivered",
        "adapter_acknowledged": True,
        "observed_output_id": uuid4(),
        "observed_output_hash": "c" * 64,
        "sibling_input_ids_before": sibling_inputs,
        "sibling_input_ids_after": sibling_inputs,
        "source_candidate_id": source_candidate,
        "source_candidate_hash": "d" * 64,
        "failed_check_id": uuid4(),
        "failed_check_candidate_id": source_candidate,
        "failed_check_candidate_hash": "d" * 64,
        "rework_record_id": uuid4(),
        "rework_execution_id": uuid4(),
        "rework_session_id": uuid4(),
        "rework_worker_item_id": worker,
        "rework_generation": 2,
        "replacement_candidate_id": replacement,
        "replacement_candidate_hash": "e" * 64,
        "rework_mode": "user",
        "rework_round": None,
        "rework_round_limit": None,
        "rework_elapsed_seconds": 120,
        "rework_time_limit_seconds": None,
        "unaffected_result_ids_before": unaffected_results,
        "unaffected_result_ids_after": unaffected_results,
    }
    base.update(overrides)
    return InterventionSnapshot.model_validate(base)


def test_local_intervention_keeps_worker_and_sibling_scope() -> None:
    report = build_intervention_evidence(_builder(), _snapshot())

    assert report.status == "PASS"
    assert "plan_generation_unchanged" in report.check_codes
    assert "rework_kept_worker_item" in report.check_codes


def test_intervention_runner_rejects_snapshot_from_another_run() -> None:
    manifest = {
        "run_id": str(uuid4()),
        "code_commit": "a" * 40,
        "runtime_fingerprint": "b" * 64,
    }
    report = run_intervention(manifest, snapshot=_snapshot())

    assert report.status == "FAIL"
    assert report.failure_code == "intervention_snapshot_run_id_mismatch"


def test_persisted_input_without_adapter_ack_is_incomplete() -> None:
    report = build_intervention_evidence(
        _builder(),
        _snapshot(input_status="delivered", adapter_acknowledged=False),
    )

    assert report.status == "INCOMPLETE"
    assert report.failure_code == "adapter_ack_missing"


def test_terminal_input_delivery_failure_fails_intervention_evidence() -> None:
    for status in ("blocked", "failed", "cancelled", "superseded"):
        report = build_intervention_evidence(
            _builder(),
            _snapshot(input_status=status),
        )

        assert report.status == "FAIL"
        assert report.failure_code == "worker_input_delivery_failed"


def test_pending_or_unknown_input_delivery_is_incomplete() -> None:
    for status in ("accepted", "queued", "dispatching", "unknown"):
        report = build_intervention_evidence(
            _builder(),
            _snapshot(input_status=status, adapter_acknowledged=False),
        )

        assert report.status == "INCOMPLETE"
        assert report.failure_code == "worker_input_not_delivered"


def test_snapshot_rejects_nonexistent_applied_input_status() -> None:
    with pytest.raises(ValidationError):
        _snapshot(input_status="applied")


def test_sibling_mutation_fails_local_scope_contract() -> None:
    report = build_intervention_evidence(
        _builder(),
        _snapshot(sibling_input_ids_after=(uuid4(),)),
    )

    assert report.status == "FAIL"
    assert report.failure_code == "sibling_inputs_changed"


def test_rework_exhaustion_is_visible_and_does_not_pass() -> None:
    report = build_intervention_evidence(
        _builder(),
        _snapshot(
            rework_mode="automatic",
            rework_round=3,
            rework_round_limit=2,
            rework_time_limit_seconds=1800,
        ),
    )

    assert report.status == "INCOMPLETE"
    assert report.failure_code == "rework_budget_exhausted"


def test_user_rework_preserves_missing_round_and_does_not_apply_automatic_limits() -> None:
    report = build_intervention_evidence(
        _builder(),
        _snapshot(
            rework_mode="user",
            rework_round=None,
            rework_round_limit=None,
            rework_elapsed_seconds=3600,
            rework_time_limit_seconds=None,
        ),
    )

    assert report.status == "PASS"
    assert "user_rework_round_not_applicable" in report.check_codes


def test_automatic_rework_without_persisted_round_is_incomplete() -> None:
    report = build_intervention_evidence(
        _builder(),
        _snapshot(
            rework_mode="automatic",
            rework_round=None,
            rework_round_limit=2,
            rework_time_limit_seconds=1800,
        ),
    )

    assert report.status == "INCOMPLETE"
    assert report.failure_code == "rework_round_not_observed"


def test_user_rework_rejects_automatic_round_facts() -> None:
    report = build_intervention_evidence(
        _builder(),
        _snapshot(rework_mode="user", rework_round=1),
    )

    assert report.status == "FAIL"
    assert report.failure_code == "user_rework_round_unexpected"


def test_generation_change_cannot_be_local_intervention() -> None:
    report = build_intervention_evidence(
        _builder(),
        _snapshot(plan_generation_after=3),
    )

    assert report.status == "FAIL"
    assert report.failure_code == "local_intervention_changed_plan_generation"
