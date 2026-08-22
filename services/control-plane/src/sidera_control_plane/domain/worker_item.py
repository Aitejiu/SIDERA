from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sidera_control_plane.domain.errors import InvalidTransition, ValidationError


class WorkerItemStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    RETIRED = "retired"


@dataclass(slots=True)
class WorkerItem:
    id: UUID
    workspace_id: UUID
    root_task_id: UUID
    stable_key: str
    title: str
    responsibility: str
    status: WorkerItemStatus
    context_version: int
    acknowledged_context_version: int
    derived_from_worker_item_ids: tuple[UUID, ...]
    version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def create(
        cls,
        *,
        item_id: UUID,
        workspace_id: UUID,
        root_task_id: UUID,
        title: str,
        responsibility: str,
        now: datetime,
        stable_key: str | None = None,
        derived_from_worker_item_ids: tuple[UUID, ...] = (),
    ) -> "WorkerItem":
        clean_title = title.strip()
        clean_responsibility = responsibility.strip()
        clean_stable_key = (stable_key or f"worker-item:{item_id}").strip()
        if not clean_title or not clean_responsibility:
            raise ValidationError("title and responsibility are required")
        if not clean_stable_key:
            raise ValidationError("stable_key is required")
        if now.tzinfo is None:
            raise ValidationError("now must be timezone-aware")
        if len(set(derived_from_worker_item_ids)) != len(derived_from_worker_item_ids):
            raise ValidationError("derived_from worker item IDs must be unique")
        return cls(
            id=item_id,
            workspace_id=workspace_id,
            root_task_id=root_task_id,
            stable_key=clean_stable_key,
            title=clean_title,
            responsibility=clean_responsibility,
            status=WorkerItemStatus.ACTIVE,
            context_version=1,
            acknowledged_context_version=1,
            derived_from_worker_item_ids=tuple(derived_from_worker_item_ids),
            version=1,
            created_at=now,
            updated_at=now,
        )

    @property
    def context_is_synchronized(self) -> bool:
        return self.context_version == self.acknowledged_context_version

    def _assert_not_retired(self) -> None:
        if self.status is WorkerItemStatus.RETIRED:
            raise InvalidTransition("retired WorkerItem is read-only")

    def _touch(self, now: datetime) -> None:
        if now.tzinfo is None:
            raise ValidationError("now must be timezone-aware")
        self.version += 1
        self.updated_at = now

    def pause(self, *, now: datetime) -> None:
        self._assert_not_retired()
        if self.status is WorkerItemStatus.PAUSED:
            raise InvalidTransition("WorkerItem is already paused")
        self.status = WorkerItemStatus.PAUSED
        self._touch(now)

    def resume(self, *, now: datetime) -> None:
        if self.status is not WorkerItemStatus.PAUSED:
            raise InvalidTransition("only a paused WorkerItem can resume")
        self.status = WorkerItemStatus.ACTIVE
        self._touch(now)

    def retire(self, *, now: datetime) -> None:
        self._assert_not_retired()
        self.status = WorkerItemStatus.RETIRED
        self._touch(now)

    def advance_context(self, *, now: datetime) -> None:
        self._assert_not_retired()
        self.context_version += 1
        self._touch(now)

    def acknowledge_context(
        self, *, expected_context_version: int, now: datetime
    ) -> None:
        self._assert_not_retired()
        if expected_context_version != self.context_version:
            raise ValidationError(
                "context version does not match the current WorkerItem context"
            )
        if self.context_is_synchronized:
            return
        self.acknowledged_context_version = expected_context_version
        self._touch(now)
