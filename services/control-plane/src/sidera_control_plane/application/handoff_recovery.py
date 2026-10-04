from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Protocol, cast
from uuid import UUID

from sidera_control_plane.application.handoff_candidates import (
    runtime_snapshot_for_handoff,
)
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.application.worker_runtime import (
    ResolvedWorkerRuntime,
    WorkerRuntimeInspectionPort,
    WorkerRuntimeResolver,
)
from sidera_control_plane.domain.errors import DomainError, VersionConflict
from sidera_control_plane.domain.execution import (
    Execution,
    ExecutionSessionStatus,
    ExecutionStatus,
)
from sidera_control_plane.domain.gateway import (
    GatewayOperation,
    GatewayOperationStatus,
    GatewayOperationType,
    gateway_operation_id,
    handoff_inspection_outcome_from_result_ref,
    handoff_step_operation_id,
)
from sidera_control_plane.domain.handoff_evidence import (
    HandoffInspectionOutcome,
    HandoffOperationInspection,
    HandoffWorkspaceInspection,
    HandoffWorkspaceTransfer,
    HandoffWorkspaceTransferStatus,
)
from sidera_control_plane.domain.handoff_recovery import (
    HandoffRecoveryDecision,
    HandoffRecoveryInspection,
    HandoffRecoveryResult,
    HandoffRecoveryStrategy,
    HandoffSafeConclusion,
)
from sidera_control_plane.domain.handoffs import (
    HandoffPhase,
    HandoffSourceChannel,
    HandoffTransition,
    HandoffTransitionStatus,
)
from sidera_control_plane.domain.lease import LeaseStatus
from sidera_control_plane.domain.modules import HandoffCheckpointMode
from sidera_control_plane.domain.worker import (
    SCHEDULABLE_WORKER_STATUSES,
    WorkerDesiredState,
)
from sidera_control_plane.domain.workspace import WorkspaceLifecycle

_FAILURE_STATUSES = frozenset(
    {
        HandoffTransitionStatus.BLOCKED,
        HandoffTransitionStatus.BLOCKED_UNKNOWN,
        HandoffTransitionStatus.FAILED,
    }
)
_DERIVED_RETRY_PHASES = frozenset(
    {
        HandoffPhase.CHECKPOINT_SOURCE,
        HandoffPhase.FREEZE_SOURCE_WORKSPACE,
        HandoffPhase.TRANSFER_WORKSPACE,
        HandoffPhase.ACKNOWLEDGE_CONTEXT,
    }
)
_CANONICAL_RETRY_PHASES = frozenset(
    {HandoffPhase.STOP_SOURCE, HandoffPhase.START_TARGET_SESSION}
)
_WORKSPACE_PHASES = frozenset(
    {HandoffPhase.FREEZE_SOURCE_WORKSPACE, HandoffPhase.TRANSFER_WORKSPACE}
)
_TARGET_REPLACEMENT_PHASES = frozenset({HandoffPhase.START_TARGET_SESSION})
_INSPECTION_REFERENCE_PREFIX = "handoff-recovery://"


class HandoffRecoveryError(DomainError):
    """A recovery operation cannot be proven safe from bounded evidence."""

    code = "handoff_recovery_not_allowed"


class HandoffRecoveryGateway(Protocol):
    def inspect_handoff_operation(
        self,
        *,
        workspace_id: UUID,
        transition_id: UUID,
        operation_id: UUID,
        operation_type: GatewayOperationType,
        now: datetime,
    ) -> HandoffOperationInspection:
        raise NotImplementedError


class HandoffRecoveryWorkspace(Protocol):
    def inspect(
        self,
        *,
        transition_id: UUID,
        workspace_id: UUID,
        logical_attempt: int,
        operation_id: UUID,
        now: datetime,
    ) -> HandoffWorkspaceInspection:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class _OperationSpec:
    operation_id: UUID
    operation_type: GatewayOperationType
    execution_id: UUID
    identity_matches_blocked_id: bool


@dataclass(frozen=True, slots=True)
class _PersistedState:
    transition: HandoffTransition
    source_execution: Execution | None
    source_session_state: str
    target_execution: Execution | None
    target_session_id: UUID | None
    target_session_identity_matches: bool
    target_session_state: str | None
    target_manifest_state: str | None
    target_lease: object | None
    gateway_operation: GatewayOperation | None
    workspace_transfer: HandoffWorkspaceTransfer | None


