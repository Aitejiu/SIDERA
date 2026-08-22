from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from sidera_control_plane.domain.attention import (
    AttentionAcknowledgement,
    AttentionSource,
)
from sidera_control_plane.domain.plan_normalization import PlanNormalizer
from sidera_control_plane.domain.plans import TaskPlanSnapshot
from sidera_control_plane.persistence.models import (
    AttentionAcknowledgementModel,
    EventModel,
    ExecutionEventModel,
    ExecutionModel,
    PlanSnapshotModel,
    TaskModel,
)


class AttentionAcknowledgementRepository:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._normalizer = PlanNormalizer()

    def get_current_source(
        self, root_task_id: UUID, workspace_id: UUID, attention_key: str
    ) -> AttentionSource | None:
        snapshot_row = self._session.scalar(
            select(PlanSnapshotModel)
            .where(
                PlanSnapshotModel.root_task_id == root_task_id,
                PlanSnapshotModel.workspace_id == workspace_id,
            )
            .order_by(PlanSnapshotModel.generation.desc())
            .limit(1)
        )
        if snapshot_row is None:
            return None
        snapshot = _plan(snapshot_row)
        kind, raw_resource_id, state = _attention_key_parts(attention_key)
        if kind is None or raw_resource_id is None or state is None:
            return None
        try:
            resource_id = UUID(raw_resource_id)
        except ValueError:
            return None

        if kind == "execution":
            execution = self._session.scalar(
                select(ExecutionModel).where(
                    ExecutionModel.id == resource_id,
                    ExecutionModel.workspace_id == workspace_id,
                )
            )
            if execution is None or not self._snapshot_contains_task(
                snapshot, execution.task_id
            ):
                return None
            if _execution_attention_state(execution.status) != state:
                return None
            correlation_id = self._session.scalar(
                select(ExecutionEventModel.id)
                .where(
                    ExecutionEventModel.execution_id == execution.id,
                    ExecutionEventModel.session_id.is_not(None),
                    ExecutionEventModel.workspace_id == workspace_id,
                )
                .order_by(ExecutionEventModel.sequence.desc())
                .limit(1)
            ) or UUID(int=0)
            return AttentionSource(
                attention_key=attention_key,
                root_task_id=root_task_id,
                resource_id=execution.id,
                resource_version=execution.version,
                correlation_id=correlation_id,
            )

        task = self._session.scalar(
            select(TaskModel).where(
                TaskModel.id == resource_id,
                TaskModel.workspace_id == workspace_id,
            )
        )
        if task is None or not self._snapshot_contains_task(snapshot, task.id):
            return None
        if _task_attention_state(task.status) != state:
            return None
        correlation_id = self._session.scalar(
            select(EventModel.id)
            .where(
                EventModel.aggregate_type == "task",
                EventModel.aggregate_id == task.id,
                EventModel.workspace_id == workspace_id,
            )
            .order_by(EventModel.created_at.desc(), EventModel.id.desc())
            .limit(1)
        ) or UUID(int=0)
        return AttentionSource(
            attention_key=attention_key,
            root_task_id=root_task_id,
            resource_id=task.id,
            resource_version=task.version,
            correlation_id=correlation_id,
        )

    def get(
        self,
        workspace_id: UUID,
        actor_type: str,
        actor_id: str,
        attention_key: str,
    ) -> AttentionAcknowledgement | None:
        row = self._session.scalar(
            select(AttentionAcknowledgementModel).where(
                AttentionAcknowledgementModel.workspace_id == workspace_id,
                AttentionAcknowledgementModel.actor_type == actor_type,
                AttentionAcknowledgementModel.actor_id == actor_id,
                AttentionAcknowledgementModel.attention_key == attention_key,
            )
        )
        return _to_domain(row) if row is not None else None

    def list_for_root(
        self,
        root_task_id: UUID,
        workspace_id: UUID,
        actor_type: str,
        actor_id: str,
    ) -> tuple[AttentionAcknowledgement, ...]:
        rows = self._session.scalars(
            select(AttentionAcknowledgementModel)
            .where(
                AttentionAcknowledgementModel.root_task_id == root_task_id,
                AttentionAcknowledgementModel.workspace_id == workspace_id,
                AttentionAcknowledgementModel.actor_type == actor_type,
                AttentionAcknowledgementModel.actor_id == actor_id,
            )
            .order_by(AttentionAcknowledgementModel.acknowledged_at.desc())
        ).all()
        return tuple(_to_domain(row) for row in rows)

    def upsert(self, acknowledgement: AttentionAcknowledgement) -> None:
        statement = insert(AttentionAcknowledgementModel).values(
            workspace_id=acknowledgement.workspace_id,
            actor_type=acknowledgement.actor_type,
            actor_id=acknowledgement.actor_id,
            attention_key=acknowledgement.attention_key,
            root_task_id=acknowledgement.root_task_id,
            source_correlation_id=acknowledgement.source_correlation_id,
            source_resource_version=acknowledgement.source_resource_version,
            acknowledged_command_id=acknowledgement.acknowledged_command_id,
            acknowledged_at=acknowledgement.acknowledged_at,
        )

        self._session.execute(
            statement.on_conflict_do_update(
                index_elements=[
                    AttentionAcknowledgementModel.workspace_id,
                    AttentionAcknowledgementModel.actor_type,
                    AttentionAcknowledgementModel.actor_id,
                    AttentionAcknowledgementModel.attention_key,
                ],
                set_={
                    "root_task_id": statement.excluded.root_task_id,
                    "source_correlation_id": statement.excluded.source_correlation_id,
                    "source_resource_version": statement.excluded.source_resource_version,
                    "acknowledged_command_id": statement.excluded.acknowledged_command_id,
                    "acknowledged_at": statement.excluded.acknowledged_at,
                },
            )
        )

    def _snapshot_contains_task(
        self, snapshot: TaskPlanSnapshot, task_id: UUID
    ) -> bool:
        canonical = self._normalizer.normalize(snapshot)
        return any(item.task_id == task_id for item in canonical.subtasks)


def _to_domain(row: AttentionAcknowledgementModel) -> AttentionAcknowledgement:
    return AttentionAcknowledgement(
        workspace_id=row.workspace_id,
        actor_type=row.actor_type,
        actor_id=row.actor_id,
        attention_key=row.attention_key,
        root_task_id=row.root_task_id,
        source_correlation_id=row.source_correlation_id,
        source_resource_version=row.source_resource_version,
        acknowledged_command_id=row.acknowledged_command_id,
        acknowledged_at=row.acknowledged_at,
    )


def _plan(row: PlanSnapshotModel) -> TaskPlanSnapshot:
    snapshot = TaskPlanSnapshot.from_payload(dict(row.payload))
    if snapshot.id != row.id or snapshot.generation != row.generation:
        raise ValueError("task plan snapshot payload does not match identity columns")
    return snapshot


def _attention_key_parts(key: str) -> tuple[str | None, str | None, str | None]:
    parts = key.split(":", 2)
    if len(parts) != 3:
        return None, None, None
    return parts[0], parts[1], parts[2]


def _execution_attention_state(status: str) -> str | None:
    return {
        "failed": "failed",
    }.get(status)


def _task_attention_state(status: str) -> str | None:
    return {
        "waiting": "waiting_user",
        "blocked": "blocked",
        "failed": "failed",
    }.get(status)
