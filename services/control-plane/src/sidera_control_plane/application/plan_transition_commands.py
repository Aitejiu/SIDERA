from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from sidera_control_plane.application.errors import NotFound
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.errors import (
    InvalidTransition,
    ValidationError,
    VersionConflict,
)
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.execution import (
    TERMINAL_EXECUTION_STATUSES,
    ExecutionSessionStatus,
)
from sidera_control_plane.domain.gateway import (
    GatewayOperation,
    GatewayOperationStatus,
    GatewayOperationType,
    context_patch_operation_id,
    gateway_operation_id,
)
from sidera_control_plane.domain.lease import LeaseStatus
from sidera_control_plane.domain.plan_revisions import WorkerTransitionAction
from sidera_control_plane.domain.plan_transitions import (
    WorkerTransition,
    WorkerTransitionStatus,
)


class PlanTransitionCommandHandler:
    command_types = frozenset(
        {
            "ReconcileWorkerTransition",
            "ConfirmWorkerTransitionStopped",
            "RecoverWorkerTransition",
        }
    )

    def __init__(self, clock: Callable[[], datetime]) -> None:
        self._clock = clock

    def handle(
        self, uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> CommandResult:
        transition = self._get_transition(uow, envelope)
        if envelope.command_type == "ReconcileWorkerTransition":
            return self._reconcile(uow, envelope, transition)
        if envelope.command_type == "ConfirmWorkerTransitionStopped":
            return self._confirm_stopped(uow, envelope, transition)
        if envelope.command_type == "RecoverWorkerTransition":
            return self._recover(uow, envelope, transition)
        raise ValidationError(
            f"unsupported plan transition command: {envelope.command_type}"
        )

    def _reconcile(
        self,
        uow: ControlPlaneUnitOfWork,
        envelope: CommandEnvelope,
        transition: WorkerTransition,
    ) -> CommandResult:
        self._require_unknown(transition)
        operation = self._validate_operation_facts(uow, transition, envelope.payload)
        patch = self._validate_patch_hash(uow, transition, envelope.payload)
        event = self._audit_event(
            envelope,
            transition,
            event_type="WorkerTransitionReconciliationRequested",
            payload=self._audit_payload(
                envelope,
                transition,
                operation=operation,
                patch_hash=patch,
            ),
        )
        uow.events.append_with_outbox(event)
        return self._result(
            envelope,
            transition,
            event_ids=(event.id,),
            operation=operation,
            patch_hash=patch,
        )

    def _confirm_stopped(
        self,
        uow: ControlPlaneUnitOfWork,
        envelope: CommandEnvelope,
        transition: WorkerTransition,
    ) -> CommandResult:
        self._require_unknown(transition)
        self._source_execution(uow, transition, envelope.payload)
        event = self._audit_event(
            envelope,
            transition,
            event_type="WorkerTransitionStoppedConfirmed",
            payload=self._audit_payload(envelope, transition),
        )
        uow.events.append_with_outbox(event)
        data = self._data(transition)
        data["source_session_stopped"] = True
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=transition.id,
            aggregate_version=transition.version,
            event_ids=(event.id,),
            data=data,
        )

    def _recover(
        self,
        uow: ControlPlaneUnitOfWork,
        envelope: CommandEnvelope,
        transition: WorkerTransition,
    ) -> CommandResult:
        self._require_unknown(transition)
        strategy = _required_str(envelope.payload, "strategy")
        operation = self._validate_operation_facts(uow, transition, envelope.payload)
        patch = self._validate_patch_hash(uow, transition, envelope.payload)
        now = self._clock()

        if strategy == "reconcile_adapter":
            event = self._audit_event(
                envelope,
                transition,
                event_type="WorkerTransitionRecoveryRequested",
                payload=self._audit_payload(
                    envelope,
                    transition,
                    strategy=strategy,
                    operation=operation,
                    patch_hash=patch,
                ),
            )
            uow.events.append_with_outbox(event)
            data = self._data(transition)
            data["recovery_strategy"] = strategy
            data["adapter_call"] = "not_started"
            return CommandResult(
                command_id=envelope.id,
                aggregate_id=transition.id,
                aggregate_version=transition.version,
                event_ids=(event.id,),
                data=data,
            )

        if strategy == "continue_after_confirmed_stop":
            self._source_execution(uow, transition, envelope.payload)
            previous_version = transition.version
            transition.recover_after_confirmed_stop(now=now)
        elif strategy == "fail_closed_branch":
            self._source_execution(uow, transition, envelope.payload)
            previous_version = transition.version
            transition.fail_closed(failure_code="operator_fail_closed", now=now)
        elif strategy == "continue_after_confirmed_patch_absence":
            patch = self._require_patch_absence(
                uow, transition, envelope.payload, operation
            )
            previous_version = transition.version
            transition.recover_after_confirmed_patch_absence(now=now)
        else:
            raise ValidationError(
                "unsupported worker transition recovery strategy",
                details={"strategy": strategy},
            )

        uow.worker_transitions.save(transition, previous_version)
        event = self._state_event(
            envelope,
            transition,
            event_type="WorkerTransitionRecovered",
            payload=self._audit_payload(
                envelope,
                transition,
                strategy=strategy,
                operation=operation,
                patch_hash=patch,
            ),
        )
        audit = self._audit_event(
            envelope,
            transition,
            event_type="WorkerTransitionRecoveryRequested",
            payload=self._audit_payload(
                envelope,
                transition,
                strategy=strategy,
                operation=operation,
                patch_hash=patch,
            ),
        )
        uow.events.append_with_outbox(event)
        uow.events.append_with_outbox(audit)
        data = self._data(transition)
        data["recovery_strategy"] = strategy
        if strategy == "continue_after_confirmed_patch_absence":
            data["fallback_patch_hash"] = patch
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=transition.id,
            aggregate_version=transition.version,
            event_ids=(event.id, audit.id),
            data=data,
        )

    @staticmethod
    def _get_transition(
        uow: ControlPlaneUnitOfWork, envelope: CommandEnvelope
    ) -> WorkerTransition:
        if envelope.target_type != "worker_transition":
            raise ValidationError(
                "worker transition commands require target_type=worker_transition"
            )
        if envelope.target_id is None or envelope.expected_version is None:
            raise ValidationError(
                "worker transition commands require target_id and expected_version"
            )
        transition = uow.worker_transitions.get(
            envelope.target_id,
            envelope.workspace_id,
            for_update=True,
        )
        if transition is None:
            raise NotFound("worker transition not found")
        if transition.version != envelope.expected_version:
            raise VersionConflict(envelope.expected_version, transition.version)
        return transition

    @staticmethod
    def _require_unknown(transition: WorkerTransition) -> None:
        if transition.status is not WorkerTransitionStatus.BLOCKED_UNKNOWN:
            raise InvalidTransition(
                "worker transition recovery requires blocked_unknown status"
            )

    def _source_execution(
        self,
        uow: ControlPlaneUnitOfWork,
        transition: WorkerTransition,
        payload: Mapping[str, object],
    ):
        source_execution_id = transition.source_execution_id
        supplied_execution_id = _optional_uuid(payload, "source_execution_id")
        if supplied_execution_id is not None and supplied_execution_id != source_execution_id:
            raise ValidationError("source_execution_id does not match transition")
        supplied_session_id = _optional_uuid(payload, "source_session_id")
        if source_execution_id is None:
            if supplied_session_id is not None:
                raise ValidationError("source_session_id is not valid without a source execution")
            return None

        execution = uow.executions.get(
            source_execution_id,
            transition.workspace_id,
            for_update=True,
        )
        if execution is None or execution.worker_item_id != transition.worker_item_id:
            raise ValidationError("source execution does not match transition")
        if execution.status not in TERMINAL_EXECUTION_STATUSES:
            raise InvalidTransition("source execution is not proven stopped")
        if execution.active_lease_id is not None:
            raise InvalidTransition("source execution still has an active lease")
        active_lease = uow.leases.get_active_for_execution(
            execution.id,
            transition.workspace_id,
            for_update=True,
        )
        if active_lease is not None and active_lease.status is LeaseStatus.ACTIVE:
            raise InvalidTransition("source execution still has an active lease")
        active_session = uow.sessions.get_active_for_execution(
            execution.id,
            for_update=True,
        )
        if active_session is not None and active_session.status is ExecutionSessionStatus.ACTIVE:
            raise InvalidTransition("source execution still has an active session")
        source_session_id = transition.source_session_id
        if supplied_session_id is not None and supplied_session_id != source_session_id:
            raise ValidationError("source_session_id does not match transition")
        if source_session_id is not None:
            source_session = uow.sessions.get_any(
                source_session_id,
                transition.workspace_id,
                for_update=True,
            )
            if source_session is None or source_session.execution_id != execution.id:
                raise ValidationError("source session does not match transition")
            if source_session.status is ExecutionSessionStatus.ACTIVE:
                raise InvalidTransition("source execution still has an active session")
        return execution

    def _validate_operation_facts(
        self,
        uow: ControlPlaneUnitOfWork,
        transition: WorkerTransition,
        payload: Mapping[str, object],
    ) -> GatewayOperation | None:
        supplied_operation_id = _optional_uuid(payload, "operation_id")
        expected = _expected_operation(transition)
        if supplied_operation_id is None:
            return None
        if expected is None or supplied_operation_id != expected[0]:
            raise ValidationError("operation_id does not match transition operation")
        operation = uow.gateway_operations.get_by_operation_id(
            supplied_operation_id,
            transition.workspace_id,
            for_update=True,
        )
        if operation is None or operation.operation_type is not expected[1]:
            raise ValidationError("transition operation is not recorded exactly")
        expected_execution_id = (
            transition.source_execution_id
            if expected[1]
            in {
                GatewayOperationType.STOP_SESSION,
                GatewayOperationType.APPLY_CONTEXT_PATCH,
            }
            else transition.target_execution_id
        )
        if (
            expected_execution_id is not None
            and operation.execution_id != expected_execution_id
        ):
            raise ValidationError("transition operation execution does not match")
        return operation

    @staticmethod
    def _validate_patch_hash(
        uow: ControlPlaneUnitOfWork,
        transition: WorkerTransition,
        payload: Mapping[str, object],
    ) -> str | None:
        supplied_hash = payload.get("patch_hash")
        if supplied_hash is None:
            return None
        if not isinstance(supplied_hash, str) or not supplied_hash.strip():
            raise ValidationError("patch_hash must be a non-empty string")
        patches = uow.context_patches.list_for_worker_item(
            transition.worker_item_id,
            transition.workspace_id,
        )
        matching = next(
            (
                patch
                for patch in patches
                if patch.worker_transition_id == transition.id
                and patch.patch_hash == supplied_hash
            ),
            None,
        )
        if matching is None:
            raise ValidationError("patch_hash does not match transition patch")
        return matching.patch_hash

    def _require_patch_absence(
        self,
        uow: ControlPlaneUnitOfWork,
        transition: WorkerTransition,
        payload: Mapping[str, object],
        operation: GatewayOperation | None,
    ) -> str:
        if transition.action is not WorkerTransitionAction.PATCH:
            raise InvalidTransition("patch-absence recovery requires a patch transition")
        patches = uow.context_patches.list_for_worker_item(
            transition.worker_item_id,
            transition.workspace_id,
        )
        patch = next(
            (item for item in patches if item.worker_transition_id == transition.id),
            None,
        )
        if patch is None:
            raise InvalidTransition("patch absence is not proven without a patch record")
        supplied_hash = payload.get("patch_hash")
        if supplied_hash is not None and supplied_hash != patch.patch_hash:
            raise ValidationError("patch_hash does not match transition patch")
        if uow.context_patches.get_acknowledgement(patch.id, transition.workspace_id) is not None:
            raise InvalidTransition("patch acknowledgement already exists")
        expected = _expected_operation(transition)
        if expected is None or expected[1] is not GatewayOperationType.APPLY_CONTEXT_PATCH:
            raise InvalidTransition("patch operation is not deterministically addressable")
        if operation is None:
            operation = uow.gateway_operations.get_by_operation_id(
                expected[0], transition.workspace_id, for_update=True
            )
        if operation is None or operation.operation_type is not GatewayOperationType.APPLY_CONTEXT_PATCH:
            raise InvalidTransition("patch application absence is not proven")
        if operation.status is not GatewayOperationStatus.FAILED:
            raise InvalidTransition("patch application absence requires a known failed operation")
        return patch.patch_hash

    def _state_event(
        self,
        envelope: CommandEnvelope,
        transition: WorkerTransition,
        *,
        event_type: str,
        payload: dict[str, object],
    ) -> EventData:
        return EventData(
            id=uuid5(
                NAMESPACE_URL,
                f"sidera.worker-transition-state:{envelope.id}:{event_type}",
            ),
            workspace_id=transition.workspace_id,
            aggregate_type="worker_transition",
            aggregate_id=transition.id,
            aggregate_version=transition.version,
            event_type=event_type,
            payload=payload,
            actor_type=envelope.actor_type,
            actor_id=envelope.actor_id,
            causation_id=envelope.id,
            correlation_id=envelope.correlation_id,
        )

    def _audit_event(
        self,
        envelope: CommandEnvelope,
        transition: WorkerTransition,
        *,
        event_type: str,
        payload: dict[str, object],
    ) -> EventData:
        return EventData(
            id=uuid5(
                NAMESPACE_URL,
                f"sidera.worker-transition-recovery:{envelope.id}:{event_type}",
            ),
            workspace_id=transition.workspace_id,
            aggregate_type="worker_transition_recovery",
            aggregate_id=uuid5(
                NAMESPACE_URL,
                f"sidera.worker-transition-recovery-aggregate:{envelope.id}",
            ),
            aggregate_version=1,
            event_type=event_type,
            payload=payload,
            actor_type=envelope.actor_type,
            actor_id=envelope.actor_id,
            causation_id=envelope.id,
            correlation_id=envelope.correlation_id,
        )

    @staticmethod
    def _audit_payload(
        envelope: CommandEnvelope,
        transition: WorkerTransition,
        *,
        strategy: str | None = None,
        operation: GatewayOperation | None = None,
        patch_hash: str | None = None,
    ) -> dict[str, object]:
        reason = _required_str(envelope.payload, "reason")
        evidence_refs = _required_refs(envelope.payload)
        payload: dict[str, object] = {
            "worker_transition_id": str(transition.id),
            "plan_transition_id": str(transition.plan_transition_id),
            "root_task_id": str(transition.root_task_id),
            "worker_item_id": str(transition.worker_item_id),
            "status": transition.status.value,
            "writer_state": transition.writer_state.value,
            "reason": reason,
            "evidence_refs": list(evidence_refs),
        }
        if strategy is not None:
            payload["strategy"] = strategy
        if operation is not None:
            payload["operation_id"] = str(operation.operation_id)
            payload["operation_type"] = operation.operation_type.value
            payload["operation_status"] = operation.status.value
        if patch_hash is not None:
            payload["patch_hash"] = patch_hash
        return payload

    @staticmethod
    def _data(
        transition: WorkerTransition,
    ) -> dict[str, object]:
        return {
            "worker_transition_id": str(transition.id),
            "plan_transition_id": str(transition.plan_transition_id),
            "status": transition.status.value,
            "writer_state": transition.writer_state.value,
            "action": transition.action.value,
            "version": transition.version,
        }

    def _result(
        self,
        envelope: CommandEnvelope,
        transition: WorkerTransition,
        *,
        event_ids: tuple[UUID, ...],
        operation: GatewayOperation | None,
        patch_hash: str | None,
    ) -> CommandResult:
        data = self._data(transition)
        if operation is not None:
            data["operation_id"] = str(operation.operation_id)
            data["operation_status"] = operation.status.value
        if patch_hash is not None:
            data["patch_hash"] = patch_hash
        return CommandResult(
            command_id=envelope.id,
            aggregate_id=transition.id,
            aggregate_version=transition.version,
            event_ids=event_ids,
            data=data,
        )


