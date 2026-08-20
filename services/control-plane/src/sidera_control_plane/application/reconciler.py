from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sidera_control_plane.application.command_service import hash_request
from sidera_control_plane.application.ports import ControlPlaneUnitOfWork
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.execution import Execution, ExecutionStatus
from sidera_control_plane.domain.lease import LeaseStatus
from sidera_control_plane.domain.task import TaskStatus


@dataclass(frozen=True, slots=True)
class ReconcilerRunResult:
    offline_workers: int
    expired_leases: int
    retries_queued: int


@dataclass(frozen=True, slots=True)
class _LeaseReconcileResult:
    expired: bool
    retry_queued: bool


class Reconciler:
    def __init__(
        self,
        uow_factory: Callable[[], ControlPlaneUnitOfWork],
        clock: Callable[[], datetime],
        *,
        worker_heartbeat_timeout_seconds: int = 45,
    ) -> None:
        if worker_heartbeat_timeout_seconds <= 0:
            raise ValueError("worker_heartbeat_timeout_seconds must be positive")
        self._uow_factory = uow_factory
        self._clock = clock
        self._worker_heartbeat_timeout_seconds = worker_heartbeat_timeout_seconds

    def run_once(self, limit: int) -> ReconcilerRunResult:
        if limit < 1:
            raise ValueError("limit must be positive")
        now = self._clock()
        offline_workers = self._reconcile_stale_workers(now=now, limit=limit)
        expired_lease_ids = self._claim_expired_lease_ids(now=now, limit=limit)
        expired_leases = 0
        retries_queued = 0
        for lease_id, workspace_id in expired_lease_ids:
            result = self._reconcile_expired_lease(
                lease_id=lease_id,
                workspace_id=workspace_id,
                now=now,
            )
            if result.expired:
                expired_leases += 1
            if result.retry_queued:
                retries_queued += 1
        return ReconcilerRunResult(
            offline_workers=offline_workers,
            expired_leases=expired_leases,
            retries_queued=retries_queued,
        )

    def _reconcile_stale_workers(self, *, now: datetime, limit: int) -> int:
        heartbeat_cutoff = now - timedelta(
            seconds=self._worker_heartbeat_timeout_seconds
        )
        with self._uow_factory() as uow:
            workers = uow.workers.claim_stale(
                heartbeat_cutoff=heartbeat_cutoff,
                limit=limit,
            )
            for worker in workers:
                previous_version = worker.version
                worker.mark_offline()
                uow.workers.save(worker, previous_version)
                envelope = _system_envelope(
                    workspace_id=worker.workspace_id,
                    command_type="MarkWorkerOffline",
                    target_type="worker",
                    target_id=worker.id,
                    expected_version=previous_version,
                    idempotency_key=f"worker-offline:{worker.id}:v{previous_version}",
                    payload={
                        "worker_id": str(worker.id),
                        "status": worker.status.value,
                        "desired_state": worker.desired_state.value,
                    },
                )
                event = _event(
                    envelope,
                    workspace_id=worker.workspace_id,
                    aggregate_type="worker",
                    aggregate_id=worker.id,
                    aggregate_version=worker.version,
                    event_type="WorkerOffline",
                    payload={
                        "status": worker.status.value,
                        "desired_state": worker.desired_state.value,
                        "current_load": worker.current_load,
                        "capacity": worker.capacity,
                    },
                )
                _record_system_operation(uow, envelope, worker.id, worker.version, (event,))
            return len(workers)

    def _claim_expired_lease_ids(
        self, *, now: datetime, limit: int
    ) -> tuple[tuple[UUID, UUID], ...]:
        with self._uow_factory() as uow:
            leases = uow.leases.claim_expired(now=now, limit=limit)
            return tuple((lease.id, lease.workspace_id) for lease in leases)

    def _reconcile_expired_lease(
        self,
        *,
        lease_id: UUID,
        workspace_id: UUID,
        now: datetime,
    ) -> _LeaseReconcileResult:
        with self._uow_factory() as uow:
            lease_snapshot = uow.leases.get(lease_id, workspace_id)
            if (
                lease_snapshot is None
                or lease_snapshot.status is not LeaseStatus.ACTIVE
                or lease_snapshot.expires_at > now
            ):
                return _LeaseReconcileResult(expired=False, retry_queued=False)
            execution = uow.executions.get(
                lease_snapshot.execution_id,
                workspace_id,
                for_update=True,
            )
            if execution is None:
                return _LeaseReconcileResult(expired=False, retry_queued=False)
            lease = uow.leases.get(lease_id, workspace_id, for_update=True)
            if (
                lease is None
                or lease.status is not LeaseStatus.ACTIVE
                or lease.expires_at > now
            ):
                return _LeaseReconcileResult(expired=False, retry_queued=False)
            if (
                execution.active_lease_id != lease.id
                or execution.status
                not in {ExecutionStatus.LEASED, ExecutionStatus.RUNNING}
            ):
                return _LeaseReconcileResult(expired=False, retry_queued=False)
            worker = uow.workers.get(lease.worker_id, workspace_id, for_update=True)
            if worker is None:
                return _LeaseReconcileResult(expired=False, retry_queued=False)
            task = uow.tasks.get(execution.task_id, workspace_id)
            if task is None:
                return _LeaseReconcileResult(expired=False, retry_queued=False)
            session = uow.sessions.get_active_for_execution(
                execution.id,
                for_update=True,
            )

            previous_lease_version = lease.version
            previous_execution_version = execution.version
            previous_worker_version = worker.version
            previous_session_version = session.version if session is not None else 0
            previous_task_version = task.version
            lease.expire(now=now)
            execution.mark_lost(
                lease_id=lease.id,
                fencing_token=lease.fencing_token,
                failure_code="lease_expired",
                now=now,
            )
            worker.release()
            if session is not None:
                session.close(lost=True, now=now)

            uow.leases.save(lease, previous_lease_version)
            uow.executions.save(execution, previous_execution_version)
            uow.workers.save(worker, previous_worker_version)
            if session is not None:
                uow.sessions.save(session, previous_session_version)

            retry = None
            if execution.attempt < execution.max_attempts:
                retry_delay_seconds = min(2**execution.attempt, 60)
                retry = Execution.create(
                    execution_id=uuid4(),
                    workspace_id=execution.workspace_id,
                    task_id=execution.task_id,
                    lineage_id=execution.lineage_id,
                    attempt=execution.attempt + 1,
                    max_attempts=execution.max_attempts,
                    purpose=execution.purpose,
                    capability_requirements=execution.capability_requirements,
                    pinned_worker_id=execution.pinned_worker_id,
                    not_before=now + timedelta(seconds=retry_delay_seconds),
                )
                uow.executions.add(retry)

            task_changed = False
            task_event_type: str | None = None
            task_previous_status: str | None = None
            if retry is not None and task.status is TaskStatus.RUNNING:
                task_previous_status = task.status.value
                task.mark_ready()
                task_changed = True
                task_event_type = "TaskReady"
            elif retry is None and task.status in {
                TaskStatus.READY,
                TaskStatus.RUNNING,
            } and not uow.executions.has_live_for_task(task.id):
                task_previous_status = task.status.value
                task.wait_for_decision()
                task_changed = True
                task_event_type = "TaskWaitingForDecision"
            if task_changed:
                uow.tasks.save(task, previous_task_version)

            envelope = _system_envelope(
                workspace_id=lease.workspace_id,
                command_type="ReconcileLeaseExpiry",
                target_type="lease",
                target_id=lease.id,
                expected_version=previous_lease_version,
                idempotency_key=f"lease-expired:{lease.id}:v{previous_lease_version}",
                payload={
                    "lease_id": str(lease.id),
                    "execution_id": str(execution.id),
                    "worker_id": str(worker.id),
                    "fencing_token": lease.fencing_token,
                    "retry_execution_id": str(retry.id) if retry is not None else None,
                },
            )
            events = [
                _event(
                    envelope,
                    workspace_id=lease.workspace_id,
                    aggregate_type="lease",
                    aggregate_id=lease.id,
                    aggregate_version=lease.version,
                    event_type="LeaseExpired",
                    payload={
                        "status": lease.status.value,
                        "execution_id": str(execution.id),
                        "worker_id": str(worker.id),
                        "fencing_token": lease.fencing_token,
                    },
                ),
                _event(
                    envelope,
                    workspace_id=execution.workspace_id,
                    aggregate_type="execution",
                    aggregate_id=execution.id,
                    aggregate_version=execution.version,
                    event_type="ExecutionLost",
                    payload={
                        "status": execution.status.value,
                        "failure_code": execution.failure_code,
                        "lease_id": str(lease.id),
                        "fencing_token": lease.fencing_token,
                    },
                ),
                _event(
                    envelope,
                    workspace_id=worker.workspace_id,
                    aggregate_type="worker",
                    aggregate_id=worker.id,
                    aggregate_version=worker.version,
                    event_type="WorkerCapacityReleased",
                    payload={
                        "status": worker.status.value,
                        "desired_state": worker.desired_state.value,
                        "current_load": worker.current_load,
                        "capacity": worker.capacity,
                    },
                ),
            ]
            if session is not None:
                events.append(
                    _event(
                        envelope,
                        workspace_id=session.workspace_id,
                        aggregate_type="execution_session",
                        aggregate_id=session.id,
                        aggregate_version=session.version,
                        event_type="ExecutionSessionLost",
                        payload={
                            "status": session.status.value,
                            "last_sequence": session.last_sequence,
                        },
                    )
                )
            if retry is not None:
                events.append(
                    _event(
                        envelope,
                        workspace_id=retry.workspace_id,
                        aggregate_type="execution",
                        aggregate_id=retry.id,
                        aggregate_version=retry.version,
                        event_type="ExecutionRetryQueued",
                        payload=_execution_payload(retry),
                    )
                )
            if task_changed and task_event_type is not None:
                events.append(
                    _event(
                        envelope,
                        workspace_id=task.workspace_id,
                        aggregate_type="task",
                        aggregate_id=task.id,
                        aggregate_version=task.version,
                        event_type=task_event_type,
                        payload={
                            "status": task.status.value,
                            "previous_status": task_previous_status,
                        },
                    )
                )
            _record_system_operation(
                uow,
                envelope,
                lease.id,
                lease.version,
                tuple(events),
            )
            return _LeaseReconcileResult(
                expired=True,
                retry_queued=retry is not None,
            )


