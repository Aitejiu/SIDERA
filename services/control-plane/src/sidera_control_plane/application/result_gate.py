from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.domain.result_operations import (
    ResultError,
    ResultOperationStatus,
)


class ResultGateAction(StrEnum):
    INSPECT = "inspect"
    RECOVER = "recover"
    RECORD_RECEIPT = "record_receipt"
    ACTIVATE_REVISION = "activate_revision"
    ACCEPT = "accept"
    REWORK = "rework"
    PUBLISH_CANDIDATE = "publish_candidate"
    REINTEGRATE = "reintegrate"
    CREATE_EXECUTION = "create_execution"
    CANCEL_TASK = "cancel_task"
    HANDOFF = "handoff"
    FINALIZE = "finalize"


_SAFE_ACTIONS = frozenset(
    {
        ResultGateAction.INSPECT,
        ResultGateAction.RECOVER,
        ResultGateAction.RECORD_RECEIPT,
    }
)
_IN_FLIGHT_STATUSES = frozenset(
    {
        ResultOperationStatus.PENDING,
        ResultOperationStatus.RUNNING,
        ResultOperationStatus.UNKNOWN,
    }
)


@dataclass(frozen=True, slots=True)
class ResultGateDecision:
    action: ResultGateAction
    allowed: bool
    reason: str
    operation_id: UUID | None = None
    operation_status: ResultOperationStatus | None = None


def require_result_gate(
    uow: ControlPlaneUnitOfWork,
    workspace_id: UUID,
    root_task_id: UUID,
    action: ResultGateAction | str,
) -> ResultGateDecision:
    """Fence root-level mutations while an external result operation is live.

    The plan head is locked before the result head.  An absent result head is a
    legacy root and remains compatible with pre-R6 result behavior.
    """

    _require_uuid(workspace_id, "workspace_id")
    _require_uuid(root_task_id, "root_task_id")
    try:
        action_value = ResultGateAction(action)
    except (TypeError, ValueError) as exc:
        raise ResultError(
            "result_gate_action_invalid", {"action": str(action)}
        ) from exc

    uow.plans.get_head(root_task_id, workspace_id, for_update=True)
    head = uow.results.get_head(workspace_id, root_task_id, for_update=True)
    if head is None or head.active_operation_id is None:
        return ResultGateDecision(action_value, True, "allowed")

    operation = uow.results.get_operation(
        head.active_operation_id, workspace_id, for_update=True
    )
    if operation is None:
        raise ResultError(
            "result_operation_in_progress",
            {
                "operation_id": str(head.active_operation_id),
                "reason": "active_result_operation_missing",
            },
        )
    status = getattr(operation, "status", None)
    if not isinstance(status, ResultOperationStatus):
        raise ResultError(
            "result_operation_in_progress",
            {
                "operation_id": str(head.active_operation_id),
                "reason": "active_result_operation_invalid",
            },
        )
    if status in _IN_FLIGHT_STATUSES and action_value not in _SAFE_ACTIONS:
        operation_id = _operation_id(operation, head.active_operation_id)
        raise ResultError(
            "result_operation_in_progress",
            {
                "operation_id": str(operation_id),
                "status": status.value,
                "action": action_value.value,
            },
        )
    operation_id = _operation_id(operation, head.active_operation_id)
    return ResultGateDecision(
        action=action_value,
        allowed=True,
        reason=(
            "inspection_allowed"
            if action_value in _SAFE_ACTIONS
            else "known_result_operation_terminal"
        ),
        operation_id=operation_id,
        operation_status=status,
    )


def _require_uuid(value: object, label: str) -> None:
    if not isinstance(value, UUID):
        raise TypeError(f"{label} must be a UUID")


def _operation_id(operation: object, fallback: UUID) -> UUID:
    value = getattr(operation, "id", None)
    if not isinstance(value, UUID):
        value = getattr(operation, "operation_id", None)
    return value if isinstance(value, UUID) else fallback


__all__ = ["ResultGateAction", "ResultGateDecision", "require_result_gate"]
