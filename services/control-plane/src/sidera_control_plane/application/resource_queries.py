from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sidera_control_plane.application.errors import NotFound


@dataclass(frozen=True, slots=True)
class WorkerView:
    id: UUID
    workspace_id: UUID
    adapter_type: str
    display_name: str
    status: str
    capabilities: tuple[str, ...]
    capacity: int
    current_load: int
    cost_per_hour_microunits: int
    startup_latency_ms: int
    last_heartbeat_at: datetime
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ExecutionView:
    id: UUID
    workspace_id: UUID
    task_id: UUID
    lineage_id: UUID
    attempt: int
    max_attempts: int
    purpose: str
    status: str
    capability_requirements: tuple[str, ...]
    pinned_worker_id: UUID | None
    assigned_worker_id: UUID | None
    active_lease_id: UUID | None
    last_fencing_token: int
    not_before: datetime
    failure_code: str | None
    started_at: datetime | None
    finished_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class LeaseView:
    id: UUID
    execution_id: UUID
    worker_id: UUID
    status: str
    issued_at: datetime
    expires_at: datetime
    renewed_at: datetime | None
    ended_at: datetime | None
    fencing_token: int
    assignment_reason: dict[str, object]
    version: int


class ResourceQueryStore(Protocol):
    def list_workers(self, workspace_id: UUID) -> tuple[WorkerView, ...]:
        raise NotImplementedError

    def get_worker(self, workspace_id: UUID, worker_id: UUID) -> WorkerView | None:
        raise NotImplementedError

    def list_executions(
        self, workspace_id: UUID, task_id: UUID | None
    ) -> tuple[ExecutionView, ...]:
        raise NotImplementedError

    def get_execution(
        self, workspace_id: UUID, execution_id: UUID
    ) -> ExecutionView | None:
        raise NotImplementedError

    def list_leases(
        self, workspace_id: UUID, execution_id: UUID
    ) -> tuple[LeaseView, ...]:
        raise NotImplementedError


class ResourceQueryService:
    def __init__(self, store: ResourceQueryStore) -> None:
        self._store = store

    def list_workers(self, workspace_id: UUID) -> tuple[WorkerView, ...]:
        return self._store.list_workers(workspace_id)

    def get_worker(self, workspace_id: UUID, worker_id: UUID) -> WorkerView:
        worker = self._store.get_worker(workspace_id, worker_id)
        if worker is None:
            raise NotFound("worker not found")
        return worker

    def list_executions(
        self, workspace_id: UUID, task_id: UUID | None = None
    ) -> tuple[ExecutionView, ...]:
        return self._store.list_executions(workspace_id, task_id)

    def get_execution(self, workspace_id: UUID, execution_id: UUID) -> ExecutionView:
        execution = self._store.get_execution(workspace_id, execution_id)
        if execution is None:
            raise NotFound("execution not found")
        return execution

    def list_leases(
        self, workspace_id: UUID, execution_id: UUID
    ) -> tuple[LeaseView, ...]:
        self.get_execution(workspace_id, execution_id)
        return self._store.list_leases(workspace_id, execution_id)
