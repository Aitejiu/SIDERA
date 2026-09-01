from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sidera_control_plane.application.errors import NoCurrentPlan
from sidera_control_plane.domain.attention import AttentionAcknowledgement
from sidera_control_plane.domain.plan_convergence_projections import (
    PlanConvergenceFacts,
)
from sidera_control_plane.domain.plan_normalization import CanonicalTaskPlan
from sidera_control_plane.domain.task_console_projections import (
    TaskConsoleActivityFact,
    TaskConsoleActivityProjection,
    TaskConsoleEvidenceFact,
    TaskConsoleExecutionFact,
    TaskConsoleGraphProjection,
    TaskConsoleOverviewProjection,
    TaskConsolePlacementFact,
    TaskConsoleProjectionService,
    TaskConsoleTaskFact,
    TaskConsoleWorkerItemFact,
)
from sidera_control_plane.domain.worker_item_projections import ActiveHandoffProjection


@dataclass(frozen=True, slots=True)
class TaskConsoleQuerySnapshot:
    workspace_id: UUID
    plan: CanonicalTaskPlan
    tasks: dict[UUID, TaskConsoleTaskFact]
    worker_items: dict[UUID, TaskConsoleWorkerItemFact]
    placements: tuple[TaskConsolePlacementFact, ...]
    executions: tuple[TaskConsoleExecutionFact, ...]
    evidence: tuple[TaskConsoleEvidenceFact, ...]
    activity: tuple[TaskConsoleActivityFact, ...]
    acknowledgements: tuple[AttentionAcknowledgement, ...]
    last_change_cursor: int
    as_of: datetime
    convergence_facts: PlanConvergenceFacts | None = None
    handoffs: dict[UUID, ActiveHandoffProjection] | None = None


class TaskConsoleQueryStore(Protocol):
    def load(
        self,
        workspace_id: UUID,
        root_task_id: UUID,
        *,
        actor_type: str | None = None,
        actor_id: str | None = None,
    ) -> TaskConsoleQuerySnapshot | None:
        raise NotImplementedError

    def list_current_root_ids(self, workspace_id: UUID) -> tuple[UUID, ...]:
        raise NotImplementedError


class TaskConsoleQueryService:
    def __init__(
        self,
        store: TaskConsoleQueryStore,
        *,
        actor_type: str | None = None,
        actor_id: str | None = None,
    ) -> None:
        self._store = store
        self._composer = TaskConsoleProjectionService()
        self._actor_type = actor_type
        self._actor_id = actor_id

    def list_overviews(
        self, workspace_id: UUID
    ) -> tuple[TaskConsoleOverviewProjection, ...]:
        overviews: list[TaskConsoleOverviewProjection] = []
        for root_task_id in self._store.list_current_root_ids(workspace_id):
            loaded = self._load(workspace_id, root_task_id)
            if loaded is not None:
                overviews.append(self._compose_graph(loaded).overview)
        return tuple(overviews)

    def get_overview(
        self, workspace_id: UUID, root_task_id: UUID
    ) -> TaskConsoleOverviewProjection:
        return self.get_graph(workspace_id, root_task_id).overview

    def get_graph(self, workspace_id: UUID, root_task_id: UUID) -> TaskConsoleGraphProjection:
        loaded = self._require_loaded(workspace_id, root_task_id)
        return self._compose_graph(loaded)

    def _compose_graph(
        self, loaded: TaskConsoleQuerySnapshot
    ) -> TaskConsoleGraphProjection:
        return self._composer.compose(
            loaded.plan,
            tasks=loaded.tasks,
            worker_items=loaded.worker_items,
            placements=loaded.placements,
            executions=loaded.executions,
            evidence=loaded.evidence,
            activity=loaded.activity,
            last_change_cursor=loaded.last_change_cursor,
            as_of=loaded.as_of,
            acknowledgements=loaded.acknowledgements,
            convergence_facts=loaded.convergence_facts,
            handoffs=loaded.handoffs or {},
        )

    def get_activity(
        self,
        workspace_id: UUID,
        root_task_id: UUID,
        *,
        after: str | None,
        limit: int,
    ) -> TaskConsoleActivityProjection:
        loaded = self._require_loaded(workspace_id, root_task_id)
        return self._composer.compose_activity(
            loaded.plan,
            loaded.activity,
            last_change_cursor=loaded.last_change_cursor,
            after=after,
            limit=limit,
        )

    def _require_loaded(
        self, workspace_id: UUID, root_task_id: UUID
    ) -> TaskConsoleQuerySnapshot:
        loaded = self._load(workspace_id, root_task_id)
        if loaded is None:
            raise NoCurrentPlan("current task plan is unavailable")
        return loaded

    def _load(
        self, workspace_id: UUID, root_task_id: UUID
    ) -> TaskConsoleQuerySnapshot | None:
        return self._store.load(
            workspace_id,
            root_task_id,
            actor_type=self._actor_type,
            actor_id=self._actor_id,
        )
