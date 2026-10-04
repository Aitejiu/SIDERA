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
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.gateway import (
    DeliveryOutcome,
    DeliveryResult,
    GatewayOperationStatus,
    GatewayOperationType,
    NativeInputIdentity,
)
from sidera_control_plane.domain.inputs import (
    ExecutionInput,
    ExecutionInputStatus,
    input_operation_id,
)


class InputGateway(Protocol):
    def send_input(self, item: ExecutionInput) -> DeliveryResult:
        raise NotImplementedError

    def query_input(self, item: ExecutionInput) -> DeliveryResult:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class InputDispatchReport:
    claimed: int
    delivered: int
    deferred: int
    blocked: int
    unknown: int
    failed: int
    accepted: int = 0
    queried: int = 0


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
        self._query_turn = False

    def run_once(self, batch_size: int = 20) -> InputDispatchReport:
        if not 1 <= batch_size <= 1_000:
            raise ValueError("batch_size must be between 1 and 1000")
        with self._uow_factory() as uow:
            select_pending = getattr(
                uow.execution_inputs, "select_pending_native", None
            )
            pending_budget = (
                batch_size // 2 if batch_size > 1 else int(self._query_turn)
            )
            pending = () if select_pending is None else select_pending(pending_budget)
            claimed = uow.execution_inputs.claim_queued(batch_size - len(pending))
            remaining = batch_size - len(claimed) - len(pending)
            if remaining and select_pending is not None:
                selected_ids = tuple(item.id for item in (*pending, *claimed))
                pending += select_pending(remaining, exclude_ids=selected_ids)
        self._query_turn = not self._query_turn

        delivered = deferred = blocked = unknown = failed = accepted = 0
        for item, query_only in (
            *((item, False) for item in claimed),
            *((item, True) for item in pending),
        ):
            transition_fence_reason = (
                None if query_only else self._transition_fence_reason(item)
            )
            if transition_fence_reason is not None:
                result = DeliveryResult(
                    operation_id=item.operation_id or item.id,
                    outcome=DeliveryOutcome.BLOCKED,
                    accepted_sequence=None,
                    failure_code=transition_fence_reason,
                )
            else:
                result = (
                    self._gateway.query_input(item) if query_only else self._send(item)
                )
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
            elif category == "accepted":
                accepted += 1
        return InputDispatchReport(
            claimed=len(claimed) + len(pending),
            delivered=delivered,
            deferred=deferred,
            blocked=blocked,
            unknown=unknown,
            failed=failed,
            accepted=accepted,
            queried=len(pending),
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
                operation_id=input_operation_id(item.id),
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
            if (
                current is None
                or current.status is not ExecutionInputStatus.DISPATCHING
            ):
                return "ignored"
            if result.native_identity is not None or self._has_native_origin(
                uow, current
            ):
                return self._persist_native_result(uow, current, item, result)
            previous_version = current.version
            operation_id = item.operation_id or input_operation_id(item.id)
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

    @staticmethod
    def _has_native_origin(uow, item) -> bool:
        operations = getattr(uow, "gateway_operations", None)
        if operations is not None and item.operation_id is not None:
            operation = operations.get_by_operation_id(
                item.operation_id, item.workspace_id
            )
            if operation is not None and (
                operation.operation_type is GatewayOperationType.SEND_INPUT
                and operation.execution_id == item.execution_id
                and operation.session_id == item.session_id
            ):
                try:
                    identity = NativeInputIdentity.from_result_ref(operation.result_ref)
                except ValidationError:
                    pass
                else:
                    if (
                        identity.workspace_id == item.workspace_id
                        and identity.input_id == item.id
                        and identity.execution_id == item.execution_id
                        and identity.session_id == item.session_id
                        and identity.operation_id == item.operation_id
                    ):
                        return True
        authorities = getattr(uow, "execution_authorities", None)
        return (
            authorities is not None
            and authorities.get_link(item.workspace_id, item.execution_id) is not None
        )

    def _persist_native_result(self, uow, current, item, result):
        identity = result.native_identity
        if identity is None:
            code = result.failure_code or "native_input_identity_unconfirmed"
            if current.failure_code != code:
                previous_version = current.version
                current.failure_code = code
                current.version += 1
                uow.execution_inputs.save(current, previous_version)
                uow.events.append_with_outbox(
                    _input_dispatch_event(
                        item,
                        operation_id=current.operation_id,
                        aggregate_version=current.version,
                        event_type="ExecutionInputDeliveryUncertain",
                        status=current.status.value,
                        accepted_sequence=None,
                        failure_code=code,
                        now=self._clock(),
                        native_origin=True,
                    )
                )
            return "unknown"
        if (
            identity.workspace_id != current.workspace_id
            or identity.input_id != current.id
            or identity.execution_id != current.execution_id
            or identity.session_id != current.session_id
            or identity.operation_id != current.operation_id
            or result.operation_id != current.operation_id
            or current.operation_id != input_operation_id(current.id)
        ):
            return "ignored"
        operation = uow.gateway_operations.get_by_operation_id(
            identity.operation_id, identity.workspace_id, for_update=True
        )
        if result.outcome not in {
            DeliveryOutcome.UNKNOWN,
            DeliveryOutcome.ACCEPTED,
        } and (
            operation is None
            or operation.status is not GatewayOperationStatus.RUNNING
            or operation.execution_id != current.execution_id
            or operation.session_id != current.session_id
            or operation.operation_type is not GatewayOperationType.SEND_INPUT
            or NativeInputIdentity.from_result_ref(operation.result_ref) != identity
        ):
            return "ignored"
        previous_version = current.version
        failure_code = result.failure_code or result.outcome.value
        if result.outcome in {DeliveryOutcome.ACCEPTED, DeliveryOutcome.UNKNOWN}:
            code = None if result.outcome is DeliveryOutcome.ACCEPTED else failure_code
            category = "accepted" if code is None else "unknown"
            if current.failure_code == code:
                return category
            current.failure_code = code
            current.version += 1
            event_type = (
                "ExecutionInputDeliveryPending"
                if code is None
                else "ExecutionInputDeliveryUncertain"
            )
        else:
            previous_operation_version = operation.version
            if result.outcome is DeliveryOutcome.DELIVERED:
                if (
                    type(result.accepted_sequence) is not int
                    or result.accepted_sequence < 1
                ):
                    return "ignored"
                current.deliver()
                operation.succeed(
                    result_ref=identity.result_ref(
                        evidence_sequence=result.accepted_sequence
                    ),
                    now=self._clock(),
                )
                category, event_type = "delivered", "ExecutionInputDelivered"
            elif result.outcome is DeliveryOutcome.BLOCKED:
                current.block_unknown(failure_code=failure_code)
                operation.fail(failure_code=failure_code, now=self._clock())
                category, event_type = "blocked", "ExecutionInputBlocked"
            else:
                current.fail(failure_code=failure_code)
                operation.fail(failure_code=failure_code, now=self._clock())
                category, event_type = "failed", "ExecutionInputFailed"
            uow.gateway_operations.save(operation, previous_operation_version)
        uow.execution_inputs.save(current, previous_version)
        uow.events.append_with_outbox(
            _input_dispatch_event(
                item,
                operation_id=identity.operation_id,
                aggregate_version=current.version,
                event_type=event_type,
                status=current.status.value,
                accepted_sequence=result.accepted_sequence,
                failure_code=current.failure_code,
                now=self._clock(),
                native_identity=identity,
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
    native_identity: NativeInputIdentity | None = None,
    native_origin: bool = False,
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
            **({"execution_mode": "native_profile"} if native_origin else {}),
            **(
                {
                    "execution_mode": "native_profile",
                    "run_id": str(native_identity.run_id),
                    "binding_version": native_identity.binding_version,
                    "authority_id": str(native_identity.authority_id),
                }
                if native_identity is not None
                else {}
            ),
        },
        actor_type="gateway",
        actor_id="input-dispatcher",
        causation_id=item.source_command_id or operation_id,
        correlation_id=operation_id,
    )
