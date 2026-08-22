from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from psycopg.errors import UniqueViolation
from sqlalchemy.exc import IntegrityError

from sidera_control_plane.application.command_service import hash_request
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.application.worker_policy import (
    WorkerCandidate,
    WorkerPolicy,
)
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.lease import AssignmentLease

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
                decision = self._policy.select(
                    execution,
                    tuple(WorkerCandidate.from_worker(worker) for worker in candidates),
                )
                if decision is None:
                    return 1, 0, 1, execution_id
                worker = next(
                    (item for item in candidates if item.id == decision.worker_id), None
                )
                if worker is None:
                    return 1, 0, 1, execution_id

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
                    assignment_reason=decision.reason,
                )
                worker.reserve()
                execution.assign(
                    lease_id=lease.id,
                    worker_id=worker.id,
                    fencing_token=fencing_token,
                )
                uow.leases.add(lease)
                uow.workers.save(worker, previous_worker_version)
                uow.executions.save(execution, previous_execution_version)

                command_id = uuid4()
                envelope = CommandEnvelope(
                    id=command_id,
                    workspace_id=execution.workspace_id,
                    command_type="ScheduleExecution",
                    target_type="execution",
                    actor_type="system",
                    actor_id="scheduler",
                    target_id=execution.id,
                    payload={
                        "worker_id": str(worker.id),
                        "lease_id": str(lease.id),
                        "fencing_token": fencing_token,
                        "assignment_reason": decision.reason,
                    },
                    expected_version=previous_execution_version,
                    idempotency_key=(
                        f"schedule:{execution.id}:v{previous_execution_version}"
                    ),
                    correlation_id=uuid4(),
                )
                events = (
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
                            "assignment_reason": decision.reason,
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
                result = CommandResult(
                    command_id=command_id,
                    aggregate_id=execution.id,
                    aggregate_version=execution.version,
                    event_ids=tuple(event.id for event in events),
                )
                uow.commands.record_applied(envelope, hash_request(envelope), result)
                for event in events:
                    uow.events.append_with_outbox(event)
                return 1, 1, 0, execution_id
        except IntegrityError as exc:
            if execution_id is None or not _is_active_lease_conflict(exc):
                raise
            return 1, 0, 1, execution_id


def _is_active_lease_conflict(error: IntegrityError) -> bool:
    return (
        isinstance(error.orig, UniqueViolation)
        and getattr(getattr(error.orig, "diag", None), "constraint_name", None)
        == ACTIVE_EXECUTION_LEASE_CONSTRAINT
    )


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
