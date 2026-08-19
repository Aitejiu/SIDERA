from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from sidera_control_plane.domain.commands import (
    CommandEnvelope,
    CommandResult,
    StoredCommand,
)
from sidera_control_plane.domain.errors import VersionConflict
from sidera_control_plane.domain.events import EventData
from sidera_control_plane.domain.task import Task, TaskStatus
from sidera_control_plane.persistence.models import (
    CommandModel,
    EventModel,
    OutboxMessageModel,
    TaskModel,
    WorkspaceModel,
)


class TaskRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, task_id: UUID, workspace_id: UUID) -> Task | None:
        row = self._session.scalar(
            select(TaskModel).where(
                TaskModel.id == task_id,
                TaskModel.workspace_id == workspace_id,
            )
        )
        return _to_domain_task(row) if row is not None else None

    def workspace_exists(self, workspace_id: UUID) -> bool:
        return (
            self._session.scalar(
                select(WorkspaceModel.id).where(WorkspaceModel.id == workspace_id)
            )
            is not None
        )

    def add(self, task: Task) -> None:
        self._session.add(
            TaskModel(
                id=task.id,
                workspace_id=task.workspace_id,
                title=task.title,
                description=task.description,
                acceptance_criteria=list(task.acceptance_criteria),
                status=task.status.value,
                priority=task.priority,
                requested_by=task.requested_by,
                version=task.version,
            )
        )

    def save(self, task: Task, previous_version: int) -> None:
        statement = (
            update(TaskModel)
            .where(
                TaskModel.id == task.id,
                TaskModel.workspace_id == task.workspace_id,
                TaskModel.version == previous_version,
            )
            .values(
                status=task.status.value,
                priority=task.priority,
                version=task.version,
                updated_at=datetime.now(UTC),
            )
        )
        result = self._session.execute(statement)
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            actual = self.current_version(task.id, task.workspace_id)
            raise VersionConflict(previous_version, actual)

    def current_version(self, task_id: UUID, workspace_id: UUID) -> int:
        version = self._session.scalar(
            select(TaskModel.version).where(
                TaskModel.id == task_id,
                TaskModel.workspace_id == workspace_id,
            )
        )
        return version if version is not None else 0


class CommandRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def find_duplicate(
        self,
        workspace_id: UUID,
        actor_type: str,
        actor_id: str,
        idempotency_key: str,
    ) -> StoredCommand | None:
        row = self._session.scalar(
            select(CommandModel).where(
                CommandModel.workspace_id == workspace_id,
                CommandModel.actor_type == actor_type,
                CommandModel.actor_id == actor_id,
                CommandModel.idempotency_key == idempotency_key,
            )
        )
        if row is None:
            return None
        return StoredCommand(
            id=row.id,
            request_hash=row.request_hash,
            status=row.status,
            result=dict(row.result or {}),
            rejection_reason=row.rejection_reason,
        )

    def record_applied(
        self,
        envelope: CommandEnvelope,
        request_hash: str,
        result: CommandResult,
    ) -> None:
        self._session.add(
            _command_model(
                envelope,
                request_hash=request_hash,
                status="applied",
                result={
                    "command_id": str(result.command_id),
                    "aggregate_id": str(result.aggregate_id),
                    "aggregate_version": result.aggregate_version,
                    "event_ids": [str(event_id) for event_id in result.event_ids],
                },
                rejection_reason=None,
            )
        )

    def record_rejected(
        self,
        envelope: CommandEnvelope,
        request_hash: str,
        error_code: str,
        error_result: dict[str, object],
    ) -> None:
        self._session.add(
            _command_model(
                envelope,
                request_hash=request_hash,
                status="rejected",
                result=error_result,
                rejection_reason=error_code[:100],
            )
        )


class EventRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def append_with_outbox(self, event: EventData) -> None:
        self._session.add(
            EventModel(
                id=event.id,
                workspace_id=event.workspace_id,
                aggregate_type=event.aggregate_type,
                aggregate_id=event.aggregate_id,
                aggregate_version=event.aggregate_version,
                event_type=event.event_type,
                payload=event.payload,
                actor_type=event.actor_type,
                actor_id=event.actor_id,
                causation_id=event.causation_id,
                correlation_id=event.correlation_id,
            )
        )
        self._session.add(
            OutboxMessageModel(
                id=uuid4(),
                event_id=event.id,
                topic=f"{event.aggregate_type}.{event.event_type}",
                payload={
                    "event_id": str(event.id),
                    "workspace_id": str(event.workspace_id),
                    "aggregate_type": event.aggregate_type,
                    "aggregate_id": str(event.aggregate_id),
                    "aggregate_version": event.aggregate_version,
                    "event_type": event.event_type,
                    "data": event.payload,
                    "correlation_id": str(event.correlation_id),
                },
            )
        )


def _to_domain_task(row: TaskModel) -> Task:
    return Task(
        id=row.id,
        workspace_id=row.workspace_id,
        title=row.title,
        description=row.description,
        acceptance_criteria=tuple(row.acceptance_criteria),
        status=TaskStatus(row.status),
        priority=row.priority,
        requested_by=row.requested_by,
        version=row.version,
    )


def _command_model(
    envelope: CommandEnvelope,
    *,
    request_hash: str,
    status: str,
    result: dict[str, object],
    rejection_reason: str | None,
) -> CommandModel:
    return CommandModel(
        id=envelope.id,
        workspace_id=envelope.workspace_id,
        command_type=envelope.command_type,
        actor_type=envelope.actor_type,
        actor_id=envelope.actor_id,
        target_type=envelope.target_type,
        target_id=envelope.target_id,
        payload=envelope.payload,
        expected_version=envelope.expected_version,
        idempotency_key=envelope.idempotency_key,
        request_hash=request_hash,
        correlation_id=envelope.correlation_id,
        status=status,
        result=result,
        rejection_reason=rejection_reason,
    )
