from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sidera_control_plane.application.errors import InvalidCursor, NotFound
from sidera_control_plane.domain.plan_convergence_projections import (
    PlanConvergenceFacts,
)
from sidera_control_plane.domain.worker_item_projections import (
    ArtifactSummaryProjection,
    BindingFact,
    EvidenceSummaryProjection,
    ExecutionFact,
    InputFact,
    InvalidActivityCursor,
    LeaseFact,
    SessionFact,
    WorkerActivityPageProjection,
    WorkerEventFact,
    WorkerFact,
    WorkerItemFact,
    WorkerItemPlacementFact,
    WorkerItemProjectionService,
    WorkerWorkspaceProjection,
)


@dataclass(frozen=True, slots=True)
class WorkerWorkspaceQuerySnapshot:
    worker_item: WorkerItemFact
    placements: tuple[WorkerItemPlacementFact, ...]
    executions: tuple[ExecutionFact, ...]
    sessions: tuple[SessionFact, ...]
    workers: tuple[WorkerFact, ...]
    bindings: tuple[BindingFact, ...]
    leases: tuple[LeaseFact, ...]
    events: tuple[WorkerEventFact, ...]
    inputs: tuple[InputFact, ...]
    artifacts: tuple[ArtifactSummaryProjection, ...]
    evidence: tuple[EvidenceSummaryProjection, ...]
    last_change_cursor: int
    as_of: datetime
    projection_stale: bool = False
    convergence_facts: PlanConvergenceFacts | None = None


class WorkerWorkspaceQueryStore(Protocol):
    def load(
        self, workspace_id: UUID, worker_item_id: UUID
    ) -> WorkerWorkspaceQuerySnapshot | None:
        raise NotImplementedError


class WorkerWorkspaceQueryService:
    def __init__(self, store: WorkerWorkspaceQueryStore) -> None:
        self._store = store

    def get_workspace(
        self, workspace_id: UUID, worker_item_id: UUID
    ) -> WorkerWorkspaceProjection:
        loaded = self._load(workspace_id, worker_item_id)
        try:
            return self._composer(loaded).compose_workspace()
        except ValueError as exc:
            raise NotFound("worker item workspace not found") from exc

    def list_activity(
        self,
        workspace_id: UUID,
        worker_item_id: UUID,
        *,
        after: str | None,
        limit: int,
    ) -> WorkerActivityPageProjection:
        loaded = self._load(workspace_id, worker_item_id)
        try:
            composer = self._composer(loaded)
            return composer.compose_activity(
                composer.activity_facts(),
                after=after,
                limit=limit,
            )
        except InvalidActivityCursor as exc:
            raise InvalidCursor(str(exc)) from exc

    def _load(
        self, workspace_id: UUID, worker_item_id: UUID
    ) -> WorkerWorkspaceQuerySnapshot:
        loaded = self._store.load(workspace_id, worker_item_id)
        if loaded is None:
            raise NotFound("worker item not found")
        return loaded

    @staticmethod
    def _composer(snapshot: WorkerWorkspaceQuerySnapshot) -> WorkerItemProjectionService:
        return WorkerItemProjectionService(
            worker_item=snapshot.worker_item,
            placements=snapshot.placements,
            executions=snapshot.executions,
            sessions=snapshot.sessions,
            workers=snapshot.workers,
            bindings=snapshot.bindings,
            leases=snapshot.leases,
            events=snapshot.events,
            inputs=snapshot.inputs,
            artifacts=snapshot.artifacts,
            evidence=snapshot.evidence,
            last_change_cursor=snapshot.last_change_cursor,
            as_of=snapshot.as_of,
            projection_stale=snapshot.projection_stale,
            convergence_facts=snapshot.convergence_facts,
        )
