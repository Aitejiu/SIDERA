from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from sidera_control_plane.domain.errors import ValidationError, VersionConflict
from sidera_control_plane.domain.work_context import (
    WorkContext,
    WorkContextRevision,
    WorkContextRevisionReason,
    WorkContextSnapshotV1,
    WorkContextStatus,
)
from sidera_control_plane.persistence.models import (
    WorkContextModel,
    WorkContextRevisionModel,
)


class WorkContextRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, context: WorkContext, first_revision: WorkContextRevision) -> None:
        if context.current_revision_id != first_revision.revision_id:
            raise ValidationError("work context current revision does not match first revision")
        self._session.add(
            WorkContextModel(
                id=context.id,
                workspace_id=context.workspace_id,
                planning_request_id=context.planning_request_id,
                root_task_id=context.root_task_id,
                status=context.status.value,
                current_revision_id=context.current_revision_id,
                current_revision_number=context.current_revision_number,
                version=context.version,
                created_at=context.created_at,
                updated_at=context.updated_at,
            )
        )
        self._session.add(_revision_model(first_revision))
        self._session.flush()

    def get(
        self,
        work_context_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> WorkContext | None:
        statement = select(WorkContextModel).where(
            WorkContextModel.id == work_context_id,
            WorkContextModel.workspace_id == workspace_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_context(row) if row is not None else None

    def get_by_planning_request(
        self,
        planning_request_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> WorkContext | None:
        statement = select(WorkContextModel).where(
            WorkContextModel.planning_request_id == planning_request_id,
            WorkContextModel.workspace_id == workspace_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_context(row) if row is not None else None

    def get_by_root_task(
        self,
        root_task_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> WorkContext | None:
        statement = select(WorkContextModel).where(
            WorkContextModel.root_task_id == root_task_id,
            WorkContextModel.workspace_id == workspace_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_context(row) if row is not None else None

    def get_revision(
        self,
        revision_id: UUID,
        workspace_id: UUID,
        *,
        for_update: bool = False,
    ) -> WorkContextRevision | None:
        statement = select(WorkContextRevisionModel).where(
            WorkContextRevisionModel.id == revision_id,
            WorkContextRevisionModel.workspace_id == workspace_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self._session.scalar(statement)
        return _to_revision(row) if row is not None else None

    def append_revision(
        self,
        context: WorkContext,
        revision: WorkContextRevision,
        *,
        expected_version: int,
    ) -> None:
        self._session.add(_revision_model(revision))
        self._save_context(context, expected_version)
        self._session.flush()

    def bind_root_task(
        self,
        context: WorkContext,
        root_task_id: UUID,
        revision: WorkContextRevision,
        *,
        expected_version: int,
    ) -> None:
        context.bind_root_task_with_revision(
            root_task_id,
            revision,
            expected_version=expected_version,
            now=datetime.now(UTC),
        )
        self._session.add(_revision_model(revision))
        self._save_context(context, expected_version)
        self._session.flush()

    def archive(self, context: WorkContext, *, expected_version: int) -> None:
        self._save_context(context, expected_version)
        self._session.flush()

    def _save_context(self, context: WorkContext, expected_version: int) -> None:
        result = self._session.execute(
            update(WorkContextModel)
            .where(
                WorkContextModel.id == context.id,
                WorkContextModel.workspace_id == context.workspace_id,
                WorkContextModel.version == expected_version,
            )
            .values(
                root_task_id=context.root_task_id,
                status=context.status.value,
                current_revision_id=context.current_revision_id,
                current_revision_number=context.current_revision_number,
                version=context.version,
                updated_at=context.updated_at,
            )
        )
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            actual = self._current_version(context.id, context.workspace_id)
            raise VersionConflict(expected_version, actual)

    def _current_version(self, work_context_id: UUID, workspace_id: UUID) -> int:
        version = self._session.scalar(
            select(WorkContextModel.version).where(
                WorkContextModel.id == work_context_id,
                WorkContextModel.workspace_id == workspace_id,
            )
        )
        return version if version is not None else 0


def _revision_model(revision: WorkContextRevision) -> WorkContextRevisionModel:
    return WorkContextRevisionModel(
        id=revision.revision_id,
        work_context_id=revision.work_context_id,
        workspace_id=revision.workspace_id,
        revision_number=revision.revision_number,
        brief_planning_request_id=revision.brief_planning_request_id,
        brief_revision=revision.brief_revision,
        proposal_revision_id=revision.proposal_revision_id,
        plan_snapshot_id=revision.plan_snapshot_id,
        reason=revision.reason.value,
        snapshot=revision.snapshot.to_mapping(),
        content_hash=revision.content_hash,
        created_by=revision.created_by,
        created_at=revision.created_at,
    )


def _to_context(row: WorkContextModel) -> WorkContext:
    return WorkContext(
        id=row.id,
        workspace_id=row.workspace_id,
        planning_request_id=row.planning_request_id,
        root_task_id=row.root_task_id,
        status=WorkContextStatus(row.status),
        current_revision_id=row.current_revision_id,
        current_revision_number=row.current_revision_number,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_revision(row: WorkContextRevisionModel) -> WorkContextRevision:
    snapshot = row.snapshot
    if not isinstance(snapshot, dict):
        raise ValidationError("stored work context snapshot must be an object")
    return WorkContextRevision(
        revision_id=row.id,
        work_context_id=row.work_context_id,
        workspace_id=row.workspace_id,
        revision_number=row.revision_number,
        brief_planning_request_id=row.brief_planning_request_id,
        brief_revision=row.brief_revision,
        proposal_revision_id=row.proposal_revision_id,
        plan_snapshot_id=row.plan_snapshot_id,
        reason=WorkContextRevisionReason(row.reason),
        snapshot=WorkContextSnapshotV1.from_mapping(snapshot),
        content_hash=row.content_hash,
        created_by=row.created_by,
        created_at=row.created_at,
    )
