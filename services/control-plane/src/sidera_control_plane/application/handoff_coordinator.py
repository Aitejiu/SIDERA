from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from sidera_control_plane.application.command_service import hash_request
from sidera_control_plane.application.handoff_context import (
    HandoffContextError,
    HandoffContextService,
)
from sidera_control_plane.application.handoff_input_transfer import (
    HandoffInputTransferError,
    HandoffInputTransferService,
)
from sidera_control_plane.application.handoff_target import (
    HandoffTargetError,
    HandoffTargetService,
)
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.application.result_source_fence import (
    ResultSourceFence,
    discover_result_source_fence,
)
from sidera_control_plane.application.scheduler import Scheduler
from sidera_control_plane.application.worker_gateway import (
    GatewayMessageError,
    GatewayRecoveryRequired,
)
from sidera_control_plane.application.workspace_handoff import (
    WorkspaceHandoffError,
    WorkspaceHandoffService,
    WorkspaceHandoffUnknown,
)
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.context_packages import (
    ContextConsumerType,
    ContextPackageStatus,
)
from sidera_control_plane.domain.errors import DomainError, InvalidTransition
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.execution import (
    Execution,
    ExecutionSessionStatus,
    ExecutionStatus,
)
from sidera_control_plane.domain.execution_requests import ExecutionRequest
from sidera_control_plane.domain.gateway import (
    DeliveryOutcome,
    DeliveryResult,
    GatewayOperationType,
    gateway_operation_id,
    handoff_step_operation_id,
)
from sidera_control_plane.domain.handoff_evidence import (
    HandoffCheckpointEvidence,
    HandoffStopEvidence,
    HandoffTargetStartResult,
    HandoffWorkspaceTargetSpec,
    HandoffWorkspaceTransferStatus,
)
from sidera_control_plane.domain.handoffs import (
    HandoffPhase,
    HandoffProposal,
    HandoffTransition,
    HandoffTransitionStatus,
)
from sidera_control_plane.domain.inputs import ExecutionInputStatus
from sidera_control_plane.domain.lease import LeaseStatus
from sidera_control_plane.domain.workspace import (
    ExecutionWorkspaceManifest,
    WorkspaceLifecycle,
)

logger = logging.getLogger(__name__)

_RUNNABLE_STATUSES = (
    HandoffTransitionStatus.PENDING,
    HandoffTransitionStatus.RUNNING,
    HandoffTransitionStatus.BLOCKED,
)

_PHASE_EVENTS = {
    HandoffPhase.CHECKPOINT_SOURCE: "HandoffCheckpointCaptured",
    HandoffPhase.STOP_SOURCE: "HandoffSourceStopped",
    HandoffPhase.FREEZE_SOURCE_WORKSPACE: "HandoffWorkspaceFrozen",
    HandoffPhase.CREATE_TARGET_EXECUTION: "HandoffTargetExecutionCreated",
    HandoffPhase.ASSEMBLE_BUNDLE: "HandoffContextBundleCreated",
    HandoffPhase.TRANSFER_WORKSPACE: "HandoffWorkspaceTransferred",
    HandoffPhase.START_TARGET_SESSION: "HandoffTargetSessionStarted",
    HandoffPhase.ACKNOWLEDGE_CONTEXT: "HandoffContextAcknowledged",
    HandoffPhase.TRANSFER_SAFE_INPUTS: "HandoffInputTransferred",
    HandoffPhase.ACTIVATE_TARGET: "HandoffTargetActivated",
    HandoffPhase.COMPLETE: "HandoffConverged",
}


class HandoffCoordinatorGateway(Protocol):
    def capture_handoff_source(
        self,
        *,
        workspace_id: UUID,
        transition_id: UUID,
        expected_transition_version: int,
    ) -> HandoffCheckpointEvidence:
        raise NotImplementedError

    def stop_handoff_source(
        self,
        *,
        workspace_id: UUID,
        transition_id: UUID,
        expected_transition_version: int,
    ) -> HandoffStopEvidence:
        raise NotImplementedError

    def start_handoff_target(
        self,
        *,
        workspace_id: UUID,
        transition_id: UUID,
        expected_transition_version: int,
    ) -> HandoffTargetStartResult:
        raise NotImplementedError

    def enable_handoff_target_writer(
        self,
        *,
        workspace_id: UUID,
        transition_id: UUID,
        expected_transition_version: int,
    ) -> DeliveryResult:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class HandoffRunResult:
    claimed: int
    progressed: int
    waiting: int
    converged: int
    blocked: int
    failed: int
    unknown: int


class _StepKind(StrEnum):
    ADVANCE = "advance"
    WAIT = "wait"
    CONVERGE = "converge"


@dataclass(frozen=True, slots=True)
class HandoffStepResult:
    kind: _StepKind
    target_phase: HandoffPhase | None = None
    event_type: str | None = None
    payload: Mapping[str, object] = field(default_factory=dict)
    target_workspace_manifest_id: UUID | None = None

    @classmethod
    def advance(
        cls,
        target_phase: HandoffPhase,
        *,
        event_type: str | None = None,
        payload: Mapping[str, object] | None = None,
        target_workspace_manifest_id: UUID | None = None,
    ) -> HandoffStepResult:
        return cls(
            kind=_StepKind.ADVANCE,
            target_phase=target_phase,
            event_type=event_type,
            payload=payload or {},
            target_workspace_manifest_id=target_workspace_manifest_id,
        )

    @classmethod
    def wait(cls) -> HandoffStepResult:
        return cls(kind=_StepKind.WAIT)

    @classmethod
    def converge(
        cls, *, payload: Mapping[str, object] | None = None
    ) -> HandoffStepResult:
        return cls(kind=_StepKind.CONVERGE, payload=payload or {})


