from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from sidera_control_plane.application.handoff_candidates import (
    runtime_snapshot_for_handoff,
)
from sidera_control_plane.application.handoff_gate import (
    HandoffGateOperation,
    HandoffGateResult,
)
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.application.worker_runtime import ResolvedWorkerRuntime
from sidera_control_plane.domain.errors import DomainError, ValidationError
from sidera_control_plane.domain.execution import (
    TERMINAL_EXECUTION_STATUSES,
    Execution,
    ExecutionStatus,
)
from sidera_control_plane.domain.execution_requests import (
    ExecutionRequest,
    ExecutionRequestStatus,
)
from sidera_control_plane.domain.handoffs import (
    HandoffPhase,
    HandoffProposal,
    HandoffProposalStatus,
    HandoffTransition,
    HandoffTransitionStatus,
    HandoffWriterState,
)
from sidera_control_plane.domain.worker import Worker
from sidera_control_plane.domain.workspace import (
    ExecutionWorkspaceManifest,
    WorkspaceLifecycle,
)

_TARGET_REQUEST_NAMESPACE = NAMESPACE_URL
_TARGET_EXECUTION_NAMESPACE = NAMESPACE_URL


class HandoffTargetError(DomainError):
    """A handoff target is missing or no longer matches its frozen facts."""

    code = "handoff_target_stale"


@dataclass(frozen=True, slots=True)
class HandoffTargetIdentity:
    execution_request_id: UUID
    execution_id: UUID
    lineage_id: UUID
    anticipated_fencing_token: int


@dataclass(frozen=True, slots=True)
class HandoffTargetAssignment:
    execution_id: UUID
    lease_id: UUID
    binding_id: UUID
    worker_id: UUID
    fencing_token: int


