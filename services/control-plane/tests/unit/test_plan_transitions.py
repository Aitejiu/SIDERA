from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.domain.errors import (
    InvalidTransition,
    StaleFencingToken,
)
from sidera_control_plane.domain.plan_revisions import WorkerTransitionAction
from sidera_control_plane.domain.plan_transitions import (
    PlanTransition,
    PlanTransitionStatus,
    WorkerTransition,
    WorkerTransitionStatus,
    WriterState,
)

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")
ROOT_TASK_ID = UUID("30000000-0000-0000-0000-000000000001")
TRANSITION_ID = UUID("60000000-0000-0000-0000-000000000001")
WORKER_ID = UUID("40000000-0000-0000-0000-000000000001")


def worker_transition(*, action: WorkerTransitionAction = WorkerTransitionAction.PATCH) -> WorkerTransition:
    return WorkerTransition(
        id=uuid4(),
        plan_transition_id=TRANSITION_ID,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
        worker_item_id=WORKER_ID,
        from_subtask_id=None,
        to_subtask_id=None,
        action=action,
        fallback_action=WorkerTransitionAction.CONTINUE,
        target_context_version=2,
        source_execution_id=None,
        source_session_id=None,
        target_execution_id=None,
        target_session_id=None,
        writer_state=WriterState.FENCED,
        status=WorkerTransitionStatus.PENDING,
        failure_code=None,
        lease_id=None,
        fencing_token=0,
        attempt_count=0,
        next_attempt_at=NOW,
        started_at=None,
        completed_at=None,
        version=1,
    )


def plan_transition() -> PlanTransition:
    return PlanTransition(
        id=TRANSITION_ID,
        workspace_id=WORKSPACE_ID,
        root_task_id=ROOT_TASK_ID,
        planning_request_id=uuid4(),
        proposal_revision_id=uuid4(),
        from_snapshot_id=uuid4(),
        from_generation=4,
        to_snapshot_id=uuid4(),
        to_generation=5,
        status=PlanTransitionStatus.CONVERGING,
        started_at=NOW,
        completed_at=None,
        version=1,
    )


def test_worker_transition_follows_safe_boundary_to_converged() -> None:
    transition = worker_transition()

    transition.wait_for_safe_boundary(now=NOW)
    transition.begin_apply(now=NOW)
    transition.wait_for_activation(now=NOW)
    transition.converge(now=NOW)

    assert transition.status is WorkerTransitionStatus.CONVERGED
    assert transition.writer_state is WriterState.CLOSED
    assert transition.completed_at == NOW
    assert transition.attempt_count == 1

    with pytest.raises(InvalidTransition):
        transition.retry(now=NOW)


def test_unknown_transition_is_frozen_and_cannot_auto_retry_or_continue() -> None:
    transition = worker_transition()
    transition.wait_for_safe_boundary(now=NOW)
    transition.begin_apply(now=NOW)
    transition.mark_unknown(failure_code="response_lost", now=NOW)

    assert transition.status is WorkerTransitionStatus.BLOCKED_UNKNOWN
    assert transition.writer_state is WriterState.UNKNOWN
    assert transition.can_auto_continue is False
    assert transition.can_release_fence is False

    with pytest.raises(InvalidTransition, match="unknown"):
        transition.retry(now=NOW)


def test_failed_transition_releases_fence_only_after_writer_closed() -> None:
    transition = worker_transition()
    transition.wait_for_safe_boundary(now=NOW)
    transition.begin_apply(now=NOW)
    transition.fail(failure_code="adapter_rejected", writer_closed=False, now=NOW)

    assert transition.status is WorkerTransitionStatus.BLOCKED_UNKNOWN
    assert transition.writer_state is WriterState.UNKNOWN
    assert transition.can_release_fence is False

    closed = worker_transition()
    closed.wait_for_safe_boundary(now=NOW)
    closed.begin_apply(now=NOW)
    closed.fail(failure_code="adapter_rejected", writer_closed=True, now=NOW)

    assert closed.status is WorkerTransitionStatus.FAILED
    assert closed.writer_state is WriterState.CLOSED
    assert closed.can_release_fence is True


def test_worker_transition_rejects_stale_fencing_token() -> None:
    transition = worker_transition()
    lease_id = uuid4()
    transition.claim(lease_id=lease_id, fencing_token=2, now=NOW)

    with pytest.raises(StaleFencingToken):
        transition.claim(lease_id=uuid4(), fencing_token=2, now=NOW)
    with pytest.raises(StaleFencingToken):
        transition.claim(lease_id=lease_id, fencing_token=1, now=NOW)


def test_plan_transition_status_is_derived_from_worker_outcomes() -> None:
    plan = plan_transition()
    first = worker_transition()
    second = worker_transition(action=WorkerTransitionAction.CREATE)

    plan.reconcile((first, second), now=NOW)
    assert plan.status is PlanTransitionStatus.CONVERGING
    assert plan.completed_at is None

    first.wait_for_safe_boundary(now=NOW)
    first.begin_apply(now=NOW)
    first.converge(now=NOW)
    second.wait_for_safe_boundary(now=NOW)
    second.begin_apply(now=NOW)
    second.fail(failure_code="known_failure", writer_closed=True, now=NOW)
    plan.reconcile((first, second), now=NOW)

    assert plan.status is PlanTransitionStatus.DEGRADED
    assert plan.completed_at == NOW
    assert not plan.is_confirmation_blocked((first, second))


def test_plan_transition_blocks_next_confirmation_for_unknown_or_active_writer() -> None:
    plan = plan_transition()
    unknown = worker_transition()
    unknown.wait_for_safe_boundary(now=NOW)
    unknown.begin_apply(now=NOW)
    unknown.mark_unknown(failure_code="lost", now=NOW)

    assert plan.is_confirmation_blocked((unknown,))
    assert PlanTransition.confirmation_blocked(
        has_in_flight=False,
        has_unknown=True,
        has_active_fence=False,
    )
