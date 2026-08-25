from collections.abc import Collection
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sidera_control_plane.application.ports import ProjectionChangePublisherPort
from sidera_control_plane.application.projection_invalidation import (
    projection_targets_for_event,
)
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
from sidera_control_plane.persistence.projection_scope import (
    resolve_worker_item_scope,
)


class TaskRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(
        self, task_id: UUID, workspace_id: UUID, *, for_update: bool = False
    ) -> Task | None:
        statement = select(TaskModel).where(
            TaskModel.id == task_id,
            TaskModel.workspace_id == workspace_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_domain_task(row) if row is not None else None

    def get_many(
        self, task_ids: Collection[UUID], workspace_id: UUID
    ) -> tuple[Task, ...]:
        ordered_ids = tuple(dict.fromkeys(task_ids))
        if not ordered_ids:
            return ()
        rows = self._session.scalars(
            select(TaskModel).where(
                TaskModel.id.in_(ordered_ids),
                TaskModel.workspace_id == workspace_id,
            )
        ).all()
        tasks_by_id = {row.id: _to_domain_task(row) for row in rows}
        return tuple(tasks_by_id[task_id] for task_id in ordered_ids if task_id in tasks_by_id)

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


class ConcurrentCommandCommit(RuntimeError):
    def __init__(self, constraint_name: str) -> None:
        super().__init__(constraint_name)
        self.constraint_name = constraint_name


class CommandRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def find_by_id(
        self, workspace_id: UUID, command_id: UUID
    ) -> StoredCommand | None:
        row = self._session.scalar(
            select(CommandModel).where(
                CommandModel.workspace_id == workspace_id,
                CommandModel.id == command_id,
            )
        )
        return _stored_command(row) if row is not None else None

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
        return _stored_command(row) if row is not None else None

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
                    "data": result.data,
                },
                rejection_reason=None,
            )
        )
        _flush_command(self._session)

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
        _flush_command(self._session)


class EventRepository:
    def __init__(
        self,
        session: Session,
        projection_changes: ProjectionChangePublisherPort | None = None,
    ) -> None:
        self._session = session
        self._projection_changes = projection_changes

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
                topic=_outbox_topic(event),
                payload={
                    "event_id": str(event.id),
                    "workspace_id": str(event.workspace_id),
                    "aggregate_type": event.aggregate_type,
                    "aggregate_id": str(event.aggregate_id),
                    "aggregate_version": event.aggregate_version,
                    "event_type": event.event_type,
                    "data": event.payload,
                    "causation_id": str(event.causation_id),
                    "correlation_id": str(event.correlation_id),
                },
            )
        )
        if self._projection_changes is not None:
            worker_item_id, root_task_id = _resolve_event_scope(self._session, event)
            for target in projection_targets_for_event(
                event,
                worker_item_id=worker_item_id,
                root_task_id=root_task_id,
            ):
                self._projection_changes.changed(
                    workspace_id=event.workspace_id,
                    projection_type=target.projection_type,
                    resource_id=target.resource_id,
                    change_type=target.change_type or event.event_type,
                    resource_version=event.aggregate_version,
                    correlation_id=event.correlation_id,
                )


def _resolve_event_scope(
    session: Session, event: EventData
) -> tuple[UUID | None, UUID | None]:
    worker_item_id = _payload_uuid(event.payload, "worker_item_id")
    root_task_id = _payload_uuid(event.payload, "root_task_id")
    context = event.payload.get("execution_context")
    if worker_item_id is None and isinstance(context, dict):
        worker_item_id = _value_uuid(context.get("worker_item_id"))

    execution_id = _value_uuid(event.payload.get("execution_id"))
    session_id = _value_uuid(event.payload.get("session_id"))
    if execution_id is None and event.aggregate_type == "execution":
        execution_id = event.aggregate_id
    if session_id is None and event.aggregate_type == "execution_session":
        session_id = event.aggregate_id
    scope = resolve_worker_item_scope(
        session,
        event.workspace_id,
        execution_id=execution_id,
        session_id=session_id,
        worker_item_id=worker_item_id,
        root_task_id=root_task_id,
    )
    worker_item_id = scope.worker_item_id
    root_task_id = scope.root_task_id

    if root_task_id is None and event.aggregate_type == "task":
        root_task_id = event.aggregate_id
    return worker_item_id, root_task_id


def _outbox_topic(event: EventData) -> str:
    if event.aggregate_type == "context_package":
        topics = {
            "ContextPackageCreated": "context_package.created",
            "ContextPackageAcknowledged": "context_package.acknowledged",
            "ContextPackageMaterialized": "context_package.materialized",
            "ContextPackageRevoked": "context_package.revoked",
            "ContextPackageCorrupt": "context_package.corrupt",
        }
        topic = topics.get(event.event_type)
        if topic is not None:
            return topic
    if event.aggregate_type == "work_context":
        topics = {
            "WorkContextCreated": "work_context.created",
            "WorkContextRevised": "work_context.revised",
            "WorkContextArchived": "work_context.archived",
        }
        topic = topics.get(event.event_type)
        if topic is not None:
            return topic
    if (
        event.aggregate_type == "work_context_binding"
        and event.event_type == "WorkContextBoundToTask"
    ):
        return "work_context.bound_to_task"
    return f"{event.aggregate_type}.{event.event_type}"


def _payload_uuid(payload: dict[str, object], key: str) -> UUID | None:
    return _value_uuid(payload.get(key))


def _value_uuid(value: object) -> UUID | None:
    if value is None:
        return None
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError):
        return None


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


def _stored_command(row: CommandModel) -> StoredCommand:
    return StoredCommand(
        id=row.id,
        workspace_id=row.workspace_id,
        actor_type=row.actor_type,
        actor_id=row.actor_id,
        idempotency_key=row.idempotency_key,
        target_type=row.target_type,
        request_hash=row.request_hash,
        status=row.status,
        result=dict(row.result or {}),
        rejection_reason=row.rejection_reason,
    )


def _flush_command(session: Session) -> None:
    try:
        session.flush()
    except IntegrityError as exc:
        constraint_name = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
        if constraint_name in {"commands_pkey", "uq_command_idempotency"}:
            raise ConcurrentCommandCommit(constraint_name) from exc
        raise


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
