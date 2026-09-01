from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.domain.errors import InvalidTransition, ValidationError
from sidera_control_plane.domain.execution_requests import (
    DependencyEnforcement,
    ExecutionRequest,
    ExecutionRequestDependency,
    ExecutionRequestStatus,
)

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")
REQUEST_ID = UUID("21000000-0000-0000-0000-000000000001")
PROPOSAL_ID = UUID("22000000-0000-0000-0000-000000000001")
PLAN_ID = UUID("23000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("24000000-0000-0000-0000-000000000001")
SUBTASK_ID = UUID("25000000-0000-0000-0000-000000000001")
WORKER_ITEM_ID = UUID("26000000-0000-0000-0000-000000000001")


def make_request(
    *,
    request_id: UUID = REQUEST_ID,
    source_execution_id: UUID | None = None,
    source_session_id: UUID | None = None,
    handoff_transition_id: UUID | None = None,
) -> ExecutionRequest:
    return ExecutionRequest.create(
        request_id=request_id,
        workspace_id=WORKSPACE_ID,
        planning_request_id=UUID("27000000-0000-0000-0000-000000000001"),
        proposal_revision_id=PROPOSAL_ID,
        plan_snapshot_id=PLAN_ID,
        plan_generation=1,
        root_task_id=ROOT_TASK_ID,
        subtask_id=SUBTASK_ID,
        worker_item_id=WORKER_ITEM_ID,
        worker_item_key="backend",
        intent_key="backend-implementation",
        purpose="Implement the backend",
        capability_requirements=frozenset({"python"}),
        adapter_preferences=("opencode",),
        adapter_forbidden=("unsafe",),
        budget_class="standard",
        not_before=NOW,
        max_attempts=2,
        expected_artifact_refs=("artifact_type:diff",),
        expected_evidence_refs=("acceptance:tests-pass",),
        now=NOW,
        source_execution_id=source_execution_id,
        source_session_id=source_session_id,
        handoff_transition_id=handoff_transition_id,
    )


def make_dependency(
    *,
    downstream: UUID = REQUEST_ID,
    upstream: UUID | None = None,
    enforcement: DependencyEnforcement = DependencyEnforcement.HARD,
    artifacts: tuple[str, ...] = (),
    evidence: tuple[str, ...] = (),
) -> ExecutionRequestDependency:
    return ExecutionRequestDependency(
        downstream_request_id=downstream,
        upstream_request_id=upstream or UUID("28000000-0000-0000-0000-000000000001"),
        enforcement=enforcement,
        required_artifact_refs=artifacts,
        required_evidence_refs=evidence,
    )


def test_execution_request_starts_blocked_and_materializes_once() -> None:
    request = make_request()

    assert request.status is ExecutionRequestStatus.BLOCKED
    assert request.plan_snapshot_id == PLAN_ID
    assert request.plan_generation == 1
    assert request.adapter_forbidden == frozenset({"unsafe"})
    request.mark_ready(now=NOW)
    assert request.status is ExecutionRequestStatus.READY

    lineage_id = uuid4()
    execution_id = uuid4()
    request.materialize(
        execution_lineage_id=lineage_id,
        first_execution_id=execution_id,
        now=NOW,
    )
    assert request.status is ExecutionRequestStatus.MATERIALIZED
    assert request.execution_lineage_id == lineage_id
    assert request.first_execution_id == execution_id

    with pytest.raises(InvalidTransition):
        request.materialize(
            execution_lineage_id=uuid4(),
            first_execution_id=uuid4(),
            now=NOW,
        )


def test_continuation_request_keeps_exact_source_lineage() -> None:
    source_execution_id = uuid4()
    source_session_id = uuid4()

    request = make_request(
        request_id=uuid4(),
        source_execution_id=source_execution_id,
        source_session_id=source_session_id,
    )

    assert request.source_execution_id == source_execution_id
    assert request.source_session_id == source_session_id


def test_cancelled_and_obsolete_requests_cannot_reopen() -> None:
    cancelled = make_request()
    cancelled.cancel(now=NOW, reason="user stopped the plan")
    assert cancelled.status is ExecutionRequestStatus.CANCELLED
    with pytest.raises(InvalidTransition):
        cancelled.mark_ready(now=NOW)

    obsolete = make_request(request_id=uuid4())
    obsolete.obsolete(now=NOW, reason="plan generation replaced")
    assert obsolete.status is ExecutionRequestStatus.OBSOLETE
    with pytest.raises(InvalidTransition):
        obsolete.mark_ready(now=NOW)


def test_materialized_handoff_target_retirement_releases_attempt_slot() -> None:
    source_execution_id = uuid4()
    source_session_id = uuid4()
    transition_id = uuid4()
    target = make_request(
        request_id=uuid4(),
        source_execution_id=source_execution_id,
        source_session_id=source_session_id,
        handoff_transition_id=transition_id,
    )
    target.mark_ready(now=NOW)
    target_lineage_id = uuid4()
    first_execution_id = uuid4()
    target.materialize(
        execution_lineage_id=target_lineage_id,
        first_execution_id=first_execution_id,
        now=NOW,
    )

    target.retire_handoff_target(now=NOW, reason="target_start_unknown")

    assert target.status is ExecutionRequestStatus.OBSOLETE
    assert target.handoff_transition_id == transition_id
    assert target.execution_lineage_id == target_lineage_id
    assert target.first_execution_id == first_execution_id
    assert target.gate_details == {
        "reason": "target_start_unknown",
        "retired_execution_lineage_id": str(target_lineage_id),
        "retired_first_execution_id": str(first_execution_id),
        "retired_handoff_transition_id": str(transition_id),
    }


def test_request_rejects_unscoped_required_refs_and_invalid_snapshot() -> None:
    with pytest.raises(ValidationError, match="artifact_type"):
        ExecutionRequest.create(
            request_id=uuid4(),
            workspace_id=WORKSPACE_ID,
            planning_request_id=uuid4(),
            proposal_revision_id=PROPOSAL_ID,
            plan_snapshot_id=PLAN_ID,
            plan_generation=1,
            root_task_id=ROOT_TASK_ID,
            subtask_id=SUBTASK_ID,
            worker_item_id=WORKER_ITEM_ID,
            worker_item_key="backend",
            intent_key="backend-implementation",
            purpose="Implement the backend",
            capability_requirements=frozenset({"python"}),
            adapter_preferences=(),
            budget_class="standard",
            not_before=NOW,
            max_attempts=1,
            expected_artifact_refs=("free-form output",),
            expected_evidence_refs=(),
            now=NOW,
        )


def test_request_rejects_an_adapter_preference_that_is_forbidden() -> None:
    with pytest.raises(ValidationError, match="forbidden"):
        ExecutionRequest.create(
            request_id=uuid4(),
            workspace_id=WORKSPACE_ID,
            planning_request_id=uuid4(),
            proposal_revision_id=PROPOSAL_ID,
            plan_snapshot_id=PLAN_ID,
            plan_generation=1,
            root_task_id=ROOT_TASK_ID,
            subtask_id=SUBTASK_ID,
            worker_item_id=WORKER_ITEM_ID,
            worker_item_key="backend",
            intent_key="backend-implementation",
            purpose="Implement the backend",
            capability_requirements=frozenset({"python"}),
            adapter_preferences=("opencode",),
            adapter_forbidden=("opencode",),
            budget_class="standard",
            not_before=NOW,
            max_attempts=1,
            expected_artifact_refs=(),
            expected_evidence_refs=(),
            now=NOW,
        )


def test_dependency_uses_stable_request_ids_and_never_allows_self_edge() -> None:
    dependency = make_dependency()
    assert dependency.is_blocking
    assert dependency.upstream_request_id != dependency.downstream_request_id

    with pytest.raises(ValidationError, match="self"):
        make_dependency(
            upstream=REQUEST_ID,
        )

    informational = make_dependency(
        enforcement=DependencyEnforcement.INFORMATIONAL,
    )
    assert not informational.is_blocking
