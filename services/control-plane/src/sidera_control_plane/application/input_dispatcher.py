from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

from sidera_control_plane.application.handoff_gate import (
    HandoffGateOperation,
    HandoffOperationGate,
)
from sidera_control_plane.application.plan_transition_gate import (
    PlanTransitionGate,
    PlanTransitionGateOperation,
)
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.application.worker_gateway import GatewayRecoveryRequired
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.gateway import DeliveryOutcome, DeliveryResult
from sidera_control_plane.domain.inputs import ExecutionInput, ExecutionInputStatus


class InputGateway(Protocol):
    def send_input(self, item: ExecutionInput) -> DeliveryResult:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class InputDispatchReport:
    claimed: int
    delivered: int
    deferred: int
    blocked: int
    unknown: int
    failed: int


_SAFE_BOUNDARY_FAILURE_CODES = frozenset(
    {
        "prompt_busy",
        "safe_boundary_unavailable",
        "worker_transition_fence",
        "worker_transition_in_flight",
        "handoff_in_progress",
        "handoff_gate_unavailable",
    }
)


class InputDispatcher:
    """Deliver persisted Session inputs without an in-process queue."""

    def __init__(
        self,
        uow_factory: Callable[[], ControlPlaneUnitOfWork],
        gateway: InputGateway,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._uow_factory = uow_factory
        self._gateway = gateway
        self._clock = clock

    def run_once(self, batch_size: int = 20) -> InputDispatchReport:
        if not 1 <= batch_size <= 1_000:
            raise ValueError("batch_size must be between 1 and 1000")
        with self._uow_factory() as uow:
            claimed = uow.execution_inputs.claim_queued(batch_size)

        delivered = deferred = blocked = unknown = failed = 0
        for item in claimed:
            transition_fence_reason = self._transition_fence_reason(item)
            if transition_fence_reason is not None:
                result = DeliveryResult(
                    operation_id=item.operation_id or item.id,
                    outcome=DeliveryOutcome.BLOCKED,
                    accepted_sequence=None,
                    failure_code=transition_fence_reason,
                )
            else:
                result = self._send(item)
            category = self._persist_result(item, result)
            if category == "delivered":
                delivered += 1
            elif category == "deferred":
                deferred += 1
            elif category == "blocked":
                blocked += 1
            elif category == "unknown":
                blocked += 1
                unknown += 1
            elif category == "failed":
                failed += 1
        return InputDispatchReport(
            claimed=len(claimed),
            delivered=delivered,
            deferred=deferred,
            blocked=blocked,
            unknown=unknown,
            failed=failed,
        )

    def _transition_fence_reason(self, item: ExecutionInput) -> str | None:
        """Re-read the authoritative WorkerItem fence after claiming input."""
        with self._uow_factory() as uow:
            executions = getattr(uow, "executions", None)
            if executions is None:
                return None
            try:
                execution = executions.get(
                    item.execution_id, item.workspace_id, for_update=True
                )
                worker_item_id = getattr(execution, "worker_item_id", None)
                if worker_item_id is None:
                    return None
                handoff_gate = HandoffOperationGate(uow).evaluate(
                    worker_item_id=worker_item_id,
                    workspace_id=item.workspace_id,
                    operation=HandoffGateOperation.USER_INPUT,
                )
                if not handoff_gate.allowed:
                    return handoff_gate.blocking_code
                gate = PlanTransitionGate(uow).evaluate(
                    worker_item_id=worker_item_id,
                    workspace_id=item.workspace_id,
                    plan_generation=None,
                    operation=PlanTransitionGateOperation.INPUT,
                )
                return None if gate.allowed else gate.reason
            except Exception:  # noqa: BLE001
                # A failed fence read must not turn into an external write.
                return "worker_transition_fence"

    def _send(self, item: ExecutionInput) -> DeliveryResult:
        operation_id = item.operation_id
        if operation_id is None:
            return DeliveryResult(
                operation_id=item.id,
                outcome=DeliveryOutcome.UNKNOWN,
                accepted_sequence=None,
                failure_code="input_operation_id_missing",
            )
        try:
            result = self._gateway.send_input(item)
        except GatewayRecoveryRequired:
            return DeliveryResult(
                operation_id=operation_id,
                outcome=DeliveryOutcome.UNKNOWN,
                accepted_sequence=None,
                failure_code="gateway_recovery_required",
            )
        except Exception:  # noqa: BLE001
            return DeliveryResult(
                operation_id=operation_id,
                outcome=DeliveryOutcome.UNKNOWN,
                accepted_sequence=None,
                failure_code="gateway_dispatch_unknown",
            )
        if result.operation_id != operation_id:
            return DeliveryResult(
                operation_id=operation_id,
                outcome=DeliveryOutcome.UNKNOWN,
                accepted_sequence=None,
                failure_code="adapter_operation_id_mismatch",
            )
        return result

    def _persist_result(self, item: ExecutionInput, result: DeliveryResult) -> str:
        with self._uow_factory() as uow:
            current = uow.execution_inputs.get(
                item.id, item.workspace_id, for_update=True
            )
            if current is None or current.status is not ExecutionInputStatus.DISPATCHING:
                return "ignored"
            previous_version = current.version
            operation_id = item.operation_id or item.id
            failure_code = result.failure_code or result.outcome.value
            if result.outcome is DeliveryOutcome.DELIVERED:
                current.deliver()
                category = "delivered"
                event_type = "ExecutionInputDelivered"
            elif result.outcome is DeliveryOutcome.BLOCKED:
                if failure_code in _SAFE_BOUNDARY_FAILURE_CODES:
                    current.defer()
                    category = "deferred"
                    event_type = "ExecutionInputDeferred"
                else:
                    current.block_unknown(failure_code=failure_code)
                    category = "blocked"
                    event_type = "ExecutionInputBlocked"
            elif result.outcome is DeliveryOutcome.UNKNOWN:
                current.block_unknown(failure_code=failure_code)
                category = "unknown"
                event_type = "ExecutionInputBlocked"
            else:
                current.fail(failure_code=failure_code)
                category = "failed"
                event_type = "ExecutionInputFailed"
            uow.execution_inputs.save(current, previous_version)
            uow.events.append_with_outbox(
                _input_dispatch_event(
                    item,
                    operation_id=operation_id,
                    aggregate_version=current.version,
                    event_type=event_type,
                    status=current.status.value,
                    accepted_sequence=result.accepted_sequence,
                    failure_code=(
                        None if category in {"delivered", "deferred"} else failure_code
                    ),
                    now=self._clock(),
                )
            )
            return category


def _input_dispatch_event(
    item: ExecutionInput,
    *,
    operation_id: UUID,
    aggregate_version: int,
    event_type: str,
    status: str,
    accepted_sequence: int | None,
    failure_code: str | None,
    now: datetime,
) -> EventData:
    return EventData(
        id=uuid4(),
        workspace_id=item.workspace_id,
        aggregate_type="execution_input",
        aggregate_id=item.id,
        aggregate_version=aggregate_version,
        event_type=event_type,
        payload={
            "execution_id": str(item.execution_id),
            "session_id": str(item.session_id),
            "operation_id": str(operation_id),
            "status": status,
            "accepted_sequence": accepted_sequence,
            "failure_code": failure_code,
            "recorded_at": now.isoformat(),
        },
        actor_type="gateway",
        actor_id="input-dispatcher",
        causation_id=item.source_command_id or operation_id,
        correlation_id=operation_id,
    )