def _system_envelope(
    *,
    workspace_id: UUID,
    command_type: str,
    target_type: str,
    target_id: UUID,
    expected_version: int,
    idempotency_key: str,
    payload: dict[str, Any],
) -> CommandEnvelope:
    return CommandEnvelope(
        id=uuid4(),
        workspace_id=workspace_id,
        command_type=command_type,
        target_type=target_type,
        actor_type="system",
        actor_id="reconciler",
        target_id=target_id,
        payload=payload,
        expected_version=expected_version,
        idempotency_key=idempotency_key,
        correlation_id=uuid4(),
    )


def _record_system_operation(
    uow: ControlPlaneUnitOfWork,
    envelope: CommandEnvelope,
    aggregate_id: UUID,
    aggregate_version: int,
    events: tuple[EventData, ...],
) -> None:
    result = CommandResult(
        command_id=envelope.id,
        aggregate_id=aggregate_id,
        aggregate_version=aggregate_version,
        event_ids=tuple(event.id for event in events),
    )
    uow.commands.record_applied(envelope, hash_request(envelope), result)
    for event in events:
        uow.events.append_with_outbox(event)


def _event(
    envelope: CommandEnvelope,
    *,
    workspace_id: UUID,
    aggregate_type: str,
    aggregate_id: UUID,
    aggregate_version: int,
    event_type: str,
    payload: dict[str, Any],
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


def _execution_payload(execution: Execution) -> dict[str, object]:
    return {
        "task_id": str(execution.task_id),
        "lineage_id": str(execution.lineage_id),
        "attempt": execution.attempt,
        "max_attempts": execution.max_attempts,
        "purpose": execution.purpose,
        "status": execution.status.value,
        "capability_requirements": sorted(execution.capability_requirements),
        "pinned_worker_id": (
            str(execution.pinned_worker_id)
            if execution.pinned_worker_id is not None
            else None
        ),
        "not_before": execution.not_before.isoformat(),
    }
