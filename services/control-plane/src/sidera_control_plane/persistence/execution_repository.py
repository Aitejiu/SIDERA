from collections.abc import Collection
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from sidera_control_plane.domain.errors import VersionConflict
from sidera_control_plane.domain.execution import Execution, ExecutionStatus
from sidera_control_plane.persistence.models import ExecutionModel, TaskModel


class ExecutionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(
        self,
        execution_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> Execution | None:
        statement = select(ExecutionModel).where(
            ExecutionModel.id == execution_id,
            ExecutionModel.workspace_id == workspace_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_domain_execution(row) if row is not None else None

    def add(self, execution: Execution) -> None:
        self._session.add(
            ExecutionModel(
                id=execution.id,
                workspace_id=execution.workspace_id,
                task_id=execution.task_id,
                lineage_id=execution.lineage_id,
                attempt=execution.attempt,
                max_attempts=execution.max_attempts,
                purpose=execution.purpose,
                status=execution.status.value,
                capability_requirements=sorted(execution.capability_requirements),
                pinned_worker_id=execution.pinned_worker_id,
                assigned_worker_id=execution.assigned_worker_id,
                active_lease_id=execution.active_lease_id,
                last_fencing_token=execution.last_fencing_token,
                not_before=execution.not_before,
                failure_code=execution.failure_code,
                started_at=execution.started_at,
                finished_at=execution.finished_at,
                version=execution.version,
                stop_requested=execution.stop_requested,
                source_execution_id=execution.source_execution_id,
                source_session_id=execution.source_session_id,
            )
        )

    def save(self, execution: Execution, previous_version: int) -> None:
        statement = (
            update(ExecutionModel)
            .where(
                ExecutionModel.id == execution.id,
                ExecutionModel.workspace_id == execution.workspace_id,
                ExecutionModel.version == previous_version,
            )
            .values(
                task_id=execution.task_id,
                lineage_id=execution.lineage_id,
                attempt=execution.attempt,
                max_attempts=execution.max_attempts,
                purpose=execution.purpose,
                status=execution.status.value,
                capability_requirements=sorted(execution.capability_requirements),
                pinned_worker_id=execution.pinned_worker_id,
                assigned_worker_id=execution.assigned_worker_id,
                active_lease_id=execution.active_lease_id,
                last_fencing_token=execution.last_fencing_token,
                not_before=execution.not_before,
                failure_code=execution.failure_code,
                started_at=execution.started_at,
                finished_at=execution.finished_at,
                version=execution.version,
                stop_requested=execution.stop_requested,
                source_execution_id=execution.source_execution_id,
                source_session_id=execution.source_session_id,
                updated_at=datetime.now(UTC),
            )
        )
        result = self._session.execute(statement)
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            actual = self._current_version(execution.id, execution.workspace_id)
            raise VersionConflict(previous_version, actual)

    def attempt_exists(self, lineage_id: UUID, attempt: int) -> bool:
        return (
            self._session.scalar(
                select(ExecutionModel.id).where(
                    ExecutionModel.lineage_id == lineage_id,
                    ExecutionModel.attempt == attempt,
                )
            )
            is not None
        )

    def claim_schedulable(
        self,
        *,
        now: datetime,
        limit: int,
        excluded_ids: Collection[UUID] = (),
    ) -> tuple[Execution, ...]:
        statement = select(ExecutionModel).join(
            TaskModel,
            and_(
                TaskModel.id == ExecutionModel.task_id,
                TaskModel.workspace_id == ExecutionModel.workspace_id,
            ),
        ).where(
            ExecutionModel.status == ExecutionStatus.QUEUED.value,
            ExecutionModel.active_lease_id.is_(None),
            ExecutionModel.not_before <= now,
            TaskModel.status.in_(("ready", "running")),
        )
        if excluded_ids:
            statement = statement.where(
                ExecutionModel.id.not_in(tuple(excluded_ids))
            )
        statement = (
            statement
            .order_by(
                TaskModel.priority.desc(),
                ExecutionModel.not_before.asc(),
                ExecutionModel.created_at.asc(),
                ExecutionModel.id.asc(),
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        rows = self._session.scalars(statement).all()
        return tuple(_to_domain_execution(row) for row in rows)

    def has_live_for_task(
        self, task_id: UUID, *, excluding: UUID | None = None
    ) -> bool:
        statement = select(func.count()).select_from(ExecutionModel).where(
            ExecutionModel.task_id == task_id,
            ExecutionModel.status.in_(
                (
                    ExecutionStatus.QUEUED.value,
                    ExecutionStatus.LEASED.value,
                    ExecutionStatus.RUNNING.value,
                )
            ),
        )
        if excluding is not None:
            statement = statement.where(ExecutionModel.id != excluding)
        return (self._session.scalar(statement) or 0) > 0

    def _current_version(self, execution_id: UUID, workspace_id: UUID) -> int:
        version = self._session.scalar(
            select(ExecutionModel.version).where(
                ExecutionModel.id == execution_id,
                ExecutionModel.workspace_id == workspace_id,
            )
        )
        return version if version is not None else 0


def _to_domain_execution(row: ExecutionModel) -> Execution:
    return Execution(
        id=row.id,
        workspace_id=row.workspace_id,
        task_id=row.task_id,
        lineage_id=row.lineage_id,
        attempt=row.attempt,
        max_attempts=row.max_attempts,
        purpose=row.purpose,
        status=ExecutionStatus(row.status),
        capability_requirements=frozenset(row.capability_requirements),
        pinned_worker_id=row.pinned_worker_id,
        assigned_worker_id=row.assigned_worker_id,
        active_lease_id=row.active_lease_id,
        last_fencing_token=row.last_fencing_token,
        not_before=row.not_before,
        failure_code=row.failure_code,
        started_at=row.started_at,
        finished_at=row.finished_at,
        version=row.version,
        stop_requested=row.stop_requested,
        source_execution_id=row.source_execution_id,
        source_session_id=row.source_session_id,
    )