class HandoffRecoveryService:
    """Reconcile and recover a handoff from evidence without replaying unknown work."""

    def __init__(
        self,
        uow_factory: Callable[[], ControlPlaneUnitOfWork],
        *,
        gateway: HandoffRecoveryGateway | None = None,
        workspace_handoff: HandoffRecoveryWorkspace | None = None,
        runtime_resolver: WorkerRuntimeInspectionPort | None = None,
        heartbeat_timeout: timedelta = timedelta(seconds=45),
    ) -> None:
        if heartbeat_timeout <= timedelta(0):
            raise ValueError("handoff recovery heartbeat timeout must be positive")
        self._uow_factory = uow_factory
        self._gateway = gateway
        self._workspace_handoff = workspace_handoff
        self._runtime_resolver = runtime_resolver or WorkerRuntimeResolver()
        self._heartbeat_timeout = heartbeat_timeout

    def inspect_unknown(
        self,
        *,
        transition: HandoffTransition,
        now: datetime,
    ) -> HandoffRecoveryInspection:
        """Inspect durable and boundary evidence without issuing an operation."""

        _aware(now, "handoff recovery inspection timestamp")
        _require_legacy_transition(transition)

        before = self._load_state(transition)
        if before.transition.version != transition.version:
            raise VersionConflict(transition.version, before.transition.version)
        if before.transition.status not in _FAILURE_STATUSES:
            raise HandoffRecoveryError("handoff transition is not recoverable")

        operation_spec = _operation_spec(before.transition)
        operation_inspection: HandoffOperationInspection | None = None
        if operation_spec is not None:
            operation_inspection = self._inspect_gateway_operation(
                before,
                operation_spec,
                now=now,
            )

        workspace_inspection: HandoffWorkspaceInspection | None = None
        if before.transition.phase is HandoffPhase.TRANSFER_WORKSPACE:
            workspace_inspection = self._inspect_workspace_transfer(
                before,
                now=now,
            )

        after = self._load_state(transition)
        if after.transition.version != before.transition.version:
            raise VersionConflict(before.transition.version, after.transition.version)

        operations = (operation_inspection,) if operation_inspection else ()
        conclusions = _safe_conclusions(
            after,
            operations=operations,
            workspace_inspection=workspace_inspection,
        )
        refs = _inspection_refs(
            after.transition,
            operations=operations,
            workspace_inspection=workspace_inspection,
        )
        return HandoffRecoveryInspection(
            transition_id=after.transition.id,
            transition_version=after.transition.version,
            logical_attempt=after.transition.logical_attempt,
            phase=after.transition.phase,
            writer_state=after.transition.writer_state,
            blocked_operation_id=after.transition.blocked_operation_id,
            operation_inspections=operations,
            workspace_inspection=workspace_inspection,
            source_session_state=after.source_session_state,
            target_session_state=after.target_session_state,
            target_manifest_state=after.target_manifest_state,
            safe_conclusions=conclusions,
            evidence_refs=refs,
            inspected_at=now,
        )

    def available_strategies(
        self,
        *,
        transition: HandoffTransition,
        inspection: HandoffRecoveryInspection,
    ) -> tuple[HandoffRecoveryStrategy, ...]:
        """Return only strategies supported by this exact inspection."""

        if transition.handoff_schema_version == 2:
            return ()
        _require_legacy_transition(transition)
        _validate_inspection(transition, inspection)
        if transition.status not in _FAILURE_STATUSES:
            return ()
        conclusions = set(inspection.safe_conclusions)
        phase = transition.phase
        result: list[HandoffRecoveryStrategy] = []
        operation_safe = bool(
            conclusions
            & {
                HandoffSafeConclusion.OPERATION_ABSENT,
                HandoffSafeConclusion.OPERATION_FAILED,
            }
        )
        workspace_safe = bool(
            inspection.workspace_inspection is not None
            and inspection.workspace_inspection.outcome
            in {
                HandoffInspectionOutcome.ABSENT,
                HandoffInspectionOutcome.FAILED,
            }
        )

        if phase in _DERIVED_RETRY_PHASES and (operation_safe or workspace_safe):
            result.append(HandoffRecoveryStrategy.RETRY_CURRENT_PHASE)
        elif (
            phase in _CANONICAL_RETRY_PHASES
            and operation_safe
            and transition.status is not HandoffTransitionStatus.BLOCKED_UNKNOWN
        ):
            # A canonical UNKNOWN row is never reset. A known BLOCKED/FAILED row
            # may be retried only after the operation boundary says no side effect.
            result.append(HandoffRecoveryStrategy.RETRY_CURRENT_PHASE)

        if (
            phase is HandoffPhase.STOP_SOURCE
            and HandoffSafeConclusion.SOURCE_STOPPED in conclusions
        ):
            result.append(HandoffRecoveryStrategy.CONFIRM_SOURCE_STOPPED)

        target_absent = HandoffSafeConclusion.TARGET_ABSENT in conclusions
        target_active = HandoffSafeConclusion.TARGET_ACTIVE in conclusions
        if phase in _TARGET_REPLACEMENT_PHASES and target_absent:
            result.extend(
                (
                    HandoffRecoveryStrategy.CONFIRM_TARGET_ABSENT,
                    HandoffRecoveryStrategy.RETRY_SAME_TARGET,
                    HandoffRecoveryStrategy.SELECT_NEW_TARGET,
                    HandoffRecoveryStrategy.CONTINUE_WITH_SOURCE_AGENT,
                )
            )
        if (
            phase
            in {
                HandoffPhase.START_TARGET_SESSION,
                HandoffPhase.ACKNOWLEDGE_CONTEXT,
            }
            and target_active
        ):
            result.append(HandoffRecoveryStrategy.CONFIRM_TARGET_ACTIVE)

        return tuple(dict.fromkeys(result))

    def persist_operation_inspection(
        self,
        *,
        transition: HandoffTransition,
        inspection: HandoffRecoveryInspection,
    ) -> None:
        """Persist provider inspection evidence without advancing the Transition."""

        _require_legacy_transition(transition)
        _validate_inspection(transition, inspection)
        operation = _matching_operation(inspection, GatewayOperationType.START_SESSION)
        if operation is None:
            operation = next(iter(inspection.operation_inspections), None)
        if operation is None:
            raise HandoffRecoveryError("handoff operation inspection is missing")
        spec = _operation_spec(transition)
        if spec is None or operation.operation_id != spec.operation_id:
            raise HandoffRecoveryError("handoff operation inspection identity changed")
        with self._uow_factory() as uow:
            current_transition = uow.handoff_transitions.get(
                transition.id, transition.workspace_id, for_update=True
            )
            if current_transition is None:
                raise HandoffRecoveryError("handoff transition was not found")
            _require_legacy_transition(current_transition)
            current = uow.gateway_operations.get_by_operation_id(
                operation.operation_id,
                transition.workspace_id,
                for_update=True,
            )
            if current is None:
                raise HandoffRecoveryError("handoff operation was not found")
            previous_version = current.version
            current.record_handoff_inspection(outcome=operation.outcome.value)
            uow.gateway_operations.save(current, previous_version)

    def apply_decision(
        self,
        *,
        transition: HandoffTransition,
        decision: HandoffRecoveryDecision,
        now: datetime,
    ) -> HandoffRecoveryResult:
        """Apply one append-only, expected-version recovery decision."""

        _aware(now, "handoff recovery decision timestamp")
        _require_legacy_transition(transition)
        inspection = self.inspect_unknown(transition=transition, now=now)
        with self._uow_factory() as uow:
            return self.apply_decision_in_uow(
                uow,
                transition=transition,
                decision=decision,
                inspection=inspection,
                now=now,
            )

    def apply_decision_in_uow(
        self,
        uow: ControlPlaneUnitOfWork,
        *,
        transition: HandoffTransition,
        decision: HandoffRecoveryDecision,
        inspection: HandoffRecoveryInspection,
        now: datetime,
    ) -> HandoffRecoveryResult:
        """Apply a preflighted decision inside the caller's command transaction."""

        _aware(now, "handoff recovery decision timestamp")
        _require_legacy_transition(transition)
        if decision.source_channel is not HandoffSourceChannel.WEB:
            raise HandoffRecoveryError("handoff recovery source channel must be web")
        if decision.handoff_transition_id != transition.id:
            raise HandoffRecoveryError("recovery decision transition identity changed")
        _validate_inspection(transition, inspection)
        if not set(decision.evidence_refs).issubset(set(inspection.evidence_refs)):
            raise HandoffRecoveryError(
                "recovery decision evidence is not from the current inspection"
            )

        current = uow.handoff_transitions.get(
            transition.id,
            transition.workspace_id,
            for_update=True,
        )
        if current is None:
            raise HandoffRecoveryError("handoff transition was not found")
        _require_legacy_transition(current)
        if current.version != decision.expected_transition_version:
            raise VersionConflict(decision.expected_transition_version, current.version)
        if current.version != transition.version:
            raise VersionConflict(transition.version, current.version)

        existing = uow.handoff_recovery_decisions.get(
            decision.id,
            transition.workspace_id,
        )
        if existing is not None:
            expected_existing = replace(
                decision,
                resulting_operation_refs=existing.resulting_operation_refs,
            )
            if existing != expected_existing:
                raise HandoffRecoveryError("handoff recovery decision is immutable")
            return _result_from_transition(current, existing.resulting_operation_refs)

        allowed = self.available_strategies(
            transition=current,
            inspection=inspection,
        )
        if decision.strategy not in allowed:
            raise HandoffRecoveryError(
                "recovery strategy is not supported by current evidence",
                details={"available_strategies": [item.value for item in allowed]},
            )

        persisted = self._load_state_from_uow(uow, current)
        previous_version = current.version
        resulting_refs = self._apply_strategy(
            uow,
            current,
            persisted,
            inspection=inspection,
            strategy=decision.strategy,
            decision=decision,
            now=now,
        )
        stored_decision = replace(
            decision,
            resulting_operation_refs=resulting_refs,
        )
        uow.handoff_recovery_decisions.add_append_only(
            stored_decision,
            current.workspace_id,
        )
        uow.handoff_transitions.save_recovery(current, previous_version)
        return _result_from_transition(current, resulting_refs)

    def _apply_strategy(
        self,
        uow: ControlPlaneUnitOfWork,
        transition: HandoffTransition,
        state: _PersistedState,
        *,
        inspection: HandoffRecoveryInspection,
        strategy: HandoffRecoveryStrategy,
        decision: HandoffRecoveryDecision,
        now: datetime,
    ) -> tuple[UUID, ...]:
        target_worker_id = _require_legacy_transition(transition)
        if strategy is HandoffRecoveryStrategy.RETRY_CURRENT_PHASE:
            if (
                transition.phase in _WORKSPACE_PHASES
                and state.workspace_transfer is not None
                and state.workspace_transfer.target_manifest_id is not None
            ):
                raise HandoffRecoveryError(
                    "workspace retry requires no target manifest side effect"
                )
            derived = transition.phase in _DERIVED_RETRY_PHASES
            transition.prepare_recovery_retry(
                now=now,
                new_logical_attempt=derived,
            )
            operation_id = _operation_spec(transition)
            return (operation_id.operation_id,) if operation_id is not None else ()

        if strategy is HandoffRecoveryStrategy.CONFIRM_SOURCE_STOPPED:
            operation = _matching_operation(
                inspection, GatewayOperationType.STOP_SESSION
            )
            if (
                operation is None
                or operation.outcome is not HandoffInspectionOutcome.SUCCEEDED
            ):
                raise HandoffRecoveryError("source stop success evidence is required")
            sequence = operation.accepted_sequence
            if sequence is None:
                sequence = transition.source_checkpoint_sequence
            if sequence is None:
                raise HandoffRecoveryError("source stop sequence is missing")
            if transition.source_stop_sequence is None:
                transition.record_stop(
                    sequence=sequence,
                    facts={
                        "operation_id": str(operation.operation_id),
                        "source_session_id": str(transition.source_session_id),
                        "accepted_sequence": sequence,
                        "stopped": True,
                        "produced_at": now.isoformat(),
                    },
                )
            elif transition.source_stop_sequence != sequence:
                raise HandoffRecoveryError("source stop sequence conflicts")
            transition.recover_to_next_phase(now=now)
            return (operation.operation_id,)

        if strategy is HandoffRecoveryStrategy.CONFIRM_TARGET_ACTIVE:
            transition.recover_to_next_phase(now=now)
            operation = _matching_operation(
                inspection,
                GatewayOperationType.HANDOFF_ENABLE_WRITER,
            )
            if operation is None:
                operation = _matching_operation(
                    inspection,
                    GatewayOperationType.START_SESSION,
                )
            return (operation.operation_id,) if operation is not None else ()

        if strategy is HandoffRecoveryStrategy.CONFIRM_TARGET_ABSENT:
            self._retire_target_lineage(uow, state, now=now)
            transition.begin_recovery_attempt(
                target_worker_id=target_worker_id,
                target_runtime_snapshot=transition.target_runtime_snapshot,
                now=now,
            )
            return ()

        if strategy in {
            HandoffRecoveryStrategy.RETRY_SAME_TARGET,
            HandoffRecoveryStrategy.SELECT_NEW_TARGET,
            HandoffRecoveryStrategy.CONTINUE_WITH_SOURCE_AGENT,
        }:
            self._retire_target_lineage(uow, state, now=now)
            target_snapshot = transition.target_runtime_snapshot
            if strategy is HandoffRecoveryStrategy.RETRY_SAME_TARGET:
                target_worker_id, target_snapshot = self._resolve_replacement_target(
                    uow,
                    transition,
                    transition.target_runtime_snapshot,
                    now=now,
                    source_worker=False,
                )
            elif strategy is HandoffRecoveryStrategy.SELECT_NEW_TARGET:
                target_worker_id, target_snapshot = self._resolve_replacement_target(
                    uow,
                    transition,
                    decision.replacement_target_snapshot,
                    now=now,
                    source_worker=False,
                )
            elif strategy is HandoffRecoveryStrategy.CONTINUE_WITH_SOURCE_AGENT:
                target_worker_id, target_snapshot = self._resolve_replacement_target(
                    uow,
                    transition,
                    None,
                    now=now,
                    source_worker=True,
                )
            transition.begin_recovery_attempt(
                target_worker_id=target_worker_id,
                target_runtime_snapshot=target_snapshot,
                now=now,
            )
            return ()

        raise HandoffRecoveryError("unsupported handoff recovery strategy")

    def _inspect_gateway_operation(
        self,
        state: _PersistedState,
        spec: _OperationSpec,
        *,
        now: datetime,
    ) -> HandoffOperationInspection:
        if not spec.identity_matches_blocked_id:
            return _unknown_operation_inspection(state.transition, spec, now=now)
        if self._gateway is not None:
            try:
                inspected = self._gateway.inspect_handoff_operation(
                    workspace_id=state.transition.workspace_id,
                    transition_id=state.transition.id,
                    operation_id=spec.operation_id,
                    operation_type=spec.operation_type,
                    now=now,
                )
                return _normalize_operation_inspection(
                    inspected,
                    state.transition,
                    spec,
                    now=now,
                )
            except Exception:  # noqa: BLE001
                return _unknown_operation_inspection(state.transition, spec, now=now)
        if state.gateway_operation is not None:
            return _operation_inspection_from_row(state.gateway_operation, now=now)
        return _unknown_operation_inspection(state.transition, spec, now=now)

    def _inspect_workspace_transfer(
        self,
        state: _PersistedState,
        *,
        now: datetime,
    ) -> HandoffWorkspaceInspection:
        operation_id = handoff_step_operation_id(
            state.transition.id,
            HandoffPhase.TRANSFER_WORKSPACE,
            logical_attempt=state.transition.logical_attempt,
            action="transfer_workspace",
        )
        if self._workspace_handoff is not None:
            try:
                inspected = self._workspace_handoff.inspect(
                    transition_id=state.transition.id,
                    workspace_id=state.transition.workspace_id,
                    logical_attempt=state.transition.logical_attempt,
                    operation_id=operation_id,
                    now=now,
                )
                return _normalize_workspace_inspection(
                    inspected,
                    transition=state.transition,
                    operation_id=operation_id,
                    now=now,
                )
            except Exception:  # noqa: BLE001
                return _unknown_workspace_inspection(
                    state.transition,
                    operation_id,
                    now=now,
                )
        if state.workspace_transfer is not None:
            return _workspace_inspection_from_transfer(
                state.workspace_transfer, now=now
            )
        return _unknown_workspace_inspection(
            state.transition,
            operation_id,
            now=now,
        )

    def _load_state(self, transition: HandoffTransition) -> _PersistedState:
        with self._uow_factory() as uow:
            current = uow.handoff_transitions.get(
                transition.id,
                transition.workspace_id,
            )
            if current is None:
                raise HandoffRecoveryError("handoff transition was not found")
            return self._load_state_from_uow(uow, current)

    @staticmethod
    def _load_state_from_uow(
        uow: ControlPlaneUnitOfWork,
        transition: HandoffTransition,
    ) -> _PersistedState:
        _require_legacy_transition(transition)
        source_execution = uow.executions.get(
            transition.source_execution_id,
            transition.workspace_id,
        )
        source_session = uow.sessions.get_any(
            transition.source_session_id,
            transition.workspace_id,
        )
        target_execution = None
        target_session = None
        target_manifest = None
        target_lease = None
        if transition.target_execution_id is not None:
            target_execution = uow.executions.get(
                transition.target_execution_id,
                transition.workspace_id,
            )
            if target_execution is not None:
                active_sessions = cast(
                    Callable[[UUID, UUID], tuple[object, ...]] | None,
                    getattr(uow.sessions, "list_active_for_execution", None),
                )
                if callable(active_sessions):
                    sessions = active_sessions(
                        target_execution.id,
                        transition.workspace_id,
                    )
                    if sessions:
                        target_session = sessions[0]
                if transition.target_session_id is not None:
                    target_session = uow.sessions.get_any(
                        transition.target_session_id,
                        transition.workspace_id,
                    )
                target_lease = uow.leases.get_active_for_execution(
                    target_execution.id,
                    transition.workspace_id,
                )
        if transition.target_workspace_manifest_id is not None:
            target_manifest = uow.workspaces.get(
                transition.target_workspace_manifest_id,
                transition.workspace_id,
            )
        operation = None
        spec = _operation_spec(transition)
        if spec is not None:
            operation = uow.gateway_operations.get_by_operation_id(
                spec.operation_id,
                transition.workspace_id,
            )
        transfer_repository = getattr(
            uow,
            "handoff_workspace_transfers",
            getattr(uow, "workspace_transfers", None),
        )
        transfer = None
        if (
            transfer_repository is not None
            and transition.phase is HandoffPhase.TRANSFER_WORKSPACE
        ):
            operation_id = handoff_step_operation_id(
                transition.id,
                HandoffPhase.TRANSFER_WORKSPACE,
                logical_attempt=transition.logical_attempt,
                action="transfer_workspace",
            )
            transfer = transfer_repository.get_by_operation(
                operation_id,
                transition.workspace_id,
            )
        return _PersistedState(
            transition=transition,
            source_execution=source_execution,
            source_session_state=_session_state(source_session),
            target_execution=target_execution,
            target_session_id=(
                getattr(target_session, "id", None)
                if target_session is not None
                else None
            ),
            target_session_identity_matches=(
                target_execution is not None
                and target_session is not None
                and getattr(target_session, "workspace_id", None)
                == transition.workspace_id
                and getattr(target_session, "execution_id", None) == target_execution.id
                and getattr(target_session, "worker_id", None)
                == transition.target_worker_id
            ),
            target_session_state=(
                _session_state(target_session) if target_execution is not None else None
            ),
            target_manifest_state=_manifest_state(target_manifest),
            target_lease=target_lease,
            gateway_operation=operation,
            workspace_transfer=transfer,
        )

    def _retire_target_lineage(
        self,
        uow: ControlPlaneUnitOfWork,
        state: _PersistedState,
        *,
        now: datetime,
    ) -> None:
        execution_requests = getattr(uow, "execution_requests", None)
        if execution_requests is not None:
            target_request = execution_requests.get_by_handoff_transition(
                state.transition.id,
                state.transition.workspace_id,
                for_update=True,
            )
            if target_request is not None:
                previous_request_version = target_request.version
                target_request.retire_handoff_target(now=now)
                execution_requests.save(target_request, previous_request_version)

        target = state.target_execution
        if target is None:
            return
        lease = None
        if target.active_lease_id is not None:
            lease = uow.leases.get(
                target.active_lease_id,
                target.workspace_id,
                for_update=True,
            )
            if lease is not None and lease.status is LeaseStatus.ACTIVE:
                previous_lease_version = lease.version
                lease.revoke(now=now)
                uow.leases.save(lease, previous_lease_version)
        if target.status in {
            ExecutionStatus.QUEUED,
            ExecutionStatus.LEASED,
            ExecutionStatus.RUNNING,
        }:
            previous_execution_version = target.version
            target.cancel(now=now)
            uow.executions.save(target, previous_execution_version)
            worker_id = target.assigned_worker_id
            if worker_id is not None:
                worker = uow.workers.get(
                    worker_id,
                    target.workspace_id,
                    for_update=True,
                )
                if worker is not None and worker.current_load > 0:
                    previous_worker_version = worker.version
                    worker.release()
                    uow.workers.save(worker, previous_worker_version)
        if state.transition.target_workspace_manifest_id is not None:
            manifest = uow.workspaces.get(
                state.transition.target_workspace_manifest_id,
                state.transition.workspace_id,
                for_update=True,
            )
            if manifest is not None and manifest.lifecycle is WorkspaceLifecycle.READY:
                previous_manifest_version = manifest.version
                manifest.quarantine()
                uow.workspaces.save(manifest, previous_manifest_version)

    def _resolve_replacement_target(
        self,
        uow: ControlPlaneUnitOfWork,
        transition: HandoffTransition,
        requested_snapshot: Mapping[str, object] | None,
        *,
        now: datetime,
        source_worker: bool,
    ) -> tuple[UUID, Mapping[str, object]]:
        proposal = uow.handoff_proposals.get(
            transition.proposal_id,
            transition.workspace_id,
        )
        if proposal is None:
            raise HandoffRecoveryError("handoff proposal was not found")
        source_worker_id = proposal.payload.source_worker_id
        requested_worker_id = (
            source_worker_id
            if source_worker
            else _requested_worker_id(requested_snapshot)
        )
        if not source_worker and requested_worker_id == source_worker_id:
            raise HandoffRecoveryError(
                "replacement target must differ from source worker"
            )
        worker = uow.workers.get(
            requested_worker_id,
            transition.workspace_id,
            for_update=True,
        )
        if worker is None:
            raise HandoffRecoveryError("replacement target worker was not found")
        source_execution = uow.executions.get(
            transition.source_execution_id,
            transition.workspace_id,
            for_update=True,
        )
        if source_execution is None:
            raise HandoffRecoveryError("handoff source execution was not found")
        if worker.workspace_id != transition.workspace_id:
            raise HandoffRecoveryError("replacement target worker workspace changed")
        if (
            worker.status not in SCHEDULABLE_WORKER_STATUSES
            or worker.desired_state is not WorkerDesiredState.ACTIVE
            or not worker.is_fresh(now=now, timeout=self._heartbeat_timeout)
            or worker.current_load >= worker.capacity
            or not source_execution.capability_requirements.issubset(
                worker.capabilities
            )
            or worker.adapter_type in source_execution.adapter_forbidden
        ):
            raise HandoffRecoveryError(
                "replacement target worker is no longer eligible"
            )
        resolved = cast(WorkerRuntimeResolver, self._runtime_resolver).lock_and_resolve(
            uow,
            worker,
            source_execution,
        )
        if not isinstance(resolved, ResolvedWorkerRuntime):
            raise HandoffRecoveryError("replacement target runtime is unavailable")
        profile = resolved.capability_snapshot
        workspace_profile = resolved.workspace_capability_snapshot
        if (
            profile.handoff_checkpoint_mode is HandoffCheckpointMode.UNSUPPORTED
            or 1 not in profile.handoff_session_context_versions
            or 1 not in profile.handoff_writer_gate_versions
            or not profile.handoff_inspection
            or workspace_profile is None
            or 1 not in workspace_profile.workspace_handoff_versions
            or not workspace_profile.handoff_inspection
        ):
            raise HandoffRecoveryError(
                "replacement target runtime cannot accept handoff"
            )
        snapshot = runtime_snapshot_for_handoff(resolved)
        source_snapshot = proposal.payload.source_runtime_snapshot
        for key in (
            "workspace_module_instance_id",
            "workspace_module_config_version",
            "workspace_trust_boundary_hash",
            "workspace_repository_scope_hash",
            "workspace_path_scope_hash",
        ):
            if snapshot.get(key) != source_snapshot.get(key):
                raise HandoffRecoveryError("replacement target workspace facts changed")
        if requested_snapshot is not None:
            requested = dict(requested_snapshot)
            if requested.get("worker_id") != str(requested_worker_id):
                raise HandoffRecoveryError(
                    "replacement target snapshot worker identity changed"
                )
            if len(requested) > 1 and _plain_json(requested) != _plain_json(snapshot):
                raise HandoffRecoveryError(
                    "replacement target runtime snapshot is stale"
                )
        return requested_worker_id, snapshot


