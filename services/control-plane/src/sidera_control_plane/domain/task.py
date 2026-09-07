from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from sidera_control_plane.domain.errors import InvalidTransition, ValidationError


class TaskStatus(StrEnum):
    PENDING = "pending"
    BLOCKED = "blocked"
    READY = "ready"
    RUNNING = "running"
    WAITING = "waiting"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


TERMINAL_TASK_STATUSES = {
    TaskStatus.SUCCEEDED,
    TaskStatus.FAILED,
    TaskStatus.CANCELED,
}


@dataclass(slots=True)
class Task:
    id: UUID
    workspace_id: UUID
    title: str
    description: str
    acceptance_criteria: tuple[str, ...]
    status: TaskStatus
    priority: int
    requested_by: str
    version: int

    @classmethod
    def create(
        cls,
        *,
        task_id: UUID,
        workspace_id: UUID,
        title: str,
        description: str,
        acceptance_criteria: tuple[str, ...],
        priority: int,
        requested_by: str,
    ) -> "Task":
        clean_title = title.strip()
        clean_criteria = tuple(item.strip() for item in acceptance_criteria if item.strip())
        if not clean_title or not clean_criteria:
            raise ValidationError("title and acceptance_criteria are required")
        cls._validate_priority(priority)
        return cls(
            id=task_id,
            workspace_id=workspace_id,
            title=clean_title,
            description=description.strip(),
            acceptance_criteria=clean_criteria,
            status=TaskStatus.PENDING,
            priority=priority,
            requested_by=requested_by,
            version=1,
        )

    @staticmethod
    def _validate_priority(priority: int) -> None:
        if not 0 <= priority <= 100:
            raise ValidationError("priority must be between 0 and 100")

    def _assert_mutable(self) -> None:
        if self.status in TERMINAL_TASK_STATUSES:
            raise InvalidTransition(f"task is terminal: {self.status}")

    def set_priority(self, priority: int) -> None:
        self._assert_mutable()
        self._validate_priority(priority)
        self.priority = priority
        self.version += 1

    def update_specification(
        self,
        *,
        title: str,
        description: str,
        acceptance_criteria: tuple[str, ...],
    ) -> None:
        self._assert_mutable()
        clean_title = title.strip()
        clean_criteria = tuple(item.strip() for item in acceptance_criteria if item.strip())
        if not clean_title or not clean_criteria:
            raise ValidationError("title and acceptance_criteria are required")
        if (
            self.title == clean_title
            and self.description == description.strip()
            and self.acceptance_criteria == clean_criteria
        ):
            return
        self.title = clean_title
        self.description = description.strip()
        self.acceptance_criteria = clean_criteria
        self.version += 1

    def pause(self) -> None:
        self._assert_mutable()
        if self.status is TaskStatus.PAUSED:
            raise InvalidTransition("task is already paused")
        self.status = TaskStatus.PAUSED
        self.version += 1

    def resume(self) -> None:
        if self.status is not TaskStatus.PAUSED:
            raise InvalidTransition("only a paused task can resume")
        self.status = TaskStatus.PENDING
        self.version += 1

    def cancel(self) -> None:
        self._assert_mutable()
        self.status = TaskStatus.CANCELED
        self.version += 1

    def mark_ready(self) -> None:
        self._assert_mutable()
        if self.status not in {
            TaskStatus.PENDING,
            TaskStatus.WAITING,
            TaskStatus.RUNNING,
        }:
            raise InvalidTransition(f"task cannot become ready from {self.status}")
        self.status = TaskStatus.READY
        self.version += 1

    def mark_running(self) -> None:
        self._assert_mutable()
        if self.status not in {TaskStatus.READY, TaskStatus.RUNNING}:
            raise InvalidTransition(f"task cannot run from {self.status}")
        if self.status is not TaskStatus.RUNNING:
            self.status = TaskStatus.RUNNING
            self.version += 1

    def wait_for_decision(self) -> None:
        self._assert_mutable()
        if self.status not in {TaskStatus.READY, TaskStatus.RUNNING}:
            raise InvalidTransition(f"task cannot wait from {self.status}")
        self.status = TaskStatus.WAITING
        self.version += 1

    def await_acceptance(self) -> None:
        self._assert_mutable()
        if self.status is TaskStatus.WAITING:
            return
        if self.status not in {TaskStatus.PENDING, TaskStatus.READY, TaskStatus.RUNNING}:
            raise InvalidTransition(f"task cannot await acceptance from {self.status}")
        self.status = TaskStatus.WAITING
        self.version += 1

    def complete(self) -> None:
        self._assert_mutable()
        if self.status is not TaskStatus.WAITING:
            raise InvalidTransition("only a waiting task can complete")
        self.status = TaskStatus.SUCCEEDED
        self.version += 1
