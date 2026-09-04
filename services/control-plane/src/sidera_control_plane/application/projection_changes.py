from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from sidera_control_plane.domain.projections import ProjectionChange
from sidera_control_plane.persistence.projection_change_repository import (
    ProjectionChangeRepository,
)


class ProjectionChangePublisher:
    def __init__(
        self,
        repository: ProjectionChangeRepository,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._clock = clock or (lambda: datetime.now(UTC))
        self._published: dict[tuple[object, ...], ProjectionChange] = {}

    def changed(
        self,
        *,
        workspace_id: UUID,
        projection_type: str,
        resource_id: UUID | None,
        change_type: str,
        resource_version: int | None,
        correlation_id: UUID,
    ) -> ProjectionChange:
        key = (
            workspace_id,
            projection_type,
            resource_id,
            change_type,
            resource_version,
            correlation_id if resource_id is None else None,
        )
        existing = self._published.get(key)
        if existing is not None:
            return existing
        change = self._repository.append(
            workspace_id=workspace_id,
            schema_version=1,
            projection_type=projection_type,
            resource_id=resource_id,
            change_type=change_type,
            resource_version=resource_version,
            occurred_at=self._clock(),
            correlation_id=correlation_id,
        )
        self._published[key] = change
        return change

    def latest_cursor(self, workspace_id: UUID) -> int:
        return self._repository.latest_cursor(workspace_id)

    def latest_target_cursor(
        self, workspace_id: UUID, projection_type: str, resource_id: UUID
    ) -> int:
        return self._repository.latest_target_cursor(
            workspace_id, projection_type, resource_id
        )

    def oldest_cursor(self, workspace_id: UUID) -> int | None:
        return self._repository.oldest_cursor(workspace_id)