def _operation_spec(transition: HandoffTransition) -> _OperationSpec | None:
    operation_id: UUID | None = None
    operation_type: GatewayOperationType | None = None
    execution_id = transition.source_execution_id
    if transition.phase is HandoffPhase.CHECKPOINT_SOURCE:
        operation_id = handoff_step_operation_id(
            transition.id,
            transition.phase,
            logical_attempt=transition.logical_attempt,
            action="checkpoint",
        )
        operation_type = GatewayOperationType.HANDOFF_CHECKPOINT
    elif transition.phase is HandoffPhase.STOP_SOURCE:
        operation_id = gateway_operation_id(
            transition.source_execution_id,
            GatewayOperationType.STOP_SESSION,
        )
        operation_type = GatewayOperationType.STOP_SESSION
    elif transition.phase is HandoffPhase.START_TARGET_SESSION:
        if transition.target_execution_id is None:
            return None
        execution_id = transition.target_execution_id
        operation_id = gateway_operation_id(
            transition.target_execution_id,
            GatewayOperationType.START_SESSION,
        )
        operation_type = GatewayOperationType.START_SESSION
    elif transition.phase is HandoffPhase.ACKNOWLEDGE_CONTEXT:
        operation_id = handoff_step_operation_id(
            transition.id,
            transition.phase,
            logical_attempt=transition.logical_attempt,
            action="enable_writer",
        )
        operation_type = GatewayOperationType.HANDOFF_ENABLE_WRITER
    if operation_id is None or operation_type is None:
        return None
    return _OperationSpec(
        operation_id=operation_id,
        operation_type=operation_type,
        execution_id=execution_id,
        identity_matches_blocked_id=(
            transition.blocked_operation_id in (None, operation_id)
        ),
    )


