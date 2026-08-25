from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select

from sidera_control_plane.application.errors import InvalidCursor
from sidera_control_plane.application.work_context_queries import (
    WorkContextDetailView,
    WorkContextRevisionSummaryView,
    WorkContextRevisionView,
)
from sidera_control_plane.domain.errors import ValidationError
from sidera_control_plane.domain.work_context import WorkContextSnapshotV1
from sidera_control_plane.persistence.database import Database
from sidera_control_plane.persistence.models import (
    WorkContextModel,
    WorkContextRevisionModel,
)


class SqlAlchemyWorkContextQueryStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    def get_by_planning_request(
        self,
        workspace_id: UUID,
        planning_request_id: UUID,
        *,
        limit: int,
        cursor: str | None,
    ) -> WorkContextDetailView | None:
        with self._database.session() as session:
            context = session.scalar(
                select(WorkContextModel).where(
                    WorkContextModel.workspace_id == workspace_id,
                    WorkContextModel.planning_request_id == planning_request_id,
                )
            )
            if context is None:
                return None
            return _detail(session, context, limit=limit, cursor=cursor)

    def get_by_root_task(
        self,
        workspace_id: UUID,
        root_task_id: UUID,
        *,
        limit: int,
        cursor: str | None,
    ) -> WorkContextDetailView | None:
        with self._database.session() as session:
            context = session.scalar(
                select(WorkContextModel).where(
                    WorkContextModel.workspace_id == workspace_id,
                    WorkContextModel.root_task_id == root_task_id,
                )
            )
            if context is None:
                return None
            return _detail(session, context, limit=limit, cursor=cursor)

    def get_revision(
        self,
        workspace_id: UUID,
        work_context_id: UUID,
        revision_id: UUID,
    ) -> WorkContextRevisionView | None:
        with self._database.session() as session:
            row = session.scalar(
                select(WorkContextRevisionModel).where(
                    WorkContextRevisionModel.workspace_id == workspace_id,
                    WorkContextRevisionModel.work_context_id == work_context_id,
                    WorkContextRevisionModel.id == revision_id,
                )
            )
            return _revision_view(row) if row is not None else None


def _detail(
    session: Any,
    context: WorkContextModel,
    *,
    limit: int,
    cursor: str | None,
) -> WorkContextDetailView | None:
    current_row = session.scalar(
        select(WorkContextRevisionModel).where(
            WorkContextRevisionModel.workspace_id == context.workspace_id,
            WorkContextRevisionModel.id == context.current_revision_id,
            WorkContextRevisionModel.work_context_id == context.id,
        )
    )
    if current_row is None:
        return None
    statement = select(WorkContextRevisionModel).where(
        WorkContextRevisionModel.workspace_id == context.workspace_id,
        WorkContextRevisionModel.work_context_id == context.id,
    )
    statement = _apply_cursor(statement, cursor)
    rows = session.scalars(
        statement.order_by(
            WorkContextRevisionModel.revision_number.asc(),
            WorkContextRevisionModel.id.asc(),
        ).limit(limit + 1)
    ).all()
    page_rows = rows[:limit]
    next_cursor = (
        _encode_cursor(page_rows[-1].revision_number, page_rows[-1].id)
        if len(rows) > limit
        else None
    )
    current_revision = _revision_view(current_row)
    return WorkContextDetailView(
        id=context.id,
        workspace_id=context.workspace_id,
        planning_request_id=context.planning_request_id,
        root_task_id=context.root_task_id,
        status=context.status,
        current_revision_id=context.current_revision_id,
        current_revision_number=context.current_revision_number,
        version=context.version,
        created_at=context.created_at,
        updated_at=context.updated_at,
        current_revision=current_revision,
        timeline=tuple(_summary_view(row) for row in page_rows),
        next_cursor=next_cursor,
    )


def _revision_view(row: WorkContextRevisionModel) -> WorkContextRevisionView:
    summary = _summary_view(row)
    return WorkContextRevisionView(
        id=summary.id,
        work_context_id=summary.work_context_id,
        workspace_id=summary.workspace_id,
        revision_number=summary.revision_number,
        brief_planning_request_id=summary.brief_planning_request_id,
        brief_revision=summary.brief_revision,
        proposal_revision_id=summary.proposal_revision_id,
        plan_snapshot_id=summary.plan_snapshot_id,
        reason=summary.reason,
        content_hash=summary.content_hash,
        created_by=summary.created_by,
        created_at=summary.created_at,
        snapshot=_snapshot(row.snapshot),
    )


def _summary_view(row: WorkContextRevisionModel) -> WorkContextRevisionSummaryView:
    return WorkContextRevisionSummaryView(
        id=row.id,
        work_context_id=row.work_context_id,
        workspace_id=row.workspace_id,
        revision_number=row.revision_number,
        brief_planning_request_id=row.brief_planning_request_id,
        brief_revision=row.brief_revision,
        proposal_revision_id=row.proposal_revision_id,
        plan_snapshot_id=row.plan_snapshot_id,
        reason=row.reason,
        content_hash=row.content_hash,
        created_by=row.created_by,
        created_at=row.created_at,
    )


def _snapshot(value: object) -> WorkContextSnapshotV1:
    if not isinstance(value, Mapping):
        raise ValidationError("stored work context snapshot must be an object")
    return WorkContextSnapshotV1.from_mapping(value)


def _apply_cursor(statement: Any, cursor: str | None) -> Any:
    if cursor is None:
        return statement
    revision_number, revision_id = _decode_cursor(cursor)
    return statement.where(
        or_(
            WorkContextRevisionModel.revision_number > revision_number,
            and_(
                WorkContextRevisionModel.revision_number == revision_number,
                WorkContextRevisionModel.id > revision_id,
            ),
        )
    )


def _encode_cursor(revision_number: int, revision_id: UUID) -> str:
    payload = {"revision_number": revision_number, "id": str(revision_id)}
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    )
    return encoded.decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[int, UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        revision_number = payload["revision_number"]
        revision_id = UUID(str(payload["id"]))
    except (binascii.Error, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise InvalidCursor("invalid work context query cursor") from exc
    if (
        not isinstance(revision_number, int)
        or isinstance(revision_number, bool)
        or revision_number < 1
    ):
        raise InvalidCursor("invalid work context query cursor")
    return revision_number, revision_id
