from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.application.plan_transition_gate import (
    PlanTransitionGate,
    PlanTransitionGateDecision,
    PlanTransitionGateOperation,
)
from sidera_control_plane.domain.plan_transitions import (
    WorkerTransitionStatus,
)

WORKSPACE_ID = UUID("81000000-0000-0000-0000-000000000001")
WORKER_ITEM_ID = UUID("82000000-0000-0000-0000-000000000001")


@dataclass
class FakeWorkerItems:
    placement: object | None

    def get_current_placement(self, worker_item_id, workspace_id, **_kwargs):
        assert worker_item_id == WORKER_ITEM_ID
        assert workspace_id == WORKSPACE_ID
        return self.placement


@dataclass
class FakeTransitions:
    active: object | None

    def get_active_for_worker_item(self, worker_item_id, workspace_id, **_kwargs):
        assert worker_item_id == WORKER_ITEM_ID
        assert workspace_id == WORKSPACE_ID
        return self.active


class FakeUnitOfWork:
    def __init__(self, *, generation: int | None, active: object | None = None):
        self.worker_items = FakeWorkerItems(
            None
            if generation is None
            else SimpleNamespace(plan_generation=generation)
        )
        self.worker_transitions = FakeTransitions(active)


def _transition(status: WorkerTransitionStatus) -> object:
    return SimpleNamespace(id=uuid4(), status=status)


def _gate(uow: FakeUnitOfWork) -> PlanTransitionGate:
    return PlanTransitionGate(uow)  # type: ignore[arg-type]


def test_current_generation_without_transition_is_allowed() -> None:
    result = _gate(FakeUnitOfWork(generation=3)).evaluate(
        worker_item_id=WORKER_ITEM_ID,
        workspace_id=WORKSPACE_ID,
        plan_generation=3,
        operation=PlanTransitionGateOperation.SCHEDULE,
    )

    assert result.decision is PlanTransitionGateDecision.ALLOW
    assert result.reason == "allowed"
    assert result.current_generation == 3
    assert result.transition_id is None


def test_old_generation_is_obsolete_before_transition_is_considered() -> None:
    result = _gate(
        FakeUnitOfWork(
            generation=3,
            active=_transition(WorkerTransitionStatus.BLOCKED_UNKNOWN),
        )
    ).evaluate(
        worker_item_id=WORKER_ITEM_ID,
        workspace_id=WORKSPACE_ID,
        plan_generation=2,
        operation=PlanTransitionGateOperation.ACTIVATE,
    )

    assert result.decision is PlanTransitionGateDecision.OBSOLETE
    assert result.reason == "plan_generation_stale"
    assert result.current_generation == 3


def test_future_generation_is_blocked_with_a_stable_reason() -> None:
    result = _gate(FakeUnitOfWork(generation=3)).evaluate(
        worker_item_id=WORKER_ITEM_ID,
        workspace_id=WORKSPACE_ID,
        plan_generation=4,
        operation=PlanTransitionGateOperation.MUTATE,
    )

    assert result.decision is PlanTransitionGateDecision.BLOCK
    assert result.reason == "plan_generation_ahead"


@pytest.mark.parametrize(
    ("status", "reason"),
    (
        (WorkerTransitionStatus.PENDING, "worker_transition_in_flight"),
        (WorkerTransitionStatus.WAITING_SAFE_BOUNDARY, "worker_transition_in_flight"),
        (WorkerTransitionStatus.APPLYING, "worker_transition_in_flight"),
        (WorkerTransitionStatus.WAITING_ACTIVATION, "worker_transition_in_flight"),
        (WorkerTransitionStatus.BLOCKED_UNKNOWN, "worker_transition_unknown"),
    ),
)
def test_active_transition_blocks_runtime_operation(
    status: WorkerTransitionStatus, reason: str
) -> None:
    transition = _transition(status)
    result = _gate(FakeUnitOfWork(generation=3, active=transition)).evaluate(
        worker_item_id=WORKER_ITEM_ID,
        workspace_id=WORKSPACE_ID,
        plan_generation=3,
        operation=PlanTransitionGateOperation.INPUT,
    )

    assert result.decision is PlanTransitionGateDecision.BLOCK
    assert result.reason == reason
    assert result.transition_id == transition.id
    assert result.transition_status == status.value


def test_waiting_activation_can_be_allowed_for_the_exact_transition_target() -> None:
    transition = _transition(WorkerTransitionStatus.WAITING_ACTIVATION)
    result = _gate(FakeUnitOfWork(generation=3, active=transition)).evaluate(
        worker_item_id=WORKER_ITEM_ID,
        workspace_id=WORKSPACE_ID,
        plan_generation=3,
        operation=PlanTransitionGateOperation.ACTIVATE,
        allow_transition_id=transition.id,
    )

    assert result.decision is PlanTransitionGateDecision.ALLOW
    assert result.reason == "allowed"
    assert result.transition_id == transition.id


def test_waiting_activation_allows_scheduling_only_for_bound_target_execution() -> None:
    transition = _transition(WorkerTransitionStatus.WAITING_ACTIVATION)
    target_execution_id = uuid4()
    transition.target_execution_id = target_execution_id  # type: ignore[attr-defined]
    result = _gate(FakeUnitOfWork(generation=3, active=transition)).evaluate(
        worker_item_id=WORKER_ITEM_ID,
        workspace_id=WORKSPACE_ID,
        plan_generation=3,
        operation=PlanTransitionGateOperation.SCHEDULE,
        allow_transition_id=transition.id,
        target_id=target_execution_id,
    )

    assert result.decision is PlanTransitionGateDecision.ALLOW


def test_missing_current_placement_fails_closed() -> None:
    result = _gate(FakeUnitOfWork(generation=None)).evaluate(
        worker_item_id=WORKER_ITEM_ID,
        workspace_id=WORKSPACE_ID,
        plan_generation=1,
        operation=PlanTransitionGateOperation.SCHEDULE,
    )

    assert result.decision is PlanTransitionGateDecision.BLOCK
    assert result.reason == "worker_item_placement_missing"


def test_legacy_execution_without_plan_placement_remains_allowed() -> None:
    result = _gate(FakeUnitOfWork(generation=None)).evaluate(
        worker_item_id=WORKER_ITEM_ID,
        workspace_id=WORKSPACE_ID,
        plan_generation=None,
        operation=PlanTransitionGateOperation.SCHEDULE,
    )

    assert result.decision is PlanTransitionGateDecision.ALLOW
    assert result.reason == "allowed"