def _operation_inspection_from_row(
    operation: GatewayOperation,
    *,
    now: datetime,
) -> HandoffOperationInspection:
    durable_inspection = handoff_inspection_outcome_from_result_ref(
        operation.result_ref
    )
    outcome = {
        GatewayOperationStatus.REQUESTED: HandoffInspectionOutcome.IN_PROGRESS,
        GatewayOperationStatus.RUNNING: HandoffInspectionOutcome.IN_PROGRESS,
        GatewayOperationStatus.SUCCEEDED: HandoffInspectionOutcome.SUCCEEDED,
        GatewayOperationStatus.FAILED: HandoffInspectionOutcome.FAILED,
        GatewayOperationStatus.UNKNOWN: HandoffInspectionOutcome.UNKNOWN,
    }[operation.status]
    if durable_inspection is not None:
        outcome = HandoffInspectionOutcome(durable_inspection)
    return HandoffOperationInspection(
        operation_id=operation.operation_id,
        operation_type=operation.operation_type,
        outcome=outcome,
        execution_id=operation.execution_id,
        session_id=operation.session_id,
        accepted_sequence=_sequence_from_result_ref(operation.result_ref),
        context_binding=None,
        observed_at=now,
        evidence_refs=(
            f"{_INSPECTION_REFERENCE_PREFIX}gateway/{operation.operation_id}/v{operation.version}",
        ),
    )


