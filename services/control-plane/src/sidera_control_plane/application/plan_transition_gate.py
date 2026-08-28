from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID

from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.domain.plan_transitions import (
    WORKER_TRANSITION_IN_FLIGHT,
    WorkerTransitionStatus,
)


class PlanTransitionGateOperation(StrEnum):
    SCHEDULE = "schedule"
    ACTIVATE = "activate"
    INPUT = "input"
    MUTATE = "mutate"


class PlanTransitionGateDecision(StrEnum):
    ALLOW = "allow"
    BLOCK = "block"
    OBSOLETE = "obsolete"


@dataclass(frozen=True, slots=True)
class PlanTransitionGateResult:
    decision: PlanTransitionGateDecision
    reason: str
    worker_item_id: UUID
    workspace_id: UUID
    operation: PlanTransitionGateOperation
    requested_generation: int | None
    current_generation: int | None
    transition_id: UUID | None = None
    transition_status: str | None = None
    details: Mapping[str, object] = MappingProxyType({})

    @property
    def allowed(self) -> bool:
        return self.decision is PlanTransitionGateDecision.ALLOW

    @property
    def blocked(self) -> bool:
        return self.decision is PlanTransitionGateDecision.BLOCK

    @property
    def obsolete(self) -> bool:
        return self.decision is PlanTransitionGateDecision.OBSOLETE


class PlanTransitionGate:
    """Authoritative runtime gate for a WorkerItem plan generation.

    The gate is deliberately read-only. Row locks and transition fencing remain
    the concurrency authority; this service gives all runtime entry points the
    same decision and stable reason codes before they perform side effects.
    """

    def __init__(self, uow: ControlPlaneUnitOfWork) -> None:
        self._uow = uow

    def evaluate(
        self,
        *,
        worker_item_id: UUID,
        workspace_id: UUID,
        plan_generation: int | None,
        operation: PlanTransitionGateOperation,
        allow_transition_id: UUID | None = None,
        target_id: UUID | None = None,
    ) -> PlanTransitionGateResult:
        operation = PlanTransitionGateOperation(operation)
        placement = self._current_placement(worker_item_id, workspace_id)
        current_generation = (
            getattr(placement, "plan_generation", None) if placement is not None else None
        )
        transition = self._active_transition(worker_item_id, workspace_id)
        if (
            placement is None
            and self._has_worker_item_repository
            and (plan_generation is not None or transition is not None)
        ):
            return self._result(
                decision=PlanTransitionGateDecision.BLOCK,
                reason="worker_item_placement_missing",
                worker_item_id=worker_item_id,
                workspace_id=workspace_id,
                operation=operation,
                requested_generation=plan_generation,
                current_generation=None,
            )

        if (
            plan_generation is not None
            and current_generation is not None
            and plan_generation < current_generation
        ):
            return self._result(
                decision=PlanTransitionGateDecision.OBSOLETE,
                reason="plan_generation_stale",
                worker_item_id=worker_item_id,
                workspace_id=workspace_id,
                operation=operation,
                requested_generation=plan_generation,
                current_generation=current_generation,
            )
        if (
            plan_generation is not None
            and current_generation is not None
            and plan_generation > current_generation
        ):
            return self._result(
                decision=PlanTransitionGateDecision.BLOCK,
                reason="plan_generation_ahead",
                worker_item_id=worker_item_id,
                workspace_id=workspace_id,
                operation=operation,
                requested_generation=plan_generation,
                current_generation=current_generation,
            )

        if transition is None:
            return self._result(
                decision=PlanTransitionGateDecision.ALLOW,
                reason="allowed",
                worker_item_id=worker_item_id,
                workspace_id=workspace_id,
                operation=operation,
                requested_generation=plan_generation,
                current_generation=current_generation,
            )

        raw_transition_id = getattr(transition, "id", None)
        transition_id = (
            raw_transition_id if isinstance(raw_transition_id, UUID) else None
        )
        transition_status = _status_value(getattr(transition, "status", None))
        if (
            transition_status == WorkerTransitionStatus.WAITING_ACTIVATION.value
            and (
                (
                    operation is PlanTransitionGateOperation.ACTIVATE
                    and allow_transition_id is not None
                    and transition_id == allow_transition_id
                )
                or (
                    operation is PlanTransitionGateOperation.SCHEDULE
                    and getattr(transition, "target_execution_id", None) == target_id
                )
            )
        ):
            return self._result(
                decision=PlanTransitionGateDecision.ALLOW,
                reason="allowed",
                worker_item_id=worker_item_id,
                workspace_id=workspace_id,
                operation=operation,
                requested_generation=plan_generation,
                current_generation=current_generation,
                transition_id=transition_id,
                transition_status=transition_status,
            )
        if transition_status == WorkerTransitionStatus.BLOCKED_UNKNOWN.value:
            reason = "worker_transition_unknown"
        elif _is_in_flight(getattr(transition, "status", None)):
            reason = "worker_transition_in_flight"
        else:
            # A repository returning an unexpected active status is a failure
            # closed condition, even if a future status is added later.
            reason = "worker_transition_unknown"
        return self._result(
            decision=PlanTransitionGateDecision.BLOCK,
            reason=reason,
            worker_item_id=worker_item_id,
            workspace_id=workspace_id,
            operation=operation,
            requested_generation=plan_generation,
            current_generation=current_generation,
            transition_id=transition_id,
            transition_status=transition_status,
        )

    @property
    def _has_worker_item_repository(self) -> bool:
        return getattr(self._uow, "worker_items", None) is not None

    def _current_placement(self, worker_item_id: UUID, workspace_id: UUID) -> object | None:
        repository = getattr(self._uow, "worker_items", None)
        if repository is None:
            return None
        return repository.get_current_placement(worker_item_id, workspace_id)

    def _active_transition(self, worker_item_id: UUID, workspace_id: UUID) -> object | None:
        repository = getattr(self._uow, "worker_transitions", None)
        if repository is None:
            return None
        return repository.get_active_for_worker_item(
            worker_item_id,
            workspace_id,
            for_update=True,
        )

    @staticmethod
    def _result(
        *,
        decision: PlanTransitionGateDecision,
        reason: str,
        worker_item_id: UUID,
        workspace_id: UUID,
        operation: PlanTransitionGateOperation,
        requested_generation: int | None,
        current_generation: int | None,
        transition_id: UUID | None = None,
        transition_status: str | None = None,
    ) -> PlanTransitionGateResult:
        details: dict[str, object] = {
            "operation": operation.value,
            "worker_item_id": str(worker_item_id),
            "requested_generation": requested_generation,
            "current_generation": current_generation,
        }
        if transition_id is not None:
            details["worker_transition_id"] = str(transition_id)
        if transition_status is not None:
            details["worker_transition_status"] = transition_status
        return PlanTransitionGateResult(
            decision=decision,
            reason=reason,
            worker_item_id=worker_item_id,
            workspace_id=workspace_id,
            operation=operation,
            requested_generation=requested_generation,
            current_generation=current_generation,
            transition_id=transition_id,
            transition_status=transition_status,
            details=MappingProxyType(details),
        )


def _status_value(status: object) -> str | None:
    if isinstance(status, WorkerTransitionStatus):
        return status.value
    if isinstance(status, str):
        return status
    return None


def _is_in_flight(status: object) -> bool:
    try:
        return WorkerTransitionStatus(status) in WORKER_TRANSITION_IN_FLIGHT
    except (TypeError, ValueError):
        return False
