from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from sidera_control_plane.persistence.models import (
    ExecutionModel,
    ExecutionSessionModel,
    WorkerItemModel,
)


@dataclass(frozen=True, slots=True)
class ProjectionScope:
    worker_item_id: UUID | None
    root_task_id: UUID | None


def resolve_worker_item_scope(
    session: Session,
    workspace_id: UUID,
    *,
    execution_id: UUID | None = None,
    session_id: UUID | None = None,
    worker_item_id: UUID | None = None,
    root_task_id: UUID | None = None,
) -> ProjectionScope:
    # Event persistence happens before the command record is added. Keep these
    # lookups read-only with respect to the current unit of work so they cannot
    # flush an event whose causation command is still pending.
    with session.no_autoflush:
        if execution_id is None and session_id is not None:
            execution_session = _pending_session(
                session, workspace_id, session_id
            ) or session.scalar(
                select(ExecutionSessionModel).where(
                    ExecutionSessionModel.workspace_id == workspace_id,
                    ExecutionSessionModel.id == session_id,
                )
            )
            if execution_session is not None:
                execution_id = execution_session.execution_id
        if worker_item_id is None and execution_id is not None:
            execution = _pending_execution(
                session, workspace_id, execution_id
            ) or session.scalar(
                select(ExecutionModel).where(
                    ExecutionModel.workspace_id == workspace_id,
                    ExecutionModel.id == execution_id,
                )
            )
            if execution is not None:
                worker_item_id = execution.worker_item_id
        if root_task_id is None and worker_item_id is not None:
            worker_item = _pending_worker_item(
                session, workspace_id, worker_item_id
            ) or session.scalar(
                select(WorkerItemModel).where(
                    WorkerItemModel.workspace_id == workspace_id,
                    WorkerItemModel.id == worker_item_id,
                )
            )
            if worker_item is not None:
                root_task_id = worker_item.root_task_id
    return ProjectionScope(worker_item_id, root_task_id)


def _pending_session(
    session: Session, workspace_id: UUID, session_id: UUID
) -> ExecutionSessionModel | None:
    for instance in session.new:
        if (
            isinstance(instance, ExecutionSessionModel)
            and instance.workspace_id == workspace_id
            and instance.id == session_id
        ):
            return instance
    return None


def _pending_execution(
    session: Session, workspace_id: UUID, execution_id: UUID
) -> ExecutionModel | None:
    for instance in session.new:
        if (
            isinstance(instance, ExecutionModel)
            and instance.workspace_id == workspace_id
            and instance.id == execution_id
        ):
            return instance
    return None


def _pending_worker_item(
    session: Session, workspace_id: UUID, worker_item_id: UUID
) -> WorkerItemModel | None:
    for instance in session.new:
        if (
            isinstance(instance, WorkerItemModel)
            and instance.workspace_id == workspace_id
            and instance.id == worker_item_id
        ):
            return instance
    return None
