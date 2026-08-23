from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from psycopg.errors import UniqueViolation
from sqlalchemy.exc import IntegrityError

from sidera_control_plane.application.command_service import hash_request
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.application.scheduling_blocks import (
    SchedulingBlockService,
    aggregate_runtime_issues,
)
from sidera_control_plane.application.worker_policy import (
    WorkerCandidate,
    WorkerPolicy,
)
from sidera_control_plane.application.worker_runtime import (
    ResolvedWorkerRuntime,
    RuntimeCandidateInspection,
    WorkerRuntimeResolver,
)
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.execution import Execution
from sidera_control_plane.domain.lease import AssignmentLease
from sidera_control_plane.domain.modules import (
    ExecutionBinding,
    WorkerAdapterCapabilityProfile,
)
from sidera_control_plane.domain.scheduling import SchedulingIssue
from sidera_control_plane.domain.worker import Worker

ACTIVE_EXECUTION_LEASE_CONSTRAINT = "uq_assignment_leases_active_execution"


@dataclass(frozen=True, slots=True)
class SchedulerRunResult:
    examined: int
    assigned: int
    deferred: int


class Scheduler:
    def __init__(
        self,
        uow_factory: Callable[[], ControlPlaneUnitOfWork],
        policy: WorkerPolicy,
        clock: Callable[[], datetime],
        *,
        assignment_lease_seconds: int = 30,
        worker_heartbeat_timeout_seconds: int = 45,
        runtime_resolver: WorkerRuntimeResolver | None = None,
        scheduling_blocks: SchedulingBlockService | None = None,
    ) -> None:
        if assignment_lease_seconds <= 0:
            raise ValueError("assignment_lease_seconds must be positive")
        if worker_heartbeat_timeout_seconds <= 0:
            raise ValueError("worker_heartbeat_timeout_seconds must be positive")
        self._uow_factory = uow_factory
        self._policy = policy
        self._clock = clock
        self._assignment_lease_seconds = assignment_lease_seconds
        self._worker_heartbeat_timeout_seconds = worker_heartbeat_timeout_seconds
        self._runtime_resolver = runtime_resolver or WorkerRuntimeResolver()
        self._scheduling_blocks = scheduling_blocks or SchedulingBlockService()

    def run_once(self, limit: int) -> SchedulerRunResult:
        if limit < 1:
            raise ValueError("limit must be positive")
        examined = 0
        assigned = 0
        deferred = 0
        deferred_ids: set[UUID] = set()
        for _ in range(limit):
            item_examined, item_assigned, item_deferred, execution_id = (
                self._assign_one(excluded_ids=frozenset(deferred_ids))
            )
            examined += item_examined
            assigned += item_assigned
            deferred += item_deferred
            if item_examined == 0:
                break
            if item_deferred:
                if execution_id is None:
                    break
                deferred_ids.add(execution_id)
        return SchedulerRunResult(
            examined=examined,
            assigned=assigned,
            deferred=deferred,
        )

    def _assign_one(
        self, *, excluded_ids: Collection[UUID]
    ) -> tuple[int, int, int, UUID | None]:
        return self._assign_one_transaction(excluded_ids=excluded_ids)

    def _assign_one_transaction(
        self, *, excluded_ids: Collection[UUID]
    ) -> tuple[int, int, int, UUID | None]:
        now = self._clock()
        execution_id: UUID | None = None
        try:
            with self._uow_factory() as uow:
                claimed = uow.executions.claim_schedulable(
                    now=now,
                    limit=1,
                    excluded_ids=excluded_ids,
                )
                if not claimed:
                    return 0, 0, 0, None
                execution = claimed[0]
                execution_id = execution.id
                candidates = uow.workers.lock_candidates(
                    workspace_id=execution.workspace_id,
                    required_capabilities=execution.capability_requirements,
                    pinned_worker_id=execution.pinned_worker_id,
                    heartbeat_cutoff=now
                    - timedelta(seconds=self._worker_heartbeat_timeout_seconds),
                )
                forbidden_ids = {
                    worker.id
                    for worker in candidates
                    if worker.adapter_type in execution.adapter_forbidden
                }
                runtime_candidates = tuple(
                    worker
                    for worker in candidates
                    if worker.id not in forbidden_ids
                )
                inspections = self._runtime_resolver.inspect_candidates(
                    uow,
                    runtime_candidates,
                    execution,
                )
                diagnostics = list(inspections)
                remaining = [
                    inspection
                    for inspection in inspections
                    if inspection.resolved is not None
                ]

                while remaining:
                    decision = self._policy.select(
                        execution,
                        tuple(
                            WorkerCandidate.from_worker(item.worker)
                            for item in remaining
                        ),
                    )
                    if decision is None:
                        break
                    selected = next(
                        (
                            item
                            for item in remaining
                            if item.worker.id == decision.worker_id
                        ),
                        None,
                    )
                    if selected is None:
                        break
                    resolved = self._runtime_resolver.lock_and_resolve(
                        uow,
                        selected.worker,
                        execution,
                    )
                    if isinstance(resolved, SchedulingIssue):
                        diagnostics = [
                            item
                            for item in diagnostics
                            if item.worker.id != selected.worker.id
                        ]
                        diagnostics.append(
                            RuntimeCandidateInspection(
                                worker=selected.worker,
                                resolved=None,
                                issue=resolved,
                            )
                        )
                        remaining = [
                            item
                            for item in remaining
                            if item.worker.id != selected.worker.id
                        ]
                        continue

                    return self._commit_assignment(
                        uow=uow,
                        execution=execution,
                        worker=selected.worker,
                        resolved=resolved,
                        decision_reason=decision.reason,
                        now=now,
                        execution_id=execution_id,
                    )

                issue = aggregate_runtime_issues(
                    execution=execution,
                    inspections=tuple(diagnostics),
                    base_candidates=candidates,
                    forbidden_worker_ids=forbidden_ids,
                )
                return self._commit_deferred(
                    uow=uow,
                    execution=execution,
                    issue=issue,
                    now=now,
                    execution_id=execution_id,
                )
        except IntegrityError as exc:
            if execution_id is None or not _is_active_lease_conflict(exc):
                raise
            return 1, 0, 1, execution_id

    def _commit_assignment(
        self,
        *,
        uow: ControlPlaneUnitOfWork,
        execution: Execution,
        worker: Worker,
        resolved: ResolvedWorkerRuntime,
        decision_reason: dict[str, object],
        now: datetime,
        execution_id: UUID,
    ) -> tuple[int, int, int, UUID]:
        previous_execution_version = execution.version
        previous_worker_version = worker.version
        fencing_token = execution.last_fencing_token + 1
        lease = AssignmentLease.issue(
            lease_id=uuid4(),
            workspace_id=execution.workspace_id,
            execution_id=execution.id,
            worker_id=worker.id,
            issued_at=now,
            expires_at=now + timedelta(seconds=self._assignment_lease_seconds),
            fencing_token=fencing_token,
            assignment_reason=decision_reason,
        )
        worker.reserve()
        execution.assign(
            lease_id=lease.id,
            worker_id=worker.id,
            fencing_token=fencing_token,
        )
        binding = ExecutionBinding(
            id=uuid4(),
            workspace_id=execution.workspace_id,
            execution_id=execution.id,
            worker_id=worker.id,
            adapter_module_id=resolved.adapter_module_instance_id,
            workspace_module_id=resolved.workspace_module_instance_id,
            adapter_config_version=resolved.adapter_module_config_version,
            workspace_config_version=resolved.workspace_module_config_version,
            capability_snapshot=resolved.capability_snapshot,
            lease_id=lease.id,
            fencing_token=fencing_token,
            version=1,
        )
        uow.leases.add(lease)
        uow.bindings.add(binding)
        uow.workers.save(worker, previous_worker_version)
        uow.executions.save(execution, previous_execution_version)

        envelope = _schedule_envelope(
            execution,
            payload={
                "worker_id": str(worker.id),
                "lease_id": str(lease.id),
                "binding_id": str(binding.id),
                "fencing_token": fencing_token,
                "assignment_reason": decision_reason,
            },
            expected_version=previous_execution_version,
        )
        events = (
            _event(
                envelope,
                workspace_id=binding.workspace_id,
                aggregate_type="execution_binding",
                aggregate_id=binding.id,
                aggregate_version=binding.version,
                event_type="ExecutionRuntimeBound",
                payload={
                    "execution_id": str(execution.id),
                    "worker_id": str(worker.id),
                    "adapter_module_id": str(binding.adapter_module_id),
                    "workspace_module_id": str(binding.workspace_module_id),
                    "adapter_config_version": binding.adapter_config_version,
                    "workspace_config_version": binding.workspace_config_version,
                    "lease_id": str(lease.id),
                    "fencing_token": fencing_token,
                    "capability_snapshot": _capability_payload(
                        binding.capability_snapshot
                    ),
                },
            ),
            _event(
                envelope,
                workspace_id=execution.workspace_id,
                aggregate_type="execution",
                aggregate_id=execution.id,
                aggregate_version=execution.version,
                event_type="ExecutionLeased",
                payload={
                    "status": execution.status.value,
                    "worker_id": str(worker.id),
                    "lease_id": str(lease.id),
                    "fencing_token": fencing_token,
                },
            ),
            _event(
                envelope,
                workspace_id=lease.workspace_id,
                aggregate_type="lease",
                aggregate_id=lease.id,
                aggregate_version=lease.version,
                event_type="LeaseGranted",
                payload={
                    "status": lease.status.value,
                    "execution_id": str(execution.id),
                    "worker_id": str(worker.id),
                    "fencing_token": fencing_token,
                    "assignment_reason": decision_reason,
                },
            ),
            _event(
                envelope,
                workspace_id=worker.workspace_id,
                aggregate_type="worker",
                aggregate_id=worker.id,
                aggregate_version=worker.version,
                event_type="WorkerCapacityReserved",
                payload={
                    "status": worker.status.value,
                    "desired_state": worker.desired_state.value,
                    "current_load": worker.current_load,
                    "capacity": worker.capacity,
                },
            ),
        )
        unblocked_events = self._scheduling_blocks.resolve(
            uow,
            execution=execution,
            envelope=envelope,
            now=now,
        )
        all_events = events + unblocked_events
        _commit_events(uow, envelope, execution, all_events)
        return 1, 1, 0, execution_id

    def _commit_deferred(
        self,
        *,
        uow: ControlPlaneUnitOfWork,
        execution: Execution,
        issue: SchedulingIssue,
        now: datetime,
        execution_id: UUID,
    ) -> tuple[int, int, int, UUID]:
        envelope = _schedule_envelope(
            execution,
            payload={
                "scheduling_reason": {
                    "reason_code": issue.reason_code.value,
                    "reason_details": _plain_json(issue.reason_details),
                    "requires_operator": issue.requires_operator,
                }
            },
            expected_version=execution.version,
        )
        events = self._scheduling_blocks.record(
            uow,
            execution=execution,
            issue=issue,
            envelope=envelope,
            now=now,
        )
        _commit_events(uow, envelope, execution, events)
        return 1, 0, 1, execution_id


