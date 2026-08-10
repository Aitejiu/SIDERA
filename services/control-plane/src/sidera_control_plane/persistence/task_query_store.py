from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.engine import Row

from sidera_control_plane.application.task_queries import TaskView, TimelineItem
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import EventModel, TaskModel


class SqlAlchemyTaskQueryStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    def list_tasks(
        self, workspace_id: UUID, limit: int, offset: int
    ) -> tuple[tuple[TaskView, ...], int]:
        with self._database.session() as session:
            total = session.scalar(
                select(func.count())
                .select_from(TaskModel)
                .where(TaskModel.workspace_id == workspace_id)
            )
            rows = session.execute(
                _task_columns()
                .where(TaskModel.workspace_id == workspace_id)
                .order_by(TaskModel.created_at.desc(), TaskModel.id.desc())
                .limit(limit)
                .offset(offset)
            ).all()
        return tuple(_task_view(row) for row in rows), total or 0

    def get_task(self, workspace_id: UUID, task_id: UUID) -> TaskView | None:
        with self._database.session() as session:
            row = session.execute(
                _task_columns().where(
                    TaskModel.workspace_id == workspace_id,
                    TaskModel.id == task_id,
                )
            ).one_or_none()
        return _task_view(row) if row is not None else None

    def get_timeline(
        self, workspace_id: UUID, task_id: UUID
    ) -> tuple[TimelineItem, ...]:
        with self._database.session() as session:
            rows = session.execute(
                select(
                    EventModel.id,
                    EventModel.event_type,
                    EventModel.aggregate_version,
                    EventModel.actor_type,
                    EventModel.actor_id,
                    EventModel.payload,
                    EventModel.causation_id,
                    EventModel.correlation_id,
                    EventModel.created_at,
                )
                .where(
                    EventModel.workspace_id == workspace_id,
                    EventModel.aggregate_type == "task",
                    EventModel.aggregate_id == task_id,
                )
                .order_by(EventModel.created_at.asc(), EventModel.id.asc())
            ).all()
        return tuple(_timeline_item(row) for row in rows)


def _task_columns():
    return select(
        TaskModel.id,
        TaskModel.workspace_id,
        TaskModel.title,
        TaskModel.description,
        TaskModel.acceptance_criteria,
        TaskModel.status,
        TaskModel.priority,
        TaskModel.requested_by,
        TaskModel.version,
        TaskModel.created_at,
        TaskModel.updated_at,
    )


def _task_view(row: Row[Any]) -> TaskView:
    return TaskView(
        id=row.id,
        workspace_id=row.workspace_id,
        title=row.title,
        description=row.description,
        acceptance_criteria=tuple(row.acceptance_criteria),
        status=row.status,
        priority=row.priority,
        requested_by=row.requested_by,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _timeline_item(row: Row[Any]) -> TimelineItem:
    return TimelineItem(
        id=row.id,
        event_type=row.event_type,
        aggregate_version=row.aggregate_version,
        actor_type=row.actor_type,
        actor_id=row.actor_id,
        payload=dict(row.payload),
        causation_id=row.causation_id,
        correlation_id=row.correlation_id,
        created_at=row.created_at,
    )
