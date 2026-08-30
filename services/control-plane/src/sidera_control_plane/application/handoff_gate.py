from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.domain.errors import DomainError
from sidera_control_plane.domain.handoffs import (
    HandoffPhase,
    HandoffTransitionStatus,
    HandoffWriterState,
)


class HandoffGateOperation(StrEnum):
    USER_INPUT = "user_input"
    ORDINARY_STOP = "ordinary_stop"
    CONTINUATION = "continuation"
    ROOT_PLAN_CONFIRM = "root_plan_confirm"
    NORMAL_SCHEDULE = "normal_schedule"
    COORDINATOR_CHECKPOINT_SOURCE = "coordinator_checkpoint_source"
    COORDINATOR_STOP_SOURCE = "coordinator_stop_source"
    COORDINATOR_FREEZE_WORKSPACE = "coordinator_freeze_workspace"
    COORDINATOR_CREATE_TARGET = "coordinator_create_target"
    COORDINATOR_ASSEMBLE_CONTEXT = "coordinator_assemble_context"
    COORDINATOR_TRANSFER_WORKSPACE = "coordinator_transfer_workspace"
    COORDINATOR_ASSIGN_TARGET = "coordinator_assign_target"
    COORDINATOR_START_TARGET = "coordinator_start_target"
    COORDINATOR_ENABLE_TARGET_WRITER = "coordinator_enable_target_writer"
    COORDINATOR_TRANSFER_INPUTS = "coordinator_transfer_inputs"
    COORDINATOR_ACTIVATE_TARGET = "coordinator_activate_target"


@dataclass(frozen=True, slots=True)
class HandoffGateResult:
    allowed: bool
    blocking_code: str | None
    transition_id: UUID | None
    status: HandoffTransitionStatus | None
    phase: HandoffPhase | None
    writer_state: HandoffWriterState | None
    target_execution_id: UUID | None


class HandoffGateBlocked(DomainError):
    """A command attempted an operation fenced by an active handoff."""

    def __init__(self, result: HandoffGateResult) -> None:
        code = result.blocking_code or "handoff_gate_unavailable"
        super().__init__(
            f"handoff gate blocked operation with {code}",
            details={
                "blocking_code": code,
                "transition_id": (
                    str(result.transition_id)
                    if result.transition_id is not None
                    else None
                ),
                "status": result.status.value if result.status is not None else None,
                "phase": result.phase.value if result.phase is not None else None,
                "writer_state": (
                    result.writer_state.value
                    if result.writer_state is not None
                    else None
                ),
                "target_execution_id": (
                    str(result.target_execution_id)
                    if result.target_execution_id is not None
                    else None
                ),
            },
        )
        self.code = code
        self.result = result


class HandoffInProgress(HandoffGateBlocked):
    """A root-plan operation encountered an active WorkerItem handoff."""

    def __init__(self, transition: Any) -> None:
        super().__init__(_result_for_transition(transition, "handoff_in_progress"))


class HandoffOperationGate:
    """Derive operation permission from the authoritative active Transition."""

    def __init__(self, uow: ControlPlaneUnitOfWork) -> None:
        self._uow = uow

    def evaluate(
        self,
        *,
        worker_item_id: UUID,
        workspace_id: UUID,
        operation: HandoffGateOperation,
        transition_id: UUID | None = None,
        target_execution_id: UUID | None = None,
    ) -> HandoffGateResult:
        try:
            operation_value = HandoffGateOperation(operation)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"unsupported handoff gate operation: {operation!r}") from exc

        repository = getattr(self._uow, "handoff_transitions", None)
        if repository is None:
            return _allowed_result()
        try:
            transition = repository.get_active_for_worker_item(
                worker_item_id,
                workspace_id,
                for_update=True,
            )
        except Exception:  # noqa: BLE001
            return _blocked_result("handoff_gate_unavailable")
        if transition is None:
            return _allowed_result()

        result = _result_for_transition(transition, "handoff_in_progress")
        if operation_value in _NORMAL_OPERATIONS:
            return result

        if operation_value in _COORDINATOR_PHASES:
            expected_phase = _COORDINATOR_PHASES[operation_value]
            if result.phase is not expected_phase:
                return _result_for_transition(transition, "handoff_phase_mismatch")
            if transition_id is None or transition_id != result.transition_id:
                return _result_for_transition(
                    transition, "handoff_transition_mismatch"
                )
            if operation_value in _TARGET_ID_OPERATIONS and (
                target_execution_id is None
                or result.target_execution_id is None
                or target_execution_id != result.target_execution_id
            ):
                return _result_for_transition(transition, "handoff_target_mismatch")
            if result.status not in {
                HandoffTransitionStatus.PENDING,
                HandoffTransitionStatus.RUNNING,
            }:
                return _result_for_transition(
                    transition, "handoff_transition_blocked"
                )
            return _result_for_transition(transition, None)

        return _result_for_transition(transition, "handoff_in_progress")