def _expected_operation(
    transition: WorkerTransition,
) -> tuple[UUID, GatewayOperationType] | None:
    if transition.action in {
        WorkerTransitionAction.CONTINUE,
        WorkerTransitionAction.RETIRE,
    }:
        if transition.source_execution_id is None:
            return None
        return (
            gateway_operation_id(
                transition.source_execution_id,
                GatewayOperationType.STOP_SESSION,
            ),
            GatewayOperationType.STOP_SESSION,
        )
    if (
        transition.action is WorkerTransitionAction.PATCH
        and transition.attempt_count > 0
    ):
        return (
            context_patch_operation_id(
                transition.id,
                "apply_context_patch",
                logical_attempt=transition.attempt_count,
            ),
            GatewayOperationType.APPLY_CONTEXT_PATCH,
        )
    if transition.target_execution_id is not None:
        return (
            gateway_operation_id(
                transition.target_execution_id,
                GatewayOperationType.START_SESSION,
            ),
            GatewayOperationType.START_SESSION,
        )
    return None


def _required_str(payload: Mapping[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be a non-empty string")
    return value.strip()


def _required_refs(payload: Mapping[str, object]) -> tuple[str, ...]:
    value = payload.get("evidence_refs")
    if not isinstance(value, (list, tuple)) or not value:
        raise ValidationError("evidence_refs must contain at least one reference")
    refs = tuple(item.strip() for item in value if isinstance(item, str) and item.strip())
    if len(refs) != len(value):
        raise ValidationError("evidence_refs cannot contain empty values")
    return refs


def _optional_uuid(payload: Mapping[str, object], name: str) -> UUID | None:
    value = payload.get(name)
    if value is None:
        return None
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{name} must be a UUID") from exc
