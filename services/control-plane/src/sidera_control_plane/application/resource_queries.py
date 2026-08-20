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


class ResourceQueryStore(Protocol):
    def list_workers(self, workspace_id: UUID) -> tuple[WorkerView, ...]:
        raise NotImplementedError

    def get_worker(self, workspace_id: UUID, worker_id: UUID) -> WorkerView | None:
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