class HandoffTargetService:
    """Create one held target request and execution for a handoff attempt.

    Target identities are deterministic for a transition attempt. This makes a
    coordinator restart converge on the existing pair instead of producing a
    second request or execution.
    """

    def __init__(
        self,
        uow_factory: Callable[[], ControlPlaneUnitOfWork] | None = None,
    ) -> None:
        self._uow_factory = uow_factory

    def create_target(
        self,
        *,
        transition: HandoffTransition,
        proposal: HandoffProposal,
        source_request: ExecutionRequest,
        source_execution: Execution,
        source_manifest: ExecutionWorkspaceManifest,
        now: datetime,
        uow: ControlPlaneUnitOfWork | None = None,
    ) -> HandoffTargetIdentity:
        """Create one held target request and Execution with source lineage refs."""

        _aware(now, "handoff target timestamp")
        if uow is not None:
            return self._create_in_uow(
                uow,
                transition=transition,
                proposal=proposal,
                source_request=source_request,
                source_execution=source_execution,
                source_manifest=source_manifest,
                now=now,
            )
        if self._uow_factory is None:
            raise HandoffTargetError("target creation requires a unit-of-work factory")
        with self._uow_factory() as target_uow:
            return self._create_in_uow(
                target_uow,
                transition=transition,
                proposal=proposal,
                source_request=source_request,
                source_execution=source_execution,
                source_manifest=source_manifest,
                now=now,
            )

    def _create_in_uow(
        self,
        uow: ControlPlaneUnitOfWork,
        *,
        transition: HandoffTransition,
        proposal: HandoffProposal,
        source_request: ExecutionRequest,
        source_execution: Execution,
        source_manifest: ExecutionWorkspaceManifest,
        now: datetime,
    ) -> HandoffTargetIdentity:
        current_transition = uow.handoff_transitions.get(
            transition.id,
            transition.workspace_id,
            for_update=True,
        )
        if current_transition is None:
            raise HandoffTargetError("handoff transition was not found")
        self._validate_inputs(
            current_transition,
            proposal=proposal,
            source_request=source_request,
            source_execution=source_execution,
            source_manifest=source_manifest,
        )

        stored_source_request = uow.execution_requests.get(
            source_request.id,
            transition.workspace_id,
            for_update=True,
        )
        if stored_source_request is None:
            raise HandoffTargetError("source execution request was not found")
        if stored_source_request != source_request:
            self._validate_source_request(stored_source_request, current_transition)
            source_request = stored_source_request

        stored_source_execution = uow.executions.get(
            source_execution.id,
            transition.workspace_id,
            for_update=True,
        )
        if stored_source_execution is None:
            raise HandoffTargetError("source execution was not found")
        if stored_source_execution != source_execution:
            self._validate_source_execution(stored_source_execution, current_transition)
            source_execution = stored_source_execution

        target_worker = uow.workers.get(
            current_transition.target_worker_id,
            current_transition.workspace_id,
            for_update=True,
        )
        if target_worker is None:
            raise HandoffTargetError("handoff target worker was not found")
        expected_worker_version = proposal.payload.target_worker_version
        if (
            current_transition.logical_attempt == 1
            and target_worker.version != expected_worker_version
        ):
            raise HandoffTargetError("handoff target worker facts are stale")

        request_id, execution_id = _target_ids(
            current_transition.id,
            current_transition.logical_attempt,
        )
        anticipated_fencing_token = source_manifest.fencing_token + 1

        if current_transition.target_execution_id is not None:
            if current_transition.target_execution_id != execution_id:
                raise HandoffTargetError("handoff target execution identity changed")
            target_execution = uow.executions.get(
                execution_id,
                current_transition.workspace_id,
                for_update=True,
            )
            target_request = uow.execution_requests.get(
                request_id,
                current_transition.workspace_id,
                for_update=True,
            )
            if target_execution is None or target_request is None:
                raise HandoffTargetError("handoff target lineage is incomplete")
            self._validate_target_pair(
                target_request,
                target_execution,
                transition=current_transition,
                proposal=proposal,
                source_request=source_request,
                source_execution=source_execution,
                source_manifest=source_manifest,
                target_worker=target_worker,
            )
            _sync_transition(transition, current_transition)
            return HandoffTargetIdentity(
                execution_request_id=request_id,
                execution_id=execution_id,
                lineage_id=execution_id,
                anticipated_fencing_token=anticipated_fencing_token,
            )

        existing_execution = uow.executions.get(
            execution_id,
            current_transition.workspace_id,
            for_update=True,
        )
        existing_request = uow.execution_requests.get(
            request_id,
            current_transition.workspace_id,
            for_update=True,
        )
        if existing_execution is not None or existing_request is not None:
            raise HandoffTargetError(
                "handoff target lineage exists without transition binding"
            )

        target_execution = Execution.create(
            execution_id=execution_id,
            workspace_id=current_transition.workspace_id,
            task_id=source_request.subtask_id,
            worker_item_id=current_transition.worker_item_id,
            lineage_id=execution_id,
            attempt=1,
            max_attempts=source_request.max_attempts,
            purpose="handoff",
            capability_requirements=source_request.capability_requirements,
            pinned_worker_id=current_transition.target_worker_id,
            not_before=source_request.not_before,
            source_execution_id=source_execution.id,
            source_session_id=current_transition.source_session_id,
            handoff_transition_id=current_transition.id,
            adapter_preferences=source_request.adapter_preferences,
            adapter_forbidden=source_request.adapter_forbidden,
            initial_fencing_token=source_manifest.fencing_token,
        )
        target_request = source_request.build_handoff_target(
            request_id=request_id,
            execution_lineage_id=target_execution.lineage_id,
            first_execution_id=target_execution.id,
            source_execution_id=source_execution.id,
            source_session_id=current_transition.source_session_id,
            handoff_transition_id=current_transition.id,
            now=now,
        )

        uow.execution_requests.add(target_request)
        uow.executions.add(target_execution)
        uow.flush()
        previous_transition_version = current_transition.version
        current_transition.target_execution_id = target_execution.id
        current_transition.version += 1
        uow.handoff_transitions.save(
            current_transition,
            previous_transition_version,
        )
        _sync_transition(transition, current_transition)
        return HandoffTargetIdentity(
            execution_request_id=target_request.id,
            execution_id=target_execution.id,
            lineage_id=target_execution.lineage_id,
            anticipated_fencing_token=anticipated_fencing_token,
        )

    @staticmethod
    def _validate_inputs(
        transition: HandoffTransition,
        *,
        proposal: HandoffProposal,
        source_request: ExecutionRequest,
        source_execution: Execution,
        source_manifest: ExecutionWorkspaceManifest,
    ) -> None:
        if proposal.status is not HandoffProposalStatus.CONFIRMED:
            raise HandoffTargetError("handoff proposal is not confirmed")
        if transition.proposal_id != proposal.id:
            raise HandoffTargetError("handoff proposal does not match transition")
        if transition.status not in {
            HandoffTransitionStatus.PENDING,
            HandoffTransitionStatus.RUNNING,
        }:
            raise HandoffTargetError("handoff transition is not executable")
        if transition.phase is not HandoffPhase.CREATE_TARGET_EXECUTION:
            raise HandoffTargetError("handoff transition is not at target creation")
        payload = proposal.payload
        if (
            transition.workspace_id != payload.workspace_id
            or transition.worker_item_id != payload.worker_item_id
            or transition.source_execution_id != source_execution.id
            or transition.source_session_id != payload.source_session_id
        ):
            raise HandoffTargetError("handoff transition source identity changed")
        if transition.target_runtime_snapshot.get("worker_id") != str(
            transition.target_worker_id
        ):
            raise HandoffTargetError(
                "handoff target runtime worker identity is invalid"
            )
        HandoffTargetService._validate_source_request(source_request, transition)
        HandoffTargetService._validate_source_execution(source_execution, transition)
        if source_manifest.workspace_id != transition.workspace_id:
            raise HandoffTargetError("source workspace owner does not match transition")
        if source_manifest.id != transition.source_workspace_manifest_id:
            raise HandoffTargetError("source workspace manifest identity changed")
        if source_manifest.execution_id != source_execution.id:
            raise HandoffTargetError("source workspace execution owner changed")
        if source_manifest.task_id != source_execution.task_id:
            raise HandoffTargetError("source workspace task owner changed")
        if source_manifest.lifecycle is not WorkspaceLifecycle.FROZEN:
            raise HandoffTargetError(
                "target creation requires a frozen source workspace"
            )
        if transition.source_workspace_freeze_facts is None:
            raise HandoffTargetError(
                "target creation requires source workspace freeze facts"
            )
        if source_manifest.fencing_token < 1:
            raise HandoffTargetError("source workspace fence is invalid")

    @staticmethod
    def _validate_source_request(
        request: ExecutionRequest,
        transition: HandoffTransition,
    ) -> None:
        if request.workspace_id != transition.workspace_id:
            raise HandoffTargetError(
                "source request workspace does not match transition"
            )
        if request.worker_item_id != transition.worker_item_id:
            raise HandoffTargetError(
                "source request WorkerItem does not match transition"
            )
        if request.status is not ExecutionRequestStatus.MATERIALIZED:
            raise HandoffTargetError("source request is not materialized")
        if request.execution_lineage_id is None or request.first_execution_id is None:
            raise HandoffTargetError("source request lineage is incomplete")

    @staticmethod
    def _validate_source_execution(
        execution: Execution,
        transition: HandoffTransition,
    ) -> None:
        if execution.workspace_id != transition.workspace_id:
            raise HandoffTargetError(
                "source execution workspace does not match transition"
            )
        if execution.id != transition.source_execution_id:
            raise HandoffTargetError("source execution identity changed")
        if execution.worker_item_id != transition.worker_item_id:
            raise HandoffTargetError(
                "source execution WorkerItem does not match transition"
            )
        if execution.status not in TERMINAL_EXECUTION_STATUSES:
            raise HandoffTargetError(
                "target creation requires a stopped source execution"
            )
        if execution.active_lease_id is not None:
            raise HandoffTargetError("source execution still has an active lease")
        if execution.handoff_transition_id is not None:
            raise HandoffTargetError("source execution cannot be a handoff target")

    @staticmethod
    def _validate_target_pair(
        request: ExecutionRequest,
        execution: Execution,
        *,
        transition: HandoffTransition,
        proposal: HandoffProposal,
        source_request: ExecutionRequest,
        source_execution: Execution,
        source_manifest: ExecutionWorkspaceManifest,
        target_worker: Worker,
    ) -> None:
        expected_execution_id = _target_ids(
            transition.id,
            transition.logical_attempt,
        )[1]
        if (
            request.workspace_id != transition.workspace_id
            or request.worker_item_id != transition.worker_item_id
            or request.plan_snapshot_id != source_request.plan_snapshot_id
            or request.plan_generation != source_request.plan_generation
            or request.purpose != "handoff"
            or request.status is not ExecutionRequestStatus.MATERIALIZED
            or request.handoff_transition_id != transition.id
            or request.source_execution_id != source_execution.id
            or request.source_session_id != transition.source_session_id
            or request.execution_lineage_id != expected_execution_id
            or request.first_execution_id != expected_execution_id
        ):
            raise HandoffTargetError("handoff target request facts changed")
        if (
            execution.id != expected_execution_id
            or execution.workspace_id != transition.workspace_id
            or execution.task_id != source_request.subtask_id
            or execution.worker_item_id != transition.worker_item_id
            or execution.lineage_id != expected_execution_id
            or execution.attempt != 1
            or execution.purpose != "handoff"
            or execution.pinned_worker_id != target_worker.id
            or execution.handoff_transition_id != transition.id
            or execution.source_execution_id != source_execution.id
            or execution.source_session_id != transition.source_session_id
            or execution.last_fencing_token != source_manifest.fencing_token
            or execution.active_lease_id is not None
            or execution.assigned_worker_id is not None
            or execution.status is not ExecutionStatus.QUEUED
        ):
            raise HandoffTargetError("handoff target execution facts changed")
        if target_worker.id != transition.target_worker_id:
            raise HandoffTargetError("handoff target worker identity changed")