def _normalize_operation_inspection(
    inspected: HandoffOperationInspection,
    transition: HandoffTransition,
    spec: _OperationSpec,
    *,
    now: datetime,
) -> HandoffOperationInspection:
    if (
        not isinstance(inspected, HandoffOperationInspection)
        or inspected.operation_id != spec.operation_id
        or inspected.operation_type is not spec.operation_type
        or inspected.execution_id not in (None, spec.execution_id)
    ):
        return _unknown_operation_inspection(transition, spec, now=now)
    return HandoffOperationInspection(
        operation_id=spec.operation_id,
        operation_type=spec.operation_type,
        outcome=inspected.outcome,
        execution_id=spec.execution_id,
        session_id=inspected.session_id,
        accepted_sequence=inspected.accepted_sequence,
        context_binding=None,
        observed_at=now,
        evidence_refs=(
            f"{_INSPECTION_REFERENCE_PREFIX}gateway/{spec.operation_id}/observed",
        ),
    )


def _unknown_operation_inspection(
    transition: HandoffTransition,
    spec: _OperationSpec,
    *,
    now: datetime,
) -> HandoffOperationInspection:
    return HandoffOperationInspection(
        operation_id=spec.operation_id,
        operation_type=spec.operation_type,
        outcome=HandoffInspectionOutcome.UNKNOWN,
        execution_id=spec.execution_id,
        session_id=(
            transition.source_session_id
            if spec.execution_id == transition.source_execution_id
            else transition.target_session_id
        ),
        accepted_sequence=None,
        context_binding=None,
        observed_at=now,
        evidence_refs=(
            f"{_INSPECTION_REFERENCE_PREFIX}gateway/{spec.operation_id}/unknown",
        ),
    )


