from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from sidera_control_plane.domain.errors import VersionConflict
from sidera_control_plane.domain.execution import (
    ExecutionSession,
    ExecutionSessionStatus,
)
from sidera_control_plane.persistence.models import ExecutionSessionModel


class SessionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_active_for_execution(
        self, execution_id: UUID, *, for_update: bool = False
    ) -> ExecutionSession | None:
        statement = select(ExecutionSessionModel).where(
            ExecutionSessionModel.execution_id == execution_id,
            ExecutionSessionModel.status == ExecutionSessionStatus.ACTIVE.value,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_domain_session(row) if row is not None else None

    def add(self, session: ExecutionSession) -> None:
        self._session.add(
            ExecutionSessionModel(
                id=session.id,
                workspace_id=session.workspace_id,
                execution_id=session.execution_id,
                worker_id=session.worker_id,
                adapter_session_id=session.adapter_session_id,
                status=session.status.value,
                last_sequence=session.last_sequence,
                last_activity_at=session.last_activity_at,
                input_mode=session.input_mode,
                version=session.version,
            )
        )

    def save(self, session: ExecutionSession, previous_version: int) -> None:
        statement = (
            update(ExecutionSessionModel)
            .where(
                ExecutionSessionModel.id == session.id,
                ExecutionSessionModel.workspace_id == session.workspace_id,
                ExecutionSessionModel.version == previous_version,
            )
            .values(
                execution_id=session.execution_id,
                worker_id=session.worker_id,
                adapter_session_id=session.adapter_session_id,
                status=session.status.value,
                last_sequence=session.last_sequence,
                last_activity_at=session.last_activity_at,
                input_mode=session.input_mode,
                version=session.version,
                updated_at=datetime.now(UTC),
            )
        )
        result = self._session.execute(statement)
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            actual = self._current_version(session.id, session.workspace_id)
            raise VersionConflict(previous_version, actual)

    def _current_version(self, session_id: UUID, workspace_id: UUID) -> int:
        version = self._session.scalar(
            select(ExecutionSessionModel.version).where(
                ExecutionSessionModel.id == session_id,
                ExecutionSessionModel.workspace_id == workspace_id,
            )
        )
        return version if version is not None else 0


def _to_domain_session(row: ExecutionSessionModel) -> ExecutionSession:
    return ExecutionSession(
        id=row.id,
        workspace_id=row.workspace_id,
        execution_id=row.execution_id,
        worker_id=row.worker_id,
        adapter_session_id=row.adapter_session_id,
        status=ExecutionSessionStatus(row.status),
        last_sequence=row.last_sequence,
        last_activity_at=row.last_activity_at,
        input_mode=row.input_mode,
        version=row.version,
    )