@dataclass(frozen=True, slots=True)
class HandoffActivationGate:
    """Pure validator for target facts after the authoritative gate read."""

    def evaluate(
        self,
        *,
        execution: Execution,
        transition: HandoffTransition | None,
        operation: HandoffGateOperation,
        handoff_gate: HandoffGateResult | None = None,
    ) -> HandoffGateResult:
        if handoff_gate is not None and not handoff_gate.allowed:
            return handoff_gate
        if transition is None:
            if execution.handoff_transition_id is not None:
                return _gate_result(
                    execution=execution,
                    transition=None,
                    allowed=False,
                    blocking_code="handoff_target_stale",
                )
            return _gate_result(
                execution=execution,
                transition=None,
                allowed=True,
                blocking_code=None,
            )
        if operation is HandoffGateOperation.NORMAL_SCHEDULE:
            return _gate_result(
                execution=execution,
                transition=transition,
                allowed=False,
                blocking_code="handoff_in_progress",
            )
        if handoff_gate is not None and (
            handoff_gate.transition_id != transition.id
            or handoff_gate.target_execution_id
            not in (None, transition.target_execution_id)
        ):
            return _gate_result(
                execution=execution,
                transition=transition,
                allowed=False,
                blocking_code="handoff_target_stale",
            )
        if operation is not HandoffGateOperation.COORDINATOR_ASSIGN_TARGET:
            return _gate_result(
                execution=execution,
                transition=transition,
                allowed=False,
                blocking_code="handoff_target_stale",
            )
        if (
            transition.status
            not in {
                HandoffTransitionStatus.PENDING,
                HandoffTransitionStatus.RUNNING,
            }
            or transition.phase is not HandoffPhase.START_TARGET_SESSION
            or transition.target_execution_id != execution.id
            or execution.handoff_transition_id != transition.id
            or execution.status is not ExecutionStatus.QUEUED
            or execution.pinned_worker_id != transition.target_worker_id
            or transition.writer_state is not HandoffWriterState.TARGET_STARTING
        ):
            return _gate_result(
                execution=execution,
                transition=transition,
                allowed=False,
                blocking_code="handoff_target_stale",
            )
        return _gate_result(
            execution=execution,
            transition=transition,
            allowed=True,
            blocking_code=None,
        )


