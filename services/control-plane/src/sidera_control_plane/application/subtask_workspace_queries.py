from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sidera_control_plane.application.errors import NotFound
from sidera_control_plane.domain.plan_normalization import CanonicalTaskPlan
from sidera_control_plane.domain.subtask_projections import (
    SubtaskActivityFact,
    SubtaskArtifactFact,
    SubtaskCheckpointFact,
    SubtaskEvidenceFact,
    SubtaskExecutionFact,
    SubtaskPlacementFact,
    SubtaskSessionFact,
    SubtaskTaskFact,
    SubtaskWorkerFact,
    SubtaskWorkerItemFact,
    SubtaskWorkspaceProjection,
    SubtaskWorkspaceProjectionService,
)


@dataclass(frozen=True, slots=True)
class SubtaskWorkspaceQuerySnapshot:
    workspace_id: UUID
    plan: CanonicalTaskPlan
    tasks: dict[UUID, SubtaskTaskFact]
    worker_items: dict[UUID, SubtaskWorkerItemFact]
    placements: tuple[SubtaskPlacementFact, ...]
    executions: tuple[SubtaskExecutionFact, ...]
    sessions: tuple[SubtaskSessionFact, ...]
    workers: tuple[SubtaskWorkerFact, ...]
    artifacts: tuple[SubtaskArtifactFact, ...]
    evidence: tuple[SubtaskEvidenceFact, ...]
    checkpoints: tuple[SubtaskCheckpointFact, ...]
    activity: tuple[SubtaskActivityFact, ...]
    last_change_cursor: int
    as_of: datetime


class SubtaskWorkspaceQueryStore(Protocol):
    def load(
        self,
        workspace_id: UUID,
        root_task_id: UUID,
        subtask_key: str,
    ) -> SubtaskWorkspaceQuerySnapshot | None:
        raise NotImplementedError


class SubtaskWorkspaceQueryService:
    def __init__(self, store: SubtaskWorkspaceQueryStore) -> None:
        self._store = store
        self._composer = SubtaskWorkspaceProjectionService()

    def get(
        self,
        workspace_id: UUID,
        root_task_id: UUID,
        subtask_key: str,
    ) -> SubtaskWorkspaceProjection:
        loaded = self._store.load(workspace_id, root_task_id, subtask_key)
        if loaded is None:
            raise NotFound("subtask not found")
        try:
            return self._composer.compose(
                loaded.plan,
                subtask_key=subtask_key,
                tasks=loaded.tasks,
                worker_items=loaded.worker_items,
                placements=loaded.placements,
                executions=loaded.executions,
                sessions=loaded.sessions,
                workers=loaded.workers,
                artifacts=loaded.artifacts,
                evidence=loaded.evidence,
                checkpoints=loaded.checkpoints,
                activity=loaded.activity,
                last_change_cursor=loaded.last_change_cursor,
                as_of=loaded.as_of,
            )
        except KeyError as exc:
            raise NotFound("subtask not found") from exc
