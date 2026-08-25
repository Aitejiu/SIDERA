from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sidera_control_plane.application.errors import NotFound
from sidera_control_plane.domain.work_context import WorkContextSnapshotV1


@dataclass(frozen=True, slots=True)
class WorkContextRevisionSummaryView:
    id: UUID
    work_context_id: UUID
    workspace_id: UUID
    revision_number: int
    brief_planning_request_id: UUID
    brief_revision: int
    proposal_revision_id: UUID | None
    plan_snapshot_id: UUID | None
    reason: str
    content_hash: str
    created_by: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class WorkContextRevisionView(WorkContextRevisionSummaryView):
    snapshot: WorkContextSnapshotV1


@dataclass(frozen=True, slots=True)
class WorkContextDetailView:
    id: UUID
    workspace_id: UUID
    planning_request_id: UUID
    root_task_id: UUID | None
    status: str
    current_revision_id: UUID
    current_revision_number: int
    version: int
    created_at: datetime
    updated_at: datetime
    current_revision: WorkContextRevisionView
    timeline: tuple[WorkContextRevisionSummaryView, ...]
    next_cursor: str | None


class WorkContextQueryStore(Protocol):
    def get_by_planning_request(
        self,
        workspace_id: UUID,
        planning_request_id: UUID,
        *,
        limit: int,
        cursor: str | None,
    ) -> WorkContextDetailView | None: ...

    def get_by_root_task(
        self,
        workspace_id: UUID,
        root_task_id: UUID,
        *,
        limit: int,
        cursor: str | None,
    ) -> WorkContextDetailView | None: ...

    def get_revision(
        self,
        workspace_id: UUID,
        work_context_id: UUID,
        revision_id: UUID,
    ) -> WorkContextRevisionView | None: ...


class WorkContextQueryService:
    def __init__(self, store: WorkContextQueryStore) -> None:
        self._store = store

    def get_by_planning_request(
        self,
        workspace_id: UUID,
        planning_request_id: UUID,
        *,
        limit: int,
        cursor: str | None,
    ) -> WorkContextDetailView:
        _validate_limit(limit)
        detail = self._store.get_by_planning_request(
            workspace_id,
            planning_request_id,
            limit=limit,
            cursor=cursor,
        )
        if detail is None:
            raise NotFound("work context not found")
        return detail

    def get_by_root_task(
        self,
        workspace_id: UUID,
        root_task_id: UUID,
        *,
        limit: int,
        cursor: str | None,
    ) -> WorkContextDetailView:
        _validate_limit(limit)
        detail = self._store.get_by_root_task(
            workspace_id,
            root_task_id,
            limit=limit,
            cursor=cursor,
        )
        if detail is None:
            raise NotFound("work context not found")
        return detail

    def get_revision(
        self, workspace_id: UUID, work_context_id: UUID, revision_id: UUID
    ) -> WorkContextRevisionView:
        revision = self._store.get_revision(
            workspace_id, work_context_id, revision_id
        )
        if revision is None:
            raise NotFound("work context revision not found")
        return revision


def _validate_limit(limit: int) -> None:
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
