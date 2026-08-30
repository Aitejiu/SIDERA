from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from sidera_control_plane.application.handoff_gate import (
    HandoffGateOperation,
    HandoffOperationGate,
)
from sidera_control_plane.domain.handoffs import (
    HandoffPhase,
    HandoffTransitionStatus,
    HandoffWriterState,
)

WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000001")
WORKER_ITEM_ID = UUID("20000000-0000-0000-0000-000000000001")
TRANSITION_ID = UUID("30000000-0000-0000-0000-000000000001")
TARGET_EXECUTION_ID = UUID("40000000-0000-0000-0000-000000000001")


@dataclass
class MemoryTransitions:
    transition: object | None = None
    failure: Exception | None = None

    def get_active_for_worker_item(
        self,
        _worker_item_id: UUID,
        _workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> object | None:
        del for_update
        if self.failure is not None:
            raise self.failure
        return self.transition


def _uow(transitions: MemoryTransitions) -> SimpleNamespace:
    return SimpleNamespace(handoff_transitions=transitions)


def _transition(
    *,
    status: HandoffTransitionStatus = HandoffTransitionStatus.RUNNING,
    phase: HandoffPhase = HandoffPhase.CHECKPOINT_SOURCE,
    writer_state: HandoffWriterState = HandoffWriterState.SOURCE_ACTIVE,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=TRANSITION_ID,
        status=status,
        phase=phase,
        writer_state=writer_state,
        target_execution_id=TARGET_EXECUTION_ID,
    )


def test_handoff_gate_operation_values_are_exact() -> None:
    assert tuple(item.value for item in HandoffGateOperation) == (
        "user_input",
        "ordinary_stop",
        "continuation",
        "root_plan_confirm",
        "normal_schedule",
        "coordinator_checkpoint_source",
        "coordinator_stop_source",
        "coordinator_freeze_workspace",
        "coordinator_create_target",
        "coordinator_assemble_context",
        "coordinator_transfer_workspace",
        "coordinator_assign_target",
        "coordinator_start_target",
        "coordinator_enable_target_writer",
        "coordinator_transfer_inputs",
        "coordinator_activate_target",
    )


def test_no_active_handoff_allows_the_operation_without_facts() -> None:
    result = HandoffOperationGate(_uow(MemoryTransitions())).evaluate(
        worker_item_id=WORKER_ITEM_ID,
        workspace_id=WORKSPACE_ID,
        operation=HandoffGateOperation.USER_INPUT,
    )

    assert result.allowed is True
    assert result.blocking_code is None
    assert result.transition_id is None
    assert result.status is None
    assert result.phase is None
    assert result.writer_state is None
    assert result.target_execution_id is None


@pytest.mark.parametrize(
    "operation",
    (
        HandoffGateOperation.USER_INPUT,
        HandoffGateOperation.ORDINARY_STOP,
        HandoffGateOperation.CONTINUATION,
        HandoffGateOperation.ROOT_PLAN_CONFIRM,
        HandoffGateOperation.NORMAL_SCHEDULE,
    ),
)
def test_active_handoff_blocks_normal_operations_with_exact_facts(
    operation: HandoffGateOperation,
) -> None:
    transition = _transition()
    result = HandoffOperationGate(
        _uow(MemoryTransitions(transition=transition))
    ).evaluate(
        worker_item_id=WORKER_ITEM_ID,
        workspace_id=WORKSPACE_ID,
        operation=operation,
    )

    assert result.allowed is False
    assert result.blocking_code == "handoff_in_progress"
    assert result.transition_id == TRANSITION_ID
    assert result.status is HandoffTransitionStatus.RUNNING
    assert result.phase is HandoffPhase.CHECKPOINT_SOURCE
    assert result.writer_state is HandoffWriterState.SOURCE_ACTIVE
    assert result.target_execution_id == TARGET_EXECUTION_ID


def test_matching_coordinator_operation_and_target_is_allowed() -> None:
    transition = _transition(
        phase=HandoffPhase.START_TARGET_SESSION,
        writer_state=HandoffWriterState.TARGET_STARTING,
    )
    result = HandoffOperationGate(
        _uow(MemoryTransitions(transition=transition))
    ).evaluate(
        worker_item_id=WORKER_ITEM_ID,
        workspace_id=WORKSPACE_ID,
        operation=HandoffGateOperation.COORDINATOR_START_TARGET,
        transition_id=TRANSITION_ID,
        target_execution_id=TARGET_EXECUTION_ID,
    )

    assert result.allowed is True
    assert result.transition_id == TRANSITION_ID


def test_coordinator_identity_and_phase_are_required() -> None:
    transition = _transition(
        phase=HandoffPhase.START_TARGET_SESSION,
        writer_state=HandoffWriterState.TARGET_STARTING,
    )
    gate = HandoffOperationGate(_uow(MemoryTransitions(transition=transition)))

    wrong_phase = gate.evaluate(
        worker_item_id=WORKER_ITEM_ID,
        workspace_id=WORKSPACE_ID,
        operation=HandoffGateOperation.COORDINATOR_TRANSFER_INPUTS,
        transition_id=TRANSITION_ID,
        target_execution_id=TARGET_EXECUTION_ID,
    )
    wrong_identity = gate.evaluate(
        worker_item_id=WORKER_ITEM_ID,
        workspace_id=WORKSPACE_ID,
        operation=HandoffGateOperation.COORDINATOR_START_TARGET,
        transition_id=uuid4(),
        target_execution_id=TARGET_EXECUTION_ID,
    )

    assert wrong_phase.allowed is False
    assert wrong_phase.blocking_code == "handoff_phase_mismatch"
    assert wrong_identity.allowed is False
    assert wrong_identity.blocking_code == "handoff_transition_mismatch"


def test_failed_handoff_still_blocks_normal_operations() -> None:
    transition = _transition(status=HandoffTransitionStatus.FAILED)
    result = HandoffOperationGate(
        _uow(MemoryTransitions(transition=transition))
    ).evaluate(
        worker_item_id=WORKER_ITEM_ID,
        workspace_id=WORKSPACE_ID,
        operation=HandoffGateOperation.USER_INPUT,
    )

    assert result.allowed is False
    assert result.blocking_code == "handoff_in_progress"


def test_handoff_gate_fails_closed_when_the_authoritative_read_fails() -> None:
    result = HandoffOperationGate(
        _uow(MemoryTransitions(failure=RuntimeError("database unavailable")))
    ).evaluate(
        worker_item_id=WORKER_ITEM_ID,
        workspace_id=WORKSPACE_ID,
        operation=HandoffGateOperation.USER_INPUT,
    )

    assert result.allowed is False
    assert result.blocking_code == "handoff_gate_unavailable"
    assert result.transition_id is None