def _target_ids(transition_id: UUID, logical_attempt: int) -> tuple[UUID, UUID]:
    if logical_attempt < 1:
        raise ValidationError("handoff logical_attempt must be positive")
    return (
        uuid5(
            _TARGET_REQUEST_NAMESPACE,
            f"sidera:handoff-target-request:{transition_id}:{logical_attempt}",
        ),
        uuid5(
            _TARGET_EXECUTION_NAMESPACE,
            f"sidera:handoff-target-execution:{transition_id}:{logical_attempt}",
        ),
    )


def _sync_transition(target: HandoffTransition, source: HandoffTransition) -> None:
    target.target_execution_id = source.target_execution_id
    target.version = source.version


def _gate_result(
    *,
    execution: Execution,
    transition: HandoffTransition | None,
    allowed: bool,
    blocking_code: str | None,
) -> HandoffGateResult:
    return HandoffGateResult(
        allowed=allowed,
        blocking_code=blocking_code,
        transition_id=transition.id
        if transition is not None
        else execution.handoff_transition_id,
        status=transition.status if transition is not None else None,
        phase=transition.phase if transition is not None else None,
        writer_state=transition.writer_state if transition is not None else None,
        target_execution_id=(
            transition.target_execution_id
            if transition is not None
            else execution.id
            if execution.handoff_transition_id is not None
            else None
        ),
    )


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{name} must be timezone-aware")


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def runtime_snapshot_matches(
    runtime: ResolvedWorkerRuntime,
    expected: Mapping[str, object],
) -> bool:
    return _plain_json(runtime_snapshot_for_handoff(runtime)) == _plain_json(expected)