def _workspace_inspection_from_transfer(
    transfer: HandoffWorkspaceTransfer,
    *,
    now: datetime,
) -> HandoffWorkspaceInspection:
    outcome = {
        HandoffWorkspaceTransferStatus.REQUESTED: HandoffInspectionOutcome.IN_PROGRESS,
        HandoffWorkspaceTransferStatus.SUCCEEDED: HandoffInspectionOutcome.SUCCEEDED,
        HandoffWorkspaceTransferStatus.FAILED: HandoffInspectionOutcome.FAILED,
        HandoffWorkspaceTransferStatus.UNKNOWN: HandoffInspectionOutcome.UNKNOWN,
    }[transfer.status]
    return HandoffWorkspaceInspection(
        transition_id=transfer.transition_id,
        logical_attempt=transfer.logical_attempt,
        operation_id=transfer.operation_id,
        outcome=outcome,
        source_manifest_id=transfer.source_manifest_id,
        target_manifest_id=transfer.target_manifest_id,
        source_lifecycle=None,
        target_lifecycle=None,
        observed_at=now,
        evidence_refs=(
            f"{_INSPECTION_REFERENCE_PREFIX}workspace/{transfer.operation_id}/v{transfer.version}",
        ),
    )


def _normalize_workspace_inspection(
    inspected: HandoffWorkspaceInspection,
    *,
    transition: HandoffTransition,
    operation_id: UUID,
    now: datetime,
) -> HandoffWorkspaceInspection:
    if (
        not isinstance(inspected, HandoffWorkspaceInspection)
        or inspected.transition_id != transition.id
        or inspected.logical_attempt != transition.logical_attempt
        or inspected.operation_id != operation_id
    ):
        return _unknown_workspace_inspection(transition, operation_id, now=now)
    return HandoffWorkspaceInspection(
        transition_id=transition.id,
        logical_attempt=transition.logical_attempt,
        operation_id=operation_id,
        outcome=inspected.outcome,
        source_manifest_id=inspected.source_manifest_id,
        target_manifest_id=inspected.target_manifest_id,
        source_lifecycle=inspected.source_lifecycle,
        target_lifecycle=inspected.target_lifecycle,
        observed_at=now,
        evidence_refs=(
            f"{_INSPECTION_REFERENCE_PREFIX}workspace/{operation_id}/observed",
        ),
    )


