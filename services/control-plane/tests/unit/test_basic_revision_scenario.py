from __future__ import annotations

from uuid import uuid4

from tests.e2e.basic_acceptance.contracts import RevisionSnapshot
from tests.e2e.basic_acceptance.evidence import EvidenceBuilder
from tests.e2e.basic_acceptance.revision import build_revision_evidence


def _builder() -> EvidenceBuilder:
    return EvidenceBuilder(
        run_id=uuid4(),
        scenario="revision",
        evidence_kind="real_user_flow",
        code_commit="a" * 40,
        runtime_fingerprint="b" * 64,
    )


def _snapshot(**overrides: object) -> RevisionSnapshot:
    old_worker = uuid4()
    reused = uuid4()
    changed = uuid4()
    added = uuid4()
    preserved_result = uuid4()
    changed_result = uuid4()
    base: dict[str, object] = {
        "run_id": uuid4(),
        "root_task_id": uuid4(),
        "main_agent_thread_id_before": "thread-1",
        "main_agent_thread_id_after": "thread-1",
        "main_agent_tool_call_observed": True,
        "tool_call_id": "tool-call-1",
        "old_plan_id": uuid4(),
        "old_plan_hash": "c" * 64,
        "old_generation": 3,
        "new_plan_id": uuid4(),
        "new_plan_hash": "d" * 64,
        "new_generation": 4,
        "revision_id": uuid4(),
        "revision_hash": "e" * 64,
        "confirmation_command_id": uuid4(),
        "transition_id": uuid4(),
        "old_plan_still_queryable": True,
        "reused_worker_item_ids": (reused,),
        "changed_worker_item_ids": (changed,),
        "added_worker_item_ids": (added,),
        "old_worker_item_ids": (old_worker, reused, changed),
        "source_result_ids_before": (preserved_result, changed_result),
        "source_result_ids_after": (preserved_result, changed_result),
        "unimpacted_result_ids": (preserved_result,),
        "transition_worker_item_ids": (changed, added),
        "old_inputs_rejected_for_new_plan": True,
        "transition_status": "converged",
    }
    base.update(overrides)
    return RevisionSnapshot.model_validate(base)


def test_revision_reuses_unaffected_worker_and_result_identity() -> None:
    report = build_revision_evidence(_builder(), _snapshot())

    assert report.status == "PASS"
    assert "main_agent_revision_tool_observed" in report.check_codes
    assert "worker_reuse_scope_observed" in report.check_codes


def test_missing_main_agent_tool_call_is_incomplete() -> None:
    report = build_revision_evidence(
        _builder(),
        _snapshot(main_agent_tool_call_observed=False, tool_call_id=None),
    )

    assert report.status == "INCOMPLETE"
    assert report.failure_code == "main_agent_revision_tool_not_observed"


def test_revision_must_increment_generation() -> None:
    report = build_revision_evidence(
        _builder(),
        _snapshot(new_generation=3),
    )

    assert report.status == "FAIL"
    assert report.failure_code == "revision_generation_not_incremented"


def test_old_plan_must_remain_queryable() -> None:
    report = build_revision_evidence(
        _builder(),
        _snapshot(old_plan_still_queryable=False),
    )

    assert report.status == "FAIL"
    assert report.failure_code == "old_plan_not_queryable"


def test_old_input_cannot_be_reused_by_new_plan() -> None:
    report = build_revision_evidence(
        _builder(),
        _snapshot(old_inputs_rejected_for_new_plan=False),
    )

    assert report.status == "INCOMPLETE"
    assert report.failure_code == "old_input_scope_not_rejected"
