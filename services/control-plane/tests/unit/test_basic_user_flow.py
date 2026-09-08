from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from tests.e2e.basic_acceptance.contracts import ExecutionWindow, UserFlowSnapshot
from tests.e2e.basic_acceptance.evidence import EvidenceBuilder
from tests.e2e.basic_acceptance.user_flow import build_user_flow_evidence


def _builder() -> EvidenceBuilder:
    return EvidenceBuilder(
        run_id=uuid4(),
        scenario="user-flow",
        evidence_kind="real_user_flow",
        code_commit="a" * 40,
        runtime_fingerprint="b" * 64,
    )


def _snapshot(**overrides: object) -> UserFlowSnapshot:
    source_a = uuid4()
    source_b = uuid4()
    integration = uuid4()
    selection_hash = "c" * 64
    candidate_hash = "d" * 64
    accepted_tree = "e" * 40
    started = datetime(2026, 9, 8, 10, 0, tzinfo=UTC)
    base: dict[str, object] = {
        "run_id": uuid4(),
        "workspace_id": uuid4(),
        "root_task_id": uuid4(),
        "source_execution_ids": (source_a, source_b),
        "source_session_ids": (uuid4(), uuid4()),
        "integration_execution_id": integration,
        "integration_session_id": uuid4(),
        "input_selection_id": uuid4(),
        "input_selection_hash": selection_hash,
        "candidate_id": uuid4(),
        "candidate_hash": candidate_hash,
        "candidate_selection_hash": selection_hash,
        "verification_id": uuid4(),
        "verification_candidate_id": None,
        "verification_candidate_hash": None,
        "verification_status": "passed",
        "acceptance_id": uuid4(),
        "accepted_candidate_id": None,
        "accepted_candidate_hash": None,
        "accepted": True,
        "accepted_tree_hash": accepted_tree,
        "restored_tree_hash": accepted_tree,
        "restore_check_passed": True,
        "dependency_satisfied": True,
        "source_windows": (
            {"execution_id": source_a, "started_at": started, "finished_at": started + timedelta(minutes=2)},
            {"execution_id": source_b, "started_at": started + timedelta(minutes=1), "finished_at": started + timedelta(minutes=3)},
        ),
    }
    base.update(overrides)
    if base["verification_candidate_id"] is None:
        base["verification_candidate_id"] = base["candidate_id"]
    if base["verification_candidate_hash"] is None:
        base["verification_candidate_hash"] = base["candidate_hash"]
    if base["accepted_candidate_id"] is None:
        base["accepted_candidate_id"] = base["candidate_id"]
    if base["accepted_candidate_hash"] is None:
        base["accepted_candidate_hash"] = base["candidate_hash"]
    return UserFlowSnapshot.model_validate(base)


def test_complete_user_flow_requires_and_records_all_milestones() -> None:
    report = build_user_flow_evidence(_builder(), _snapshot())

    assert report.status == "PASS"
    assert "parallel_overlap_observed" in report.check_codes
    assert "accepted_tree_restored" in report.check_codes


def test_verification_without_acceptance_is_incomplete() -> None:
    report = build_user_flow_evidence(
        _builder(), _snapshot(accepted=False, acceptance_id=None)
    )

    assert report.status == "INCOMPLETE"
    assert report.failure_code == "acceptance_not_observed"


def test_candidate_input_hash_mismatch_fails() -> None:
    report = build_user_flow_evidence(
        _builder(), _snapshot(candidate_selection_hash="f" * 64)
    )

    assert report.status == "FAIL"
    assert report.failure_code == "candidate_input_selection_mismatch"


def test_missing_dependency_fact_is_incomplete() -> None:
    report = build_user_flow_evidence(
        _builder(), _snapshot(dependency_satisfied=False)
    )

    assert report.status == "INCOMPLETE"
    assert report.failure_code == "dependency_fact_missing"


def test_no_observed_parallel_overlap_is_incomplete() -> None:
    started = datetime(2026, 9, 8, 10, 0, tzinfo=UTC)
    snapshot = _snapshot()
    report = build_user_flow_evidence(
        _builder(),
        snapshot.model_copy(
            update={
                "source_windows": (
                    ExecutionWindow(
                        execution_id=snapshot.source_execution_ids[0],
                        started_at=started,
                        finished_at=started + timedelta(minutes=1),
                    ),
                    ExecutionWindow(
                        execution_id=snapshot.source_execution_ids[1],
                        started_at=started + timedelta(minutes=1),
                        finished_at=started + timedelta(minutes=2),
                    ),
                )
            }
        ),
    )

    assert report.status == "INCOMPLETE"
    assert report.failure_code == "parallel_overlap_not_observed"