def _unknown_workspace_inspection(
    transition: HandoffTransition,
    operation_id: UUID,
    *,
    now: datetime,
) -> HandoffWorkspaceInspection:
    return HandoffWorkspaceInspection(
        transition_id=transition.id,
        logical_attempt=transition.logical_attempt,
        operation_id=operation_id,
        outcome=HandoffInspectionOutcome.UNKNOWN,
        source_manifest_id=transition.source_workspace_manifest_id,
        target_manifest_id=transition.target_workspace_manifest_id,
        source_lifecycle=None,
        target_lifecycle=None,
        observed_at=now,
        evidence_refs=(
            f"{_INSPECTION_REFERENCE_PREFIX}workspace/{operation_id}/unknown",
        ),
    )


def _safe_conclusions(
    state: _PersistedState,
    *,
    operations: tuple[HandoffOperationInspection, ...],
    workspace_inspection: HandoffWorkspaceInspection | None,
) -> tuple[HandoffSafeConclusion, ...]:
    conclusions: list[HandoffSafeConclusion] = []
    for operation in operations:
        if operation.outcome is HandoffInspectionOutcome.SUCCEEDED:
            conclusions.append(HandoffSafeConclusion.OPERATION_SUCCEEDED)
        elif operation.outcome is HandoffInspectionOutcome.FAILED:
            conclusions.append(HandoffSafeConclusion.OPERATION_FAILED)
        elif operation.outcome is HandoffInspectionOutcome.ABSENT:
            conclusions.append(HandoffSafeConclusion.OPERATION_ABSENT)
    if workspace_inspection is not None:
        if workspace_inspection.outcome is HandoffInspectionOutcome.SUCCEEDED:
            conclusions.append(HandoffSafeConclusion.OPERATION_SUCCEEDED)
        elif workspace_inspection.outcome is HandoffInspectionOutcome.FAILED:
            conclusions.append(HandoffSafeConclusion.OPERATION_FAILED)
        elif workspace_inspection.outcome is HandoffInspectionOutcome.ABSENT:
            conclusions.append(HandoffSafeConclusion.OPERATION_ABSENT)

    if _source_stopped(state, operations):
        conclusions.append(HandoffSafeConclusion.SOURCE_STOPPED)
    if _target_absent(state, operations):
        conclusions.append(HandoffSafeConclusion.TARGET_ABSENT)
    if _target_active(state, operations):
        conclusions.append(HandoffSafeConclusion.TARGET_ACTIVE)
    return tuple(dict.fromkeys(conclusions))


def _source_stopped(
    state: _PersistedState,
    operations: tuple[HandoffOperationInspection, ...],
) -> bool:
    if state.source_session_state not in {
        ExecutionSessionStatus.CLOSED.value,
        ExecutionSessionStatus.LOST.value,
    }:
        return False
    if (
        state.source_execution is not None
        and state.source_execution.active_lease_id is not None
    ):
        return False
    stop = _matching_operation(operations, GatewayOperationType.STOP_SESSION)
    if state.transition.source_stop_sequence is not None:
        return (
            state.transition.source_stop_sequence
            == state.transition.source_checkpoint_sequence
        )
    return bool(
        stop is not None
        and stop.outcome is HandoffInspectionOutcome.SUCCEEDED
        and stop.accepted_sequence
        in {None, state.transition.source_checkpoint_sequence}
        and state.transition.source_checkpoint_sequence is not None
    )


