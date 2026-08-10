from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from sidera_control_plane.application.errors import NotFound


@dataclass(frozen=True, slots=True)
class TaskView:
    id: UUID
    workspace_id: UUID
    title: str
    description: str
    acceptance_criteria: tuple[str, ...]
    status: str
    priority: int
    requested_by: str
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class TaskPage:
    items: tuple[TaskView, ...]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True, slots=True)
class TimelineItem:
    id: UUID
    event_type: str
    aggregate_version: int
    actor_type: str
    actor_id: str
    payload: dict[str, Any]
    causation_id: UUID
    correlation_id: UUID
    created_at: datetime


class TaskQueryStore(Protocol):
    def list_tasks(
        self, workspace_id: UUID, limit: int, offset: int
    ) -> tuple[tuple[TaskView, ...], int]: ...

    def get_task(self, workspace_id: UUID, task_id: UUID) -> TaskView | None: ...

    def get_timeline(
        self, workspace_id: UUID, task_id: UUID
    ) -> tuple[TimelineItem, ...]: ...


class TaskQueryService:
    def __init__(self, store: TaskQueryStore) -> None:
        self._store = store

    def list_tasks(self, workspace_id: UUID, limit: int, offset: int) -> TaskPage:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if not 0 <= offset <= 10_000:
            raise ValueError("offset must be between 0 and 10000")
        items, total = self._store.list_tasks(workspace_id, limit, offset)
        return TaskPage(items=items, total=total, limit=limit, offset=offset)

    def get_task(self, workspace_id: UUID, task_id: UUID) -> TaskView:
        task = self._store.get_task(workspace_id, task_id)
        if task is None:
            raise NotFound("task not found")
        return task

    def get_timeline(
        self, workspace_id: UUID, task_id: UUID
    ) -> tuple[TimelineItem, ...]:
        if self._store.get_task(workspace_id, task_id) is None:
            raise NotFound("task not found")
        return self._store.get_timeline(workspace_id, task_id)