def require_handoff_operation(
    uow: ControlPlaneUnitOfWork,
    *,
    worker_item_id: UUID,
    workspace_id: UUID,
    operation: HandoffGateOperation,
    transition_id: UUID | None = None,
    target_execution_id: UUID | None = None,
) -> HandoffGateResult:
    result = HandoffOperationGate(uow).evaluate(
        worker_item_id=worker_item_id,
        workspace_id=workspace_id,
        operation=operation,
        transition_id=transition_id,
        target_execution_id=target_execution_id,
    )
    if not result.allowed:
        raise HandoffGateBlocked(result)
    return result


_NORMAL_OPERATIONS = frozenset(
    {
        HandoffGateOperation.USER_INPUT,
        HandoffGateOperation.ORDINARY_STOP,
        HandoffGateOperation.CONTINUATION,
        HandoffGateOperation.ROOT_PLAN_CONFIRM,
        HandoffGateOperation.NORMAL_SCHEDULE,
    }
)

_COORDINATOR_PHASES = {
    HandoffGateOperation.COORDINATOR_CHECKPOINT_SOURCE: HandoffPhase.CHECKPOINT_SOURCE,
    HandoffGateOperation.COORDINATOR_STOP_SOURCE: HandoffPhase.STOP_SOURCE,
    HandoffGateOperation.COORDINATOR_FREEZE_WORKSPACE: HandoffPhase.FREEZE_SOURCE_WORKSPACE,
    HandoffGateOperation.COORDINATOR_CREATE_TARGET: HandoffPhase.CREATE_TARGET_EXECUTION,
    HandoffGateOperation.COORDINATOR_ASSEMBLE_CONTEXT: HandoffPhase.ASSEMBLE_BUNDLE,
    HandoffGateOperation.COORDINATOR_TRANSFER_WORKSPACE: HandoffPhase.TRANSFER_WORKSPACE,
    HandoffGateOperation.COORDINATOR_ASSIGN_TARGET: HandoffPhase.START_TARGET_SESSION,
    HandoffGateOperation.COORDINATOR_START_TARGET: HandoffPhase.START_TARGET_SESSION,
    HandoffGateOperation.COORDINATOR_ENABLE_TARGET_WRITER: HandoffPhase.ACKNOWLEDGE_CONTEXT,
    HandoffGateOperation.COORDINATOR_TRANSFER_INPUTS: HandoffPhase.TRANSFER_SAFE_INPUTS,
    HandoffGateOperation.COORDINATOR_ACTIVATE_TARGET: HandoffPhase.ACTIVATE_TARGET,
}

_TARGET_ID_OPERATIONS = frozenset(
    {
        HandoffGateOperation.COORDINATOR_ASSIGN_TARGET,
        HandoffGateOperation.COORDINATOR_START_TARGET,
        HandoffGateOperation.COORDINATOR_ENABLE_TARGET_WRITER,
        HandoffGateOperation.COORDINATOR_TRANSFER_INPUTS,
        HandoffGateOperation.COORDINATOR_ACTIVATE_TARGET,
    }
)


def _allowed_result() -> HandoffGateResult:
    return HandoffGateResult(
        allowed=True,
        blocking_code=None,
        transition_id=None,
        status=None,
        phase=None,
        writer_state=None,
        target_execution_id=None,
    )


def _blocked_result(code: str) -> HandoffGateResult:
    return HandoffGateResult(
        allowed=False,
        blocking_code=code,
        transition_id=None,
        status=None,
        phase=None,
        writer_state=None,
        target_execution_id=None,
    )


def _result_for_transition(transition: Any, code: str | None) -> HandoffGateResult:
    status = _enum_value(transition.status, HandoffTransitionStatus)
    phase = _enum_value(transition.phase, HandoffPhase)
    writer_state = _enum_value(transition.writer_state, HandoffWriterState)
    return HandoffGateResult(
        allowed=code is None,
        blocking_code=code,
        transition_id=_uuid_or_none(getattr(transition, "id", None)),
        status=status,
        phase=phase,
        writer_state=writer_state,
        target_execution_id=_uuid_or_none(
            getattr(transition, "target_execution_id", None)
        ),
    )


def _enum_value(value: object, enum_type: type[Any]) -> Any:
    if isinstance(value, enum_type):
        return value
    return enum_type(value)


def _uuid_or_none(value: object) -> UUID | None:
    return value if isinstance(value, UUID) else None