def _is_active_lease_conflict(error: IntegrityError) -> bool:
    return (
        isinstance(error.orig, UniqueViolation)
        and getattr(getattr(error.orig, "diag", None), "constraint_name", None)
        == ACTIVE_EXECUTION_LEASE_CONSTRAINT
    )


def _schedule_envelope(
    execution: Execution,
    *,
    payload: dict[str, object],
    expected_version: int,
) -> CommandEnvelope:
    command_id = uuid4()
    return CommandEnvelope(
        id=command_id,
        workspace_id=execution.workspace_id,
        command_type="ScheduleExecution",
        target_type="execution",
        actor_type="system",
        actor_id="scheduler",
        target_id=execution.id,
        payload=payload,
        expected_version=expected_version,
        idempotency_key=f"schedule:{execution.id}:{command_id}",
        correlation_id=uuid4(),
    )


def _commit_events(
    uow: ControlPlaneUnitOfWork,
    envelope: CommandEnvelope,
    execution: Execution,
    events: tuple[EventData, ...],
) -> None:
    result = CommandResult(
        command_id=envelope.id,
        aggregate_id=execution.id,
        aggregate_version=execution.version,
        event_ids=tuple(event.id for event in events),
    )
    uow.commands.record_applied(envelope, hash_request(envelope), result)
    for event in events:
        uow.events.append_with_outbox(event)


def _capability_payload(
    profile: WorkerAdapterCapabilityProfile,
) -> dict[str, object]:
    return {
        "input": profile.input.value,
        "idempotency": profile.idempotency.value,
        "resume": profile.resume.value,
        "stop": profile.stop.value,
        "sandbox": profile.sandbox.value,
        "stability": profile.stability.value,
    }


def _plain_json(value: object) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def _event(
    envelope: CommandEnvelope,
    *,
    workspace_id: UUID,
    aggregate_type: str,
    aggregate_id: UUID,
    aggregate_version: int,
    event_type: str,
    payload: dict[str, object],
) -> EventData:
    return EventData(
        id=uuid4(),
        workspace_id=workspace_id,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        aggregate_version=aggregate_version,
        event_type=event_type,
        payload=payload,
        actor_type=envelope.actor_type,
        actor_id=envelope.actor_id,
        causation_id=envelope.id,
        correlation_id=envelope.correlation_id,
    )
