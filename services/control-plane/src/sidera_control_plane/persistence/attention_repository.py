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
    RootTaskPlanHeadModel,
    SchedulingBlockModel,
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
            .join(
                RootTaskPlanHeadModel,
                (RootTaskPlanHeadModel.active_snapshot_id == PlanSnapshotModel.id)
                & (
                    RootTaskPlanHeadModel.workspace_id
                    == PlanSnapshotModel.workspace_id
                )
                & (
                    RootTaskPlanHeadModel.root_task_id
                    == PlanSnapshotModel.root_task_id
                ),
            )
            .where(
                PlanSnapshotModel.root_task_id == root_task_id,
                PlanSnapshotModel.workspace_id == workspace_id,
            )
        )
        if snapshot_row is None:
            return None
        snapshot = _plan(snapshot_row)
        scheduling_block_parts = _scheduling_block_attention_key_parts(
            attention_key, root_task_id
        )
        if scheduling_block_parts is not None:
            return self._get_current_scheduling_block_source(
                snapshot,
                workspace_id,
                attention_key,
                *scheduling_block_parts,
            )
        subtask_parts = _subtask_attention_key_parts(attention_key, root_task_id)
        if subtask_parts is not None:
            return self._get_current_subtask_source(
                snapshot,
                workspace_id,
                attention_key,
                *subtask_parts,
            )
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

    def _get_current_scheduling_block_source(
        self,
        snapshot: TaskPlanSnapshot,
        workspace_id: UUID,
        attention_key: str,
        subtask_key: str,
        raw_block_id: str,
    ) -> AttentionSource | None:
        subtask = next(
            (
                item
                for item in self._normalizer.normalize(snapshot).subtasks
                if item.key == subtask_key
            ),
            None,
        )
        if subtask is None or subtask.task_id is None:
            return None
        try:
            block_id = UUID(raw_block_id)
        except ValueError:
            return None
        block = self._session.scalar(
            select(SchedulingBlockModel)
            .join(
                ExecutionModel,
                ExecutionModel.id == SchedulingBlockModel.execution_id,
            )
            .where(
                SchedulingBlockModel.id == block_id,
                SchedulingBlockModel.workspace_id == workspace_id,
                SchedulingBlockModel.status == "active",
                ExecutionModel.workspace_id == workspace_id,
                ExecutionModel.task_id == subtask.task_id,
            )
        )
        if block is None:
            return None
        return AttentionSource(
            attention_key=attention_key,
            root_task_id=snapshot.root_task_id,
            resource_id=block.id,
            resource_version=block.version,
            correlation_id=block.id,
        )

    def _get_current_subtask_source(
        self,
        snapshot: TaskPlanSnapshot,
        workspace_id: UUID,
        attention_key: str,
        subtask_key: str,
        state: str,
    ) -> AttentionSource | None:
        canonical = self._normalizer.normalize(snapshot)
        subtask = next(
            (item for item in canonical.subtasks if item.key == subtask_key),
            None,
        )
        if subtask is None or subtask.task_id is None:
            return None
        task = self._session.scalar(
            select(TaskModel).where(
                TaskModel.id == subtask.task_id,
                TaskModel.workspace_id == workspace_id,
            )
        )
        if task is None:
            return None
        execution_rows = self._session.scalars(
            select(ExecutionModel)
            .where(
                ExecutionModel.workspace_id == workspace_id,
                ExecutionModel.task_id == task.id,
            )
            .order_by(ExecutionModel.updated_at.desc(), ExecutionModel.id.desc())
        ).all()
        execution_states = tuple(
            current_state
            for execution in execution_rows
            for current_state in (_execution_attention_state(execution.status),)
            if current_state is not None
        )
        current_state = _subtask_attention_state(task.status, execution_states)
        if current_state != state:
            return None

        correlation_id = self._session.scalar(
            select(ExecutionEventModel.id)
            .join(
                ExecutionModel,
                ExecutionModel.id == ExecutionEventModel.execution_id,
            )
            .where(
                ExecutionEventModel.workspace_id == workspace_id,
                ExecutionModel.workspace_id == workspace_id,
                ExecutionModel.task_id == task.id,
            )
            .order_by(
                ExecutionEventModel.occurred_at.desc(),
                ExecutionEventModel.sequence.desc(),
                ExecutionEventModel.id.desc(),
            )
            .limit(1)
        )
        if correlation_id is None:
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
            root_task_id=snapshot.root_task_id,
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


def _subtask_attention_key_parts(
    key: str, root_task_id: UUID
) -> tuple[str, str] | None:
    parts = key.split(":")
    if len(parts) < 4 or parts[0] != "subtask" or parts[1] != str(root_task_id):
        return None
    return ":".join(parts[2:-1]), parts[-1]


def _scheduling_block_attention_key_parts(
    key: str, root_task_id: UUID
) -> tuple[str, str] | None:
    parts = key.split(":")
    if (
        len(parts) < 4
        or parts[0] != "scheduling_block"
        or parts[1] != str(root_task_id)
    ):
        return None
    return ":".join(parts[2:-1]), parts[-1]


def _subtask_attention_state(
    task_status: str, execution_states: tuple[str, ...]
) -> str | None:
    if "waiting_user" in execution_states or task_status in {"waiting", "waiting_user"}:
        return "waiting_user"
    if "blocked" in execution_states or task_status == "blocked":
        return "blocked"
    if "failed" in execution_states or task_status == "failed":
        return "failed"
    return None


def _execution_attention_state(status: str) -> str | None:
    return {
        "waiting_user": "waiting_user",
        "blocked": "blocked",
        "failed": "failed",
    }.get(status)


def _task_attention_state(status: str) -> str | None:
    return {
        "waiting": "waiting_user",
        "blocked": "blocked",
        "failed": "failed",
    }.get(status)