def _target_absent(
    state: _PersistedState,
    operations: tuple[HandoffOperationInspection, ...],
) -> bool:
    if state.transition.phase is not HandoffPhase.START_TARGET_SESSION:
        return False
    if state.target_session_state in {
        ExecutionSessionStatus.ACTIVE.value,
    }:
        return False
    if state.target_manifest_state == WorkspaceLifecycle.ACTIVE.value:
        return False
    execution = state.target_execution
    if execution is not None and execution.status not in {
        ExecutionStatus.QUEUED,
        ExecutionStatus.LEASED,
        ExecutionStatus.CANCELED,
        ExecutionStatus.FAILED,
        ExecutionStatus.LOST,
    }:
        return False
    start = _matching_operation(operations, GatewayOperationType.START_SESSION)
    return bool(
        start is not None
        and start.outcome
        in {
            HandoffInspectionOutcome.ABSENT,
            HandoffInspectionOutcome.FAILED,
        }
    )


def _target_active(
    state: _PersistedState,
    operations: tuple[HandoffOperationInspection, ...],
) -> bool:
    if state.transition.phase not in {
        HandoffPhase.START_TARGET_SESSION,
        HandoffPhase.ACKNOWLEDGE_CONTEXT,
    }:
        return False
    if state.target_session_state != ExecutionSessionStatus.ACTIVE.value:
        return False
    if (
        state.target_execution is None
        or state.target_execution.status is not ExecutionStatus.RUNNING
        or not state.target_session_identity_matches
        or state.target_session_id != state.transition.target_session_id
        or state.transition.target_session_id is None
    ):
        return False
    expected_manifest_state = (
        WorkspaceLifecycle.READY.value
        if state.transition.phase is HandoffPhase.START_TARGET_SESSION
        else WorkspaceLifecycle.ACTIVE.value
    )
    if state.target_manifest_state != expected_manifest_state:
        return False
    enable = _matching_operation(operations, GatewayOperationType.HANDOFF_ENABLE_WRITER)
    start = _matching_operation(operations, GatewayOperationType.START_SESSION)
    if state.transition.phase is HandoffPhase.ACKNOWLEDGE_CONTEXT:
        if not _operation_session_matches_target(enable, state.transition):
            return False
        return bool(
            enable is not None and enable.outcome is HandoffInspectionOutcome.SUCCEEDED
        )
    if not _operation_session_matches_target(start, state.transition):
        return False
    return bool(
        start is not None and start.outcome is HandoffInspectionOutcome.SUCCEEDED
    )


def _operation_session_matches_target(
    operation: HandoffOperationInspection | None,
    transition: HandoffTransition,
) -> bool:
    return operation is not None and operation.session_id in {
        None,
        transition.target_session_id,
    }


def _matching_operation(
    inspection: HandoffRecoveryInspection | tuple[HandoffOperationInspection, ...],
    operation_type: GatewayOperationType,
) -> HandoffOperationInspection | None:
    operations = (
        inspection.operation_inspections
        if isinstance(inspection, HandoffRecoveryInspection)
        else inspection
    )
    return next(
        (item for item in operations if item.operation_type is operation_type),
        None,
    )


def _inspection_refs(
    transition: HandoffTransition,
    *,
    operations: tuple[HandoffOperationInspection, ...],
    workspace_inspection: HandoffWorkspaceInspection | None,
) -> tuple[str, ...]:
    refs = [
        f"{_INSPECTION_REFERENCE_PREFIX}transition/{transition.id}/v{transition.version}"
    ]
    refs.extend(ref for operation in operations for ref in operation.evidence_refs)
    if workspace_inspection is not None:
        refs.extend(workspace_inspection.evidence_refs)
    return tuple(dict.fromkeys(refs))[:16]


def _require_legacy_transition(transition: HandoffTransition) -> UUID:
    if not isinstance(transition, HandoffTransition):
        raise HandoffRecoveryError("handoff transition is invalid")
    if transition.handoff_schema_version != 1 or transition.native_payload is not None:
        raise HandoffRecoveryError("native handoff does not support legacy recovery")
    target_worker_id = transition.target_worker_id
    if not isinstance(target_worker_id, UUID):
        raise HandoffRecoveryError("legacy handoff target worker is missing")
    return target_worker_id


def _validate_inspection(
    transition: HandoffTransition,
    inspection: HandoffRecoveryInspection,
) -> None:
    if (
        not isinstance(inspection, HandoffRecoveryInspection)
        or inspection.transition_id != transition.id
        or inspection.transition_version != transition.version
        or inspection.logical_attempt != transition.logical_attempt
        or inspection.phase is not transition.phase
        or inspection.writer_state is not transition.writer_state
    ):
        raise HandoffRecoveryError("handoff recovery inspection is stale")


def _result_from_transition(
    transition: HandoffTransition,
    operation_refs: tuple[UUID, ...],
) -> HandoffRecoveryResult:
    return HandoffRecoveryResult(
        transition_id=transition.id,
        transition_version=transition.version,
        status=transition.status,
        phase=transition.phase,
        writer_state=transition.writer_state,
        logical_attempt=transition.logical_attempt,
        resulting_operation_refs=operation_refs,
    )


def _session_state(session: object | None) -> str:
    if session is None:
        return "missing"
    status = getattr(session, "status", None)
    return str(getattr(status, "value", status))


def _manifest_state(manifest: object | None) -> str | None:
    if manifest is None:
        return None
    lifecycle = getattr(manifest, "lifecycle", None)
    return str(getattr(lifecycle, "value", lifecycle))


def _sequence_from_result_ref(result_ref: str | None) -> int | None:
    if not isinstance(result_ref, str) or not result_ref.startswith("sequence:"):
        return None
    try:
        value = int(result_ref.split(":", 1)[1])
    except ValueError:
        return None
    return value if value >= 0 else None


def _requested_worker_id(snapshot: Mapping[str, object] | None) -> UUID:
    if snapshot is None:
        raise HandoffRecoveryError("replacement target worker is required")
    value = snapshot.get("worker_id")
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise HandoffRecoveryError("replacement target worker is invalid") from exc


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    if isinstance(value, UUID):
        return str(value)
    return value


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise HandoffRecoveryError(f"{name} must be timezone-aware")
