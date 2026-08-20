from contextlib import AbstractContextManager
from datetime import datetime
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from sidera_control_plane.domain.commands import (
    CommandEnvelope,
    CommandResult,
    StoredCommand,
)
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.execution import Execution, ExecutionSession
from sidera_control_plane.domain.lease import AssignmentLease
from sidera_control_plane.domain.task import Task
from sidera_control_plane.domain.worker import Worker


class TaskRepositoryPort(Protocol):
    def get(self, task_id: UUID, workspace_id: UUID) -> Task | None:
        raise NotImplementedError

    def workspace_exists(self, workspace_id: UUID) -> bool:
        raise NotImplementedError

    def add(self, task: Task) -> None:
        raise NotImplementedError

    def save(self, task: Task, previous_version: int) -> None:
        raise NotImplementedError


class WorkerRepositoryPort(Protocol):
    def get(
        self, worker_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> Worker | None:
        raise NotImplementedError

    def add(self, worker: Worker) -> None:
        raise NotImplementedError

    def save(self, worker: Worker, previous_version: int) -> None:
        raise NotImplementedError

    def lock_candidates(
        self,
        *,
        workspace_id: UUID,
        required_capabilities: frozenset[str],
        pinned_worker_id: UUID | None,
        heartbeat_cutoff: datetime,
    ) -> tuple[Worker, ...]:
        raise NotImplementedError

    def claim_stale(
        self, *, heartbeat_cutoff: datetime, limit: int
    ) -> tuple[Worker, ...]:
        raise NotImplementedError


class ExecutionRepositoryPort(Protocol):
    def get(
        self,
        execution_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> Execution | None:
        raise NotImplementedError

    def add(self, execution: Execution) -> None:
        raise NotImplementedError

    def save(self, execution: Execution, previous_version: int) -> None:
        raise NotImplementedError

    def attempt_exists(self, lineage_id: UUID, attempt: int) -> bool:
        raise NotImplementedError

    def claim_schedulable(
        self, *, now: datetime, limit: int
    ) -> tuple[Execution, ...]:
        raise NotImplementedError

    def has_live_for_task(
        self, task_id: UUID, *, excluding: UUID | None = None
    ) -> bool:
        raise NotImplementedError


class LeaseStorePort(Protocol):
    def get(
        self, lease_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> AssignmentLease | None:
        raise NotImplementedError

    def add(self, lease: AssignmentLease) -> None:
        raise NotImplementedError

    def save(self, lease: AssignmentLease, previous_version: int) -> None:
        raise NotImplementedError

    def claim_expired(
        self, *, now: datetime, limit: int
    ) -> tuple[AssignmentLease, ...]:
        raise NotImplementedError


class SessionRepositoryPort(Protocol):
    def get_active_for_execution(
        self, execution_id: UUID, *, for_update: bool = False
    ) -> ExecutionSession | None:
        raise NotImplementedError

    def add(self, session: ExecutionSession) -> None:
        raise NotImplementedError

    def save(self, session: ExecutionSession, previous_version: int) -> None:
        raise NotImplementedError


class CommandRepositoryPort(Protocol):
    def find_duplicate(
        self,
        workspace_id: UUID,
        actor_type: str,
        actor_id: str,
        idempotency_key: str,
    ) -> StoredCommand | None:
        raise NotImplementedError

    def record_applied(
        self,
        envelope: CommandEnvelope,
        request_hash: str,
        result: CommandResult,
    ) -> None:
        raise NotImplementedError

    def record_rejected(
        self,
        envelope: CommandEnvelope,
        request_hash: str,
        error_code: str,
        error_result: dict[str, object],
    ) -> None:
        raise NotImplementedError


class EventStorePort(Protocol):
    def append_with_outbox(self, event: EventData) -> None:
        raise NotImplementedError


class ControlPlaneUnitOfWork(Protocol):
    tasks: TaskRepositoryPort
    workers: WorkerRepositoryPort
    executions: ExecutionRepositoryPort
    leases: LeaseStorePort
    sessions: SessionRepositoryPort
    commands: CommandRepositoryPort
    events: EventStorePort

    def savepoint(self) -> AbstractContextManager[None]:
        raise NotImplementedError

    def __enter__(self) -> Self:
        raise NotImplementedError

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        raise NotImplementedError
