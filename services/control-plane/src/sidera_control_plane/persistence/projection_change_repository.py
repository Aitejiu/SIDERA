from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from sidera_control_plane.domain.projections import ProjectionChange
from sidera_control_plane.persistence.models import (
    ProjectionChangeModel,
    WorkspaceProjectionCursorModel,
)


class ProjectionChangeRepository:
    def __init__(self, session: Session, *, retention_seconds: int = 604_800) -> None:
        if retention_seconds <= 0:
            raise ValueError("retention_seconds must be positive")
        self._session = session
        self._retention_seconds = retention_seconds

    def append(
        self,
        *,
        workspace_id: UUID,
        schema_version: int,
        projection_type: str,
        resource_id: UUID | None,
        change_type: str,
        resource_version: int | None,
        occurred_at: datetime,
        correlation_id: UUID,
    ) -> ProjectionChange:
        if occurred_at.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware")
        with self._session.no_autoflush:
            cursor = self._allocate_cursor(workspace_id)
            self._session.execute(
                insert(ProjectionChangeModel).values(
                    workspace_id=workspace_id,
                    cursor=cursor,
                    schema_version=schema_version,
                    projection_type=projection_type,
                    resource_id=resource_id,
                    change_type=change_type,
                    resource_version=resource_version,
                    occurred_at=occurred_at,
                    correlation_id=correlation_id,
                )
            )
            if cursor % 128 == 0:
                self._prune(workspace_id, occurred_at)
        return ProjectionChange(
            workspace_id=workspace_id,
            cursor=cursor,
            schema_version=schema_version,
            projection_type=projection_type,
            resource_id=resource_id,
            change_type=change_type,
            resource_version=resource_version,
            occurred_at=occurred_at,
            correlation_id=correlation_id,
        )

    def list_after(
        self, workspace_id: UUID, *, cursor: int, limit: int
    ) -> tuple[ProjectionChange, ...]:
        if limit < 1:
            raise ValueError("limit must be positive")
        rows = self._session.scalars(
            select(ProjectionChangeModel)
            .where(
                ProjectionChangeModel.workspace_id == workspace_id,
                ProjectionChangeModel.cursor > cursor,
            )
            .order_by(ProjectionChangeModel.cursor.asc())
            .limit(limit)
        ).all()
        return tuple(_to_domain(row) for row in rows)

    def latest_cursor(self, workspace_id: UUID) -> int:
        cursor_table_value = self._session.scalar(
            select(WorkspaceProjectionCursorModel.last_cursor).where(
                WorkspaceProjectionCursorModel.workspace_id == workspace_id
            )
        ) or 0
        projection_value = self._session.scalar(
            select(func.max(ProjectionChangeModel.cursor)).where(
                ProjectionChangeModel.workspace_id == workspace_id
            )
        ) or 0
        return max(int(cursor_table_value), int(projection_value))

    def cursor_expired(self, workspace_id: UUID, cursor: int) -> bool:
        latest = self.latest_cursor(workspace_id)
        if latest == 0 or cursor >= latest:
            return False
        next_cursor = self._session.scalar(
            select(func.min(ProjectionChangeModel.cursor)).where(
                ProjectionChangeModel.workspace_id == workspace_id,
                ProjectionChangeModel.cursor > cursor,
            )
        )
        return next_cursor is None or next_cursor != cursor + 1

    def _allocate_cursor(self, workspace_id: UUID) -> int:
        latest_projection_cursor = self._session.scalar(
            select(func.max(ProjectionChangeModel.cursor)).where(
                ProjectionChangeModel.workspace_id == workspace_id
            )
        ) or 0
        statement = (
            insert(WorkspaceProjectionCursorModel)
            .values(
                workspace_id=workspace_id,
                last_cursor=int(latest_projection_cursor) + 1,
            )
            .on_conflict_do_update(
                index_elements=[WorkspaceProjectionCursorModel.workspace_id],
                set_={
                    "last_cursor": func.greatest(
                        WorkspaceProjectionCursorModel.last_cursor,
                        int(latest_projection_cursor),
                    )
                    + 1
                },
            )
            .returning(WorkspaceProjectionCursorModel.last_cursor)
        )
        cursor = self._session.scalar(statement)
        if cursor is None:
            raise RuntimeError("projection cursor allocation returned no cursor")
        return int(cursor)

    def _prune(self, workspace_id: UUID, occurred_at: datetime) -> None:
        cutoff = occurred_at - timedelta(seconds=self._retention_seconds)
        candidates = self._session.scalars(
            select(ProjectionChangeModel.cursor)
            .where(
                ProjectionChangeModel.workspace_id == workspace_id,
                ProjectionChangeModel.occurred_at < cutoff,
            )
            .order_by(ProjectionChangeModel.cursor.asc())
            .limit(1_000)
        ).all()
        if candidates:
            self._session.execute(
                delete(ProjectionChangeModel).where(
                    ProjectionChangeModel.workspace_id == workspace_id,
                    ProjectionChangeModel.cursor.in_(candidates),
                )
            )


def _to_domain(row: ProjectionChangeModel) -> ProjectionChange:
    return ProjectionChange(
        workspace_id=row.workspace_id,
        cursor=row.cursor,
        schema_version=row.schema_version,
        projection_type=row.projection_type,
        resource_id=row.resource_id,
        change_type=row.change_type,
        resource_version=row.resource_version,
        occurred_at=row.occurred_at,
        correlation_id=row.correlation_id,
    )