class HandoffCoordinatorError(DomainError):
    code = "handoff_coordinator_error"


class _KnownStepFailure(HandoffCoordinatorError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        status: HandoffTransitionStatus = HandoffTransitionStatus.BLOCKED,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class _CoordinatorLeaseLost(HandoffCoordinatorError):
    code = "handoff_coordinator_lease_lost"


class HandoffCoordinator:
    """Advance one confirmed handoff through exactly one durable phase per run.

    Boundary services own their operation identities and evidence. The
    Coordinator owns only claim/resume, phase advancement, and the milestone
    event written in the same transaction as that advancement.
    """

    def __init__(
        self,
        *,
        uow_factory: Callable[[], ControlPlaneUnitOfWork],
        gateway: HandoffCoordinatorGateway | None = None,
        workspace_handoff: WorkspaceHandoffService | None = None,
        target_service: HandoffTargetService | None = None,
        context_service: HandoffContextService | None = None,
        input_transfer: HandoffInputTransferService | None = None,
        scheduler: Scheduler | None = None,
        clock: Callable[[], datetime] | None = None,
        owner_id: str = "handoff-coordinator",
        lease_ttl_seconds: int = 30,
        backoff_max_seconds: int = 60,
    ) -> None:
        if not owner_id.strip():
            raise ValueError("handoff coordinator owner_id is required")
        if lease_ttl_seconds < 1 or backoff_max_seconds < 1:
            raise ValueError("handoff coordinator limits must be positive")
        self._uow_factory = uow_factory
        self._gateway = gateway
        self._workspace_handoff = workspace_handoff
        self._target_service = target_service
        self._context_service = context_service
        self._input_transfer = input_transfer
        self._scheduler = scheduler
        self._clock = clock or (lambda: datetime.now(UTC))
        self._owner_id = owner_id.strip()
        self._lease_ttl_seconds = lease_ttl_seconds
        self._backoff_max_seconds = backoff_max_seconds

    def run_once(self, limit: int = 20) -> HandoffRunResult:
        if limit < 1:
            raise ValueError("handoff coordinator limit must be positive")
        now = self._clock()
        claimed = self._claim(now=now, limit=limit)
        counters = {
            "progressed": 0,
            "waiting": 0,
            "converged": 0,
            "blocked": 0,
            "failed": 0,
            "unknown": 0,
        }
        for claimed_transition in claimed:
            outcome = self._run_claimed(claimed_transition)
            counters[outcome] += 1
        return HandoffRunResult(claimed=len(claimed), **counters)

    def _claim(self, *, now: datetime, limit: int) -> tuple[HandoffTransition, ...]:
        with self._uow_factory() as uow:
            return uow.handoff_transitions.claim_next(
                owner_id=self._owner_id,
                now=now,
                limit=limit,
                statuses=_RUNNABLE_STATUSES,
                lease_ttl_seconds=self._lease_ttl_seconds,
            )

    def _run_claimed(self, claimed: HandoffTransition) -> str:
        try:
            transition = self._prepare_claim(claimed.id, claimed.workspace_id)
            if transition is None:
                return "waiting"
            step = self._dispatch_phase(transition)
            if step.kind is _StepKind.WAIT:
                self._defer(transition)
                return "waiting"
            if step.kind is _StepKind.CONVERGE:
                self._converge(transition)
                return "converged"
            self._advance(transition, step)
            return "progressed"
        except _CoordinatorLeaseLost:
            return "unknown"
        except (GatewayRecoveryRequired, WorkspaceHandoffUnknown) as exc:
            self._record_failure(
                claimed,
                code=_error_code(exc, "handoff_external_result_unknown"),
                status=HandoffTransitionStatus.BLOCKED_UNKNOWN,
                operation_id=_unknown_operation_id(claimed),
            )
            return "unknown"
        except _KnownStepFailure as exc:
            self._record_failure(
                claimed,
                code=exc.code,
                status=exc.status,
                operation_id=None,
            )
            return (
                "failed" if exc.status is HandoffTransitionStatus.FAILED else "blocked"
            )
        except (
            GatewayMessageError,
            HandoffContextError,
            HandoffInputTransferError,
            HandoffTargetError,
            WorkspaceHandoffError,
            InvalidTransition,
            HandoffCoordinatorError,
        ) as exc:
            self._record_failure(
                claimed,
                code=_error_code(exc, "handoff_phase_blocked"),
                status=HandoffTransitionStatus.BLOCKED,
                operation_id=None,
            )
            return "blocked"
        except Exception:
            logger.exception(
                "handoff_coordinator_phase_failed",
                extra={
                    "transition_id": str(claimed.id),
                    "workspace_id": str(claimed.workspace_id),
                },
            )
            self._record_failure(
                claimed,
                code="handoff_coordinator_unexpected_error",
                status=HandoffTransitionStatus.BLOCKED,
                operation_id=None,
            )
            return "blocked"

    def _prepare_claim(
        self, transition_id: UUID, workspace_id: UUID
    ) -> HandoffTransition | None:
        now = self._clock()
        with self._uow_factory() as uow:
            transition = uow.handoff_transitions.get(
                transition_id, workspace_id, for_update=True
            )
            if transition is None:
                return None
            self._assert_claim(transition, now)
            previous_version = transition.version
            if transition.status is HandoffTransitionStatus.PENDING:
                transition.start(now=now)
            elif transition.status is HandoffTransitionStatus.BLOCKED:
                transition.resume(now=now)
            elif transition.status is not HandoffTransitionStatus.RUNNING:
                transition.release_claim()
                uow.handoff_transitions.save(transition, previous_version)
                return None
            if transition.version != previous_version:
                uow.handoff_transitions.save(transition, previous_version)
            return transition

    def _dispatch_phase(self, transition: HandoffTransition) -> HandoffStepResult:
        handlers = {
            HandoffPhase.CHECKPOINT_SOURCE: self._checkpoint_source,
            HandoffPhase.STOP_SOURCE: self._stop_source,
            HandoffPhase.FREEZE_SOURCE_WORKSPACE: self._freeze_source_workspace,
            HandoffPhase.CREATE_TARGET_EXECUTION: self._create_target_execution,
            HandoffPhase.ASSEMBLE_BUNDLE: self._assemble_bundle,
            HandoffPhase.TRANSFER_WORKSPACE: self._transfer_workspace,
            HandoffPhase.START_TARGET_SESSION: self._start_target_session,
            HandoffPhase.ACKNOWLEDGE_CONTEXT: self._acknowledge_context,
            HandoffPhase.TRANSFER_SAFE_INPUTS: self._transfer_safe_inputs,
            HandoffPhase.ACTIVATE_TARGET: self._activate_target,
            HandoffPhase.COMPLETE: self._complete,
        }
        try:
            handler = handlers[transition.phase]
        except KeyError as exc:
            raise HandoffCoordinatorError("handoff phase is unsupported") from exc
        return handler(transition)

    def _checkpoint_source(self, transition: HandoffTransition) -> HandoffStepResult:
        gateway = self._require_gateway()
        evidence = gateway.capture_handoff_source(
            workspace_id=transition.workspace_id,
            transition_id=transition.id,
            expected_transition_version=transition.version,
        )
        if (
            evidence.transition_id != transition.id
            or evidence.source_session_id != transition.source_session_id
        ):
            raise GatewayRecoveryRequired("handoff checkpoint identity is unknown")
        return self._advance_result(transition)

    def _stop_source(self, transition: HandoffTransition) -> HandoffStepResult:
        gateway = self._require_gateway()
        evidence = gateway.stop_handoff_source(
            workspace_id=transition.workspace_id,
            transition_id=transition.id,
            expected_transition_version=transition.version,
        )
        if evidence.source_session_id != transition.source_session_id:
            raise GatewayRecoveryRequired("handoff stop identity is unknown")
        return self._advance_result(transition)

    def _freeze_source_workspace(
        self, transition: HandoffTransition
    ) -> HandoffStepResult:
        service = self._require_workspace_handoff()
        source_manifest = self._source_manifest(transition)
        frozen = service.freeze_source(
            transition=transition,
            source_manifest=source_manifest,
            now=self._clock(),
        )
        if frozen.lifecycle is not WorkspaceLifecycle.FROZEN:
            raise WorkspaceHandoffUnknown("source freeze was not confirmed")
        return self._advance_result(transition)

    def _create_target_execution(
        self, transition: HandoffTransition
    ) -> HandoffStepResult:
        service = self._require_target_service()
        proposal, source_request, source_execution, source_manifest = (
            self._source_context(transition)
        )
        identity = service.create_target(
            transition=transition,
            proposal=proposal,
            source_request=source_request,
            source_execution=source_execution,
            source_manifest=source_manifest,
            now=self._clock(),
        )
        if identity.execution_id != transition.target_execution_id:
            raise HandoffCoordinatorError("target execution identity was not persisted")
        return self._advance_result(transition)

    def _assemble_bundle(self, transition: HandoffTransition) -> HandoffStepResult:
        context_service = self._require_context_service()
        input_service = self._require_input_transfer()
        now = self._clock()
        classification = input_service.classify_source_inputs(
            transition=transition,
            now=now,
        )
        if transition.handoff_context_bundle_id is None:
            proposal, target_request, target_execution, _source_manifest = (
                self._target_context(transition)
            )
            source_facts = context_service.collect_source_facts(
                transition=transition,
                workspace_id=transition.workspace_id,
                input_classification=classification,
            )
            context_service.create_bundle_and_request(
                transition=transition,
                proposal=proposal,
                target_request=target_request,
                target_execution=target_execution,
                source_facts=source_facts,
                now=now,
            )
        package = context_service.wait_until_package_ready(
            transition_id=transition.id,
            workspace_id=transition.workspace_id,
            now=now,
        )
        if package is None:
            return HandoffStepResult.wait()
        if package.status is not ContextPackageStatus.ACTIVE:
            raise HandoffContextError("handoff target ContextPackage is not active")
        return self._advance_result(transition)

    def _transfer_workspace(self, transition: HandoffTransition) -> HandoffStepResult:
        service = self._require_workspace_handoff()
        source_manifest = self._source_manifest(transition)
        target = _target_spec(transition, source_manifest)
        transfer = service.transfer(
            transition=transition,
            source_manifest=source_manifest,
            target=target,
            now=self._clock(),
        )
        if transfer.status is HandoffWorkspaceTransferStatus.UNKNOWN:
            raise WorkspaceHandoffUnknown("workspace transfer result is unknown")
        if (
            transfer.status is not HandoffWorkspaceTransferStatus.SUCCEEDED
            or transfer.target_manifest_id is None
        ):
            raise HandoffCoordinatorError("workspace transfer did not succeed")
        return self._advance_result(
            transition,
            target_workspace_manifest_id=transfer.target_manifest_id,
        )

    def _start_target_session(self, transition: HandoffTransition) -> HandoffStepResult:
        scheduler = self._require_scheduler()
        gateway = self._require_gateway()
        scheduler.assign_handoff_target(
            workspace_id=transition.workspace_id,
            transition_id=transition.id,
            expected_transition_version=transition.version,
            now=self._clock(),
        )
        result = gateway.start_handoff_target(
            workspace_id=transition.workspace_id,
            transition_id=transition.id,
            expected_transition_version=transition.version,
        )
        if (
            result.execution_id != transition.target_execution_id
            or result.context_binding.consumer_execution_id != result.execution_id
            or result.context_binding.execution_session_id != result.session_id
        ):
            raise GatewayRecoveryRequired("target Session binding is unknown")
        return self._advance_result(transition)

    def _acknowledge_context(self, transition: HandoffTransition) -> HandoffStepResult:
        gateway = self._require_gateway()
        result = gateway.enable_handoff_target_writer(
            workspace_id=transition.workspace_id,
            transition_id=transition.id,
            expected_transition_version=transition.version,
        )
        if result.outcome is DeliveryOutcome.UNKNOWN:
            raise GatewayRecoveryRequired("target writer result is unknown")
        if result.outcome is DeliveryOutcome.BLOCKED:
            raise _KnownStepFailure(
                "target writer is blocked",
                code=result.failure_code or "handoff_writer_blocked",
            )
        if result.outcome is DeliveryOutcome.FAILED:
            raise _KnownStepFailure(
                "target writer failed",
                code=result.failure_code or "handoff_writer_failed",
                status=HandoffTransitionStatus.FAILED,
            )
        if result.execution_session_id != transition.target_session_id:
            raise GatewayRecoveryRequired("target writer Session identity is unknown")
        return self._advance_result(transition)

    def _transfer_safe_inputs(self, transition: HandoffTransition) -> HandoffStepResult:
        service = self._require_input_transfer()
        target_execution_id = transition.target_execution_id
        target_session_id = transition.target_session_id
        if target_execution_id is None or target_session_id is None:
            raise HandoffInputTransferError(
                "handoff target runtime identity is incomplete",
                code="handoff_input_target_invalid",
            )
        now = self._clock()
        classification = service.classify_source_inputs(
            transition=transition,
            now=now,
        )
        with self._uow_factory() as uow:
            target_execution = uow.executions.get(
                target_execution_id,
                transition.workspace_id,
                for_update=True,
            )
        if target_execution is None:
            raise HandoffInputTransferError(
                "handoff target Execution was not found",
                code="handoff_input_target_execution_missing",
            )
        service.transfer_safe_inputs(
            transition=transition,
            target_execution=target_execution,
            target_session_id=target_session_id,
            classification=classification,
            now=now,
        )
        return self._advance_result(transition)

    def _activate_target(self, transition: HandoffTransition) -> HandoffStepResult:
        return self._advance_result(transition)

    def _complete(self, _transition: HandoffTransition) -> HandoffStepResult:
        return HandoffStepResult.converge()

    def _advance_result(
        self,
        transition: HandoffTransition,
        *,
        target_workspace_manifest_id: UUID | None = None,
    ) -> HandoffStepResult:
        phases = tuple(HandoffPhase)
        index = phases.index(transition.phase)
        if index + 1 >= len(phases):
            raise InvalidTransition("handoff complete phase cannot advance")
        return HandoffStepResult.advance(
            phases[index + 1],
            event_type=_PHASE_EVENTS[transition.phase],
            target_workspace_manifest_id=target_workspace_manifest_id,
        )

    def _advance(self, transition: HandoffTransition, step: HandoffStepResult) -> None:
        if step.target_phase is None:
            raise HandoffCoordinatorError("handoff advance target phase is missing")
        now = self._clock()
        with self._uow_factory() as uow:
            current = self._locked_transition(uow, transition)
            self._assert_claim(current, now)
            if current.phase is not transition.phase:
                raise HandoffCoordinatorError("handoff phase changed during dispatch")
            from_phase = current.phase
            previous_version = current.version
            if step.target_workspace_manifest_id is not None:
                if current.target_workspace_manifest_id not in {
                    None,
                    step.target_workspace_manifest_id,
                }:
                    raise HandoffCoordinatorError(
                        "handoff target Workspace identity changed"
                    )
                if current.target_workspace_manifest_id is None:
                    current.target_workspace_manifest_id = (
                        step.target_workspace_manifest_id
                    )
                    current.version += 1
            if from_phase is HandoffPhase.ACTIVATE_TARGET:
                self._validate_activation_facts(uow, current, now=now)
            current.advance(step.target_phase, now=now)
            uow.handoff_transitions.save(current, previous_version)
            self._append_milestone(
                uow,
                current,
                event_type=step.event_type or _PHASE_EVENTS[from_phase],
                payload=step.payload,
            )

    def _converge(self, transition: HandoffTransition) -> None:
        now = self._clock()
        with self._uow_factory() as uow:
            self._lock_effective_result_mapping(
                uow,
                transition,
                require_target=True,
            )
            current = self._locked_transition(uow, transition)
            self._assert_claim(current, now)
            self._validate_activation_facts(uow, current, now=now)
            previous_version = current.version
            current.converge(now=now)
            uow.handoff_transitions.save(current, previous_version)
            self._append_milestone(
                uow,
                current,
                event_type="HandoffConverged",
                payload={},
            )

    @staticmethod
    def _lock_effective_result_mapping(
        uow: ControlPlaneUnitOfWork,
        transition: HandoffTransition,
        *,
        require_target: bool,
    ) -> None:
        """Fence the source-to-target switch before marking a handoff converged."""
        source_execution = uow.executions.get(
            transition.source_execution_id,
            transition.workspace_id,
        )
        if source_execution is None:
            raise HandoffCoordinatorError("handoff source Execution was not found")
        source_fence = discover_result_source_fence(uow, source_execution)
        if source_fence is None:
            raise HandoffCoordinatorError(
                "handoff source ExecutionRequest was not found"
            )
        target_probe = uow.execution_requests.get_by_handoff_transition(
            transition.id,
            transition.workspace_id,
        )
        if target_probe is None and require_target:
            raise HandoffCoordinatorError(
                "handoff target ExecutionRequest was not found"
            )

        worker_item = uow.worker_items.get(
            transition.worker_item_id,
            transition.workspace_id,
            for_update=True,
        )
        if worker_item is None:
            raise HandoffCoordinatorError("handoff WorkerItem was not found")
        request_ids = [source_fence.request_id]
        if target_probe is not None:
            request_ids.append(target_probe.id)
        requests = {
            item.id: item
            for item in uow.execution_requests.get_many(
                request_ids,
                transition.workspace_id,
                for_update=True,
            )
        }
        source_request = requests.get(source_fence.request_id)
        if not _source_fence_matches(source_fence, source_request):
            raise HandoffCoordinatorError(
                "handoff source ExecutionRequest changed before convergence"
            )
        if target_probe is not None:
            target_request = requests.get(target_probe.id)
            if (
                target_request is None
                or target_request.version != target_probe.version
                or target_request.worker_item_id != transition.worker_item_id
                or target_request.handoff_transition_id != transition.id
            ):
                raise HandoffCoordinatorError(
                    "handoff target ExecutionRequest changed before convergence"
                )

    def _defer(self, transition: HandoffTransition) -> None:
        now = self._clock()
        with self._uow_factory() as uow:
            current = self._locked_transition(uow, transition)
            self._assert_claim(current, now)
            previous_version = current.version
            current.defer(now=now, next_attempt_at=now)
            uow.handoff_transitions.save(current, previous_version)

    def _record_failure(
        self,
        claimed: HandoffTransition,
        *,
        code: str,
        status: HandoffTransitionStatus,
        operation_id: UUID | None,
    ) -> None:
        now = self._clock()
        try:
            with self._uow_factory() as uow:
                if status is HandoffTransitionStatus.BLOCKED_UNKNOWN:
                    self._lock_effective_result_mapping(
                        uow,
                        claimed,
                        require_target=False,
                    )
                current = self._locked_transition(uow, claimed)
                self._assert_claim(current, now)
                if status is HandoffTransitionStatus.BLOCKED_UNKNOWN and (
                    operation_id is None
                ):
                    raise _CoordinatorLeaseLost(
                        "unknown handoff outcome has no operation identity"
                    )
                retry_at = (
                    now + timedelta(seconds=min(1, self._backoff_max_seconds))
                    if status is HandoffTransitionStatus.BLOCKED
                    else None
                )
                if retry_at is not None:
                    current.next_attempt_at = retry_at
                previous_version = current.version
                current.block(
                    failure_code=code,
                    now=now,
                    status=status,
                    blocked_operation_id=operation_id,
                    workspace_only=(
                        status is HandoffTransitionStatus.BLOCKED_UNKNOWN
                        and current.phase
                        in {
                            HandoffPhase.FREEZE_SOURCE_WORKSPACE,
                            HandoffPhase.CREATE_TARGET_EXECUTION,
                            HandoffPhase.ASSEMBLE_BUNDLE,
                            HandoffPhase.TRANSFER_WORKSPACE,
                        }
                    ),
                )
                uow.handoff_transitions.save(current, previous_version)
                self._append_milestone(
                    uow,
                    current,
                    event_type="HandoffBlocked",
                    payload={
                        "failure_code": code,
                        "blocked_operation_id": (
                            str(operation_id) if operation_id is not None else None
                        ),
                    },
                )
        except _CoordinatorLeaseLost:
            raise
        except Exception:
            logger.exception(
                "handoff_coordinator_failure_persistence_failed",
                extra={"transition_id": str(claimed.id)},
            )

    def _append_milestone(
        self,
        uow: ControlPlaneUnitOfWork,
        transition: HandoffTransition,
        *,
        event_type: str,
        payload: Mapping[str, object],
    ) -> None:
        event_payload = {
            "transition_id": str(transition.id),
            "worker_item_id": str(transition.worker_item_id),
            "source_execution_id": str(transition.source_execution_id),
            "source_session_id": str(transition.source_session_id),
            "target_execution_id": _string_uuid(transition.target_execution_id),
            "target_session_id": _string_uuid(transition.target_session_id),
            "target_workspace_manifest_id": _string_uuid(
                transition.target_workspace_manifest_id
            ),
            "target_context_package_id": _string_uuid(
                transition.target_context_package_id
            ),
            "status": transition.status.value,
            "phase": transition.phase.value,
            "writer_state": transition.writer_state.value,
            "version": transition.version,
        }
        for key, value in payload.items():
            if key in {"failure_code", "blocked_operation_id"}:
                event_payload[key] = _bounded_event_value(value)
        command_id = uuid5(
            NAMESPACE_URL,
            f"sidera:handoff-command:{transition.id}:{event_type}:v{transition.version}",
        )
        event_id = uuid5(
            NAMESPACE_URL,
            f"sidera:handoff-event:{transition.id}:{event_type}:v{transition.version}",
        )
        envelope = CommandEnvelope(
            id=command_id,
            workspace_id=transition.workspace_id,
            command_type="HandoffCoordinator",
            target_type="handoff_transition",
            actor_type="system",
            actor_id=self._owner_id,
            target_id=transition.id,
            payload={
                "transition_id": str(transition.id),
                "event_type": event_type,
                "aggregate_version": transition.version,
            },
            expected_version=transition.version,
            idempotency_key=(
                f"handoff:{transition.id}:{event_type}:v{transition.version}"
            ),
            correlation_id=transition.id,
        )
        event = EventData(
            id=event_id,
            workspace_id=transition.workspace_id,
            aggregate_type="handoff_transition",
            aggregate_id=transition.id,
            aggregate_version=transition.version,
            event_type=event_type,
            payload=event_payload,
            actor_type=envelope.actor_type,
            actor_id=envelope.actor_id,
            causation_id=envelope.id,
            correlation_id=envelope.correlation_id,
        )
        if uow.commands.find_by_id(transition.workspace_id, command_id) is not None:
            return
        uow.commands.record_applied(
            envelope,
            hash_request(envelope),
            CommandResult(
                command_id=command_id,
                aggregate_id=transition.id,
                aggregate_version=transition.version,
                event_ids=(event_id,),
            ),
        )
        uow.events.append_with_outbox(event)

    def _validate_activation_facts(
        self,
        uow: ControlPlaneUnitOfWork,
        transition: HandoffTransition,
        *,
        now: datetime,
    ) -> None:
        target_execution_id = transition.target_execution_id
        target_session_id = transition.target_session_id
        target_manifest_id = transition.target_workspace_manifest_id
        package_id = transition.target_context_package_id
        package_hash = transition.target_context_package_hash
        bundle_id = transition.handoff_context_bundle_id
        if None in {
            target_execution_id,
            target_session_id,
            target_manifest_id,
            package_id,
            package_hash,
            bundle_id,
        }:
            raise HandoffCoordinatorError(
                "handoff activation identities are incomplete"
            )
        assert target_execution_id is not None
        assert target_session_id is not None
        assert target_manifest_id is not None
        assert package_id is not None
        assert package_hash is not None
        assert bundle_id is not None

        target = uow.executions.get(
            target_execution_id, transition.workspace_id, for_update=True
        )
        if target is None or target.worker_item_id != transition.worker_item_id:
            raise HandoffCoordinatorError("handoff target Execution facts are stale")
        if (
            target.status is not ExecutionStatus.RUNNING
            or target.handoff_transition_id != transition.id
            or target.assigned_worker_id != transition.target_worker_id
            or target.active_lease_id is None
        ):
            raise HandoffCoordinatorError("handoff target Execution is not active")

        binding = uow.bindings.get_for_execution(
            target.id, transition.workspace_id, for_update=True
        )
        if binding is None or binding.worker_id != transition.target_worker_id:
            raise HandoffCoordinatorError("handoff target binding is missing")
        if (
            binding.lease_id != target.active_lease_id
            or binding.fencing_token != target.last_fencing_token
        ):
            raise HandoffCoordinatorError("handoff target binding fence is stale")
        lease = uow.leases.get(
            target.active_lease_id, transition.workspace_id, for_update=True
        )
        if lease is None or lease.status is not LeaseStatus.ACTIVE:
            raise HandoffCoordinatorError("handoff target lease is not active")
        lease.assert_owner(binding.worker_id, binding.fencing_token, now)

        session = uow.sessions.get_any(
            target_session_id, transition.workspace_id, for_update=True
        )
        if session is None or (
            session.execution_id != target.id
            or session.worker_id != transition.target_worker_id
            or session.status is not ExecutionSessionStatus.ACTIVE
        ):
            raise HandoffCoordinatorError("handoff target Session is not active")

        manifest = uow.workspaces.get(
            target_manifest_id, transition.workspace_id, for_update=True
        )
        if manifest is None or (
            manifest.execution_id != target.id
            or manifest.lifecycle is not WorkspaceLifecycle.ACTIVE
            or manifest.fencing_token != binding.fencing_token
        ):
            raise HandoffCoordinatorError("handoff target Workspace is not active")

        package = uow.context_packages.get(
            package_id, transition.workspace_id, for_update=True
        )
        if package is None or (
            package.status is not ContextPackageStatus.ACTIVE
            or package.package_hash != package_hash
            or package.consumer_type is not ContextConsumerType.WORKER
            or package.consumer_id != target.id
            or package.worker_item_id != transition.worker_item_id
            or package.context_version != transition.target_context_version
            or package.expires_at <= now
        ):
            raise HandoffCoordinatorError(
                "handoff target ContextPackage binding is stale"
            )
        acknowledgement = uow.context_packages.get_ack(
            package.package_id,
            ContextConsumerType.WORKER,
            target.id,
            transition.workspace_id,
            for_update=True,
        )
        if acknowledgement is None or (
            acknowledgement.package_hash != package.package_hash
            or acknowledgement.context_version != package.context_version
            or acknowledgement.adapter_module_id != binding.adapter_module_id
            or acknowledgement.adapter_config_version != binding.adapter_config_version
        ):
            raise HandoffCoordinatorError(
                "handoff target ContextPackage acknowledgement is stale"
            )

        bundle = uow.handoff_context_bundles.get(
            bundle_id, transition.workspace_id, for_update=True
        )
        if bundle is None or (
            bundle.target_execution_id != target.id
            or bundle.worker_item_id != transition.worker_item_id
            or bundle.target_context_version != transition.target_context_version
        ):
            raise HandoffCoordinatorError("handoff context Bundle is stale")
        derived = uow.execution_inputs.list_for_handoff_transition(
            transition.id, transition.workspace_id, for_update=True
        )
        derived_source_ids: list[UUID] = []
        for item in derived:
            if (
                item.source_input_id is None
                or item.execution_id != target.id
                or item.session_id != session.id
                or item.status is not ExecutionInputStatus.QUEUED
            ):
                raise HandoffCoordinatorError("handoff input plan is incomplete")
            derived_source_ids.append(item.source_input_id)
        if set(derived_source_ids) != set(bundle.transferable_input_refs):
            raise HandoffCoordinatorError("handoff input plan is incomplete")

        _validate_target_runtime_snapshot(transition, binding)

    def _source_manifest(
        self, transition: HandoffTransition
    ) -> ExecutionWorkspaceManifest:
        with self._uow_factory() as uow:
            manifest = uow.workspaces.get(
                transition.source_workspace_manifest_id,
                transition.workspace_id,
                for_update=True,
            )
        if manifest is None:
            raise WorkspaceHandoffError("handoff source Workspace was not found")
        return manifest

    def _source_context(
        self, transition: HandoffTransition
    ) -> tuple[
        HandoffProposal, ExecutionRequest, Execution, ExecutionWorkspaceManifest
    ]:
        with self._uow_factory() as uow:
            proposal = uow.handoff_proposals.get(
                transition.proposal_id, transition.workspace_id, for_update=True
            )
            source_execution = uow.executions.get(
                transition.source_execution_id,
                transition.workspace_id,
                for_update=True,
            )
            source_manifest = uow.workspaces.get(
                transition.source_workspace_manifest_id,
                transition.workspace_id,
                for_update=True,
            )
            source_request = (
                uow.execution_requests.get_by_execution_lineage(
                    source_execution.lineage_id,
                    transition.workspace_id,
                    for_update=True,
                )
                if source_execution is not None
                else None
            )
        if not isinstance(proposal, HandoffProposal):
            raise HandoffCoordinatorError("handoff proposal was not found")
        if not isinstance(source_execution, Execution):
            raise HandoffCoordinatorError("handoff source Execution was not found")
        if not isinstance(source_request, ExecutionRequest):
            raise HandoffCoordinatorError(
                "handoff source ExecutionRequest was not found"
            )
        if not isinstance(source_manifest, ExecutionWorkspaceManifest):
            raise HandoffCoordinatorError("handoff source Workspace was not found")
        return proposal, source_request, source_execution, source_manifest

    def _target_context(
        self, transition: HandoffTransition
    ) -> tuple[
        HandoffProposal, ExecutionRequest, Execution, ExecutionWorkspaceManifest
    ]:
        proposal, _source_request, _source_execution, source_manifest = (
            self._source_context(transition)
        )
        with self._uow_factory() as uow:
            target_execution = (
                uow.executions.get(
                    transition.target_execution_id,
                    transition.workspace_id,
                    for_update=True,
                )
                if transition.target_execution_id is not None
                else None
            )
            target_request = uow.execution_requests.get_by_handoff_transition(
                transition.id, transition.workspace_id, for_update=True
            )
        if not isinstance(target_execution, Execution):
            raise HandoffCoordinatorError("handoff target Execution was not found")
        if not isinstance(target_request, ExecutionRequest):
            raise HandoffCoordinatorError(
                "handoff target ExecutionRequest was not found"
            )
        return proposal, target_request, target_execution, source_manifest

    def _locked_transition(
        self, uow: ControlPlaneUnitOfWork, transition: HandoffTransition
    ) -> HandoffTransition:
        current = uow.handoff_transitions.get(
            transition.id, transition.workspace_id, for_update=True
        )
        if current is None:
            raise HandoffCoordinatorError("handoff transition was not found")
        return current

    def _assert_claim(self, transition: HandoffTransition, now: datetime) -> None:
        if transition.lease_owner != self._owner_id:
            raise _CoordinatorLeaseLost("handoff coordinator claim is owned elsewhere")
        if transition.lease_expires_at is None or transition.lease_expires_at <= now:
            raise _CoordinatorLeaseLost("handoff coordinator claim has expired")

    def _require_gateway(self) -> HandoffCoordinatorGateway:
        if self._gateway is None:
            raise HandoffCoordinatorError("handoff gateway is not configured")
        return self._gateway

    def _require_workspace_handoff(self) -> WorkspaceHandoffService:
        if self._workspace_handoff is None:
            raise HandoffCoordinatorError("workspace handoff service is not configured")
        return self._workspace_handoff

    def _require_target_service(self) -> HandoffTargetService:
        if self._target_service is None:
            raise HandoffCoordinatorError("handoff target service is not configured")
        return self._target_service

    def _require_context_service(self) -> HandoffContextService:
        if self._context_service is None:
            raise HandoffCoordinatorError("handoff context service is not configured")
        return self._context_service

    def _require_input_transfer(self) -> HandoffInputTransferService:
        if self._input_transfer is None:
            raise HandoffCoordinatorError("handoff input service is not configured")
        return self._input_transfer

    def _require_scheduler(self) -> Scheduler:
        if self._scheduler is None:
            raise HandoffCoordinatorError("handoff scheduler is not configured")
        return self._scheduler


def _target_spec(
    transition: HandoffTransition, source_manifest: ExecutionWorkspaceManifest
) -> HandoffWorkspaceTargetSpec:
    target_execution_id = transition.target_execution_id
    if target_execution_id is None:
        raise WorkspaceHandoffError("handoff target Execution is not created")
    snapshot = transition.target_runtime_snapshot
    repositories = tuple(
        {
            "repository_ref": checkout.repository_ref,
            "base_revision": checkout.base_revision,
            "branch": checkout.branch,
            "current_head": checkout.current_head,
        }
        for checkout in source_manifest.checkouts
    )
    return HandoffWorkspaceTargetSpec(
        workspace_id=transition.workspace_id,
        task_id=source_manifest.task_id,
        target_execution_id=target_execution_id,
        target_worker_id=transition.target_worker_id,
        adapter_module_ref=_snapshot_uuid(snapshot, "adapter_module_instance_id"),
        adapter_config_version=_snapshot_int(snapshot, "adapter_module_config_version"),
        workspace_module_ref=_snapshot_uuid(snapshot, "workspace_module_instance_id"),
        workspace_config_version=_snapshot_int(
            snapshot, "workspace_module_config_version"
        ),
        repositories=repositories,
        anticipated_fencing_token=source_manifest.fencing_token + 1,
    )


def _validate_target_runtime_snapshot(
    transition: HandoffTransition, binding: Any
) -> None:
    snapshot = transition.target_runtime_snapshot
    if (
        _snapshot_uuid(snapshot, "adapter_module_instance_id")
        != binding.adapter_module_id
        or _snapshot_int(snapshot, "adapter_module_config_version")
        != binding.adapter_config_version
        or _snapshot_uuid(snapshot, "workspace_module_instance_id")
        != binding.workspace_module_id
        or _snapshot_int(snapshot, "workspace_module_config_version")
        != binding.workspace_config_version
    ):
        raise HandoffCoordinatorError("handoff target runtime binding is stale")


def _snapshot_uuid(snapshot: Mapping[str, object], key: str) -> UUID:
    value = snapshot.get(key)
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise HandoffCoordinatorError(
            f"handoff runtime snapshot field {key} is invalid"
        ) from exc


def _snapshot_int(snapshot: Mapping[str, object], key: str) -> int:
    value = snapshot.get(key)
    if isinstance(value, bool):
        raise HandoffCoordinatorError(
            f"handoff runtime snapshot field {key} is invalid"
        )
    try:
        result = int(str(value))
    except (TypeError, ValueError) as exc:
        raise HandoffCoordinatorError(
            f"handoff runtime snapshot field {key} is invalid"
        ) from exc
    if result < 1:
        raise HandoffCoordinatorError(
            f"handoff runtime snapshot field {key} is invalid"
        )
    return result


def _string_uuid(value: UUID | None) -> str | None:
    return str(value) if value is not None else None


def _source_fence_matches(
    fence: ResultSourceFence,
    request: ExecutionRequest | None,
) -> bool:
    return (
        request is not None
        and request.workspace_id == fence.workspace_id
        and request.id == fence.request_id
        and request.version == fence.request_version
        and request.worker_item_id == fence.worker_item_id
        and request.execution_lineage_id == fence.request_lineage_id
    )


def _bounded_event_value(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, UUID):
        return str(value)
    text = str(value).strip()
    return text[:256]


def _error_code(error: BaseException, default: str) -> str:
    code = getattr(error, "code", None)
    if isinstance(code, str) and code.strip():
        return code.strip()[:128]
    return default


def _unknown_operation_id(transition: HandoffTransition) -> UUID | None:
    if transition.phase is HandoffPhase.CHECKPOINT_SOURCE:
        return handoff_step_operation_id(
            transition.id,
            transition.phase,
            logical_attempt=transition.logical_attempt,
            action="checkpoint",
        )
    if transition.phase is HandoffPhase.STOP_SOURCE:
        return gateway_operation_id(
            transition.source_execution_id, GatewayOperationType.STOP_SESSION
        )
    if transition.phase is HandoffPhase.FREEZE_SOURCE_WORKSPACE:
        return handoff_step_operation_id(
            transition.id,
            transition.phase,
            logical_attempt=transition.logical_attempt,
            action="freeze_workspace",
        )
    if transition.phase is HandoffPhase.TRANSFER_WORKSPACE:
        return handoff_step_operation_id(
            transition.id,
            transition.phase,
            logical_attempt=transition.logical_attempt,
            action="transfer_workspace",
        )
    if transition.phase is HandoffPhase.START_TARGET_SESSION:
        if transition.target_execution_id is None:
            return None
        return gateway_operation_id(
            transition.target_execution_id, GatewayOperationType.START_SESSION
        )
    if transition.phase is HandoffPhase.ACKNOWLEDGE_CONTEXT:
        return handoff_step_operation_id(
            transition.id,
            transition.phase,
            logical_attempt=transition.logical_attempt,
            action="enable_writer",
        )
    return None
