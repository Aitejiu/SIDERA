from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from sidera_control_plane.domain.errors import VersionConflict
from sidera_control_plane.domain.scheduling import (
    SchedulingBlock,
    SchedulingBlockReason,
    SchedulingBlockStatus,
    SchedulingIssue,
)
from sidera_control_plane.persistence.models import SchedulingBlockModel


class SqlAlchemySchedulingBlockRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_active_for_execution(
        self,
        execution_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> SchedulingBlock | None:
        statement = select(SchedulingBlockModel).where(
            SchedulingBlockModel.execution_id == execution_id,
            SchedulingBlockModel.workspace_id == workspace_id,
            SchedulingBlockModel.status == SchedulingBlockStatus.ACTIVE.value,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_domain(row) if row is not None else None

    def add(self, block: SchedulingBlock) -> None:
        self._session.add(_to_model(block))

    def save(self, block: SchedulingBlock, previous_version: int) -> None:
        result = self._session.execute(
            update(SchedulingBlockModel)
            .where(
                SchedulingBlockModel.id == block.id,
                SchedulingBlockModel.workspace_id == block.workspace_id,
                SchedulingBlockModel.version == previous_version,
            )
            .values(
                status=block.status.value,
                reason_code=block.reason_code.value,
                reason_details=_plain_json(block.reason_details),
                fingerprint=block.fingerprint,
                requires_operator=block.requires_operator,
                first_seen_at=block.first_seen_at,
                last_seen_at=block.last_seen_at,
                occurrence_count=block.occurrence_count,
                resolved_at=block.resolved_at,
                version=block.version,
                updated_at=datetime.now(UTC),
            )
        )
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            raise VersionConflict(
                previous_version,
                self._current_version(block.id, block.workspace_id),
            )

    def _current_version(self, block_id: UUID, workspace_id: UUID) -> int:
        version = self._session.scalar(
            select(SchedulingBlockModel.version).where(
                SchedulingBlockModel.id == block_id,
                SchedulingBlockModel.workspace_id == workspace_id,
            )
        )
        return version if version is not None else 0


def _to_model(block: SchedulingBlock) -> SchedulingBlockModel:
    return SchedulingBlockModel(
        id=block.id,
        workspace_id=block.workspace_id,
        execution_id=block.execution_id,
        status=block.status.value,
        reason_code=block.reason_code.value,
        reason_details=_plain_json(block.reason_details),
        fingerprint=block.fingerprint,
        requires_operator=block.requires_operator,
        first_seen_at=block.first_seen_at,
        last_seen_at=block.last_seen_at,
        occurrence_count=block.occurrence_count,
        resolved_at=block.resolved_at,
        version=block.version,
    )


def _to_domain(row: SchedulingBlockModel) -> SchedulingBlock:
    issue = SchedulingIssue(
        reason_code=SchedulingBlockReason(row.reason_code),
        reason_details=row.reason_details,
        requires_operator=row.requires_operator,
    )
    return SchedulingBlock(
        id=row.id,
        workspace_id=row.workspace_id,
        execution_id=row.execution_id,
        status=SchedulingBlockStatus(row.status),
        reason_code=issue.reason_code,
        reason_details=issue.reason_details,
        fingerprint=row.fingerprint,
        requires_operator=row.requires_operator,
        first_seen_at=row.first_seen_at,
        last_seen_at=row.last_seen_at,
        occurrence_count=row.occurrence_count,
        resolved_at=row.resolved_at,
        version=row.version,
    )


def _plain_json(value: object) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value
