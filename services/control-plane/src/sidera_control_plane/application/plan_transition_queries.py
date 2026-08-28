from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sidera_control_plane.application.errors import NotFound


@dataclass(frozen=True, slots=True)
class TransitionNodeView:
    id: UUID
    key: str | None
    title: str
    path: str


@dataclass(frozen=True, slots=True)
class TransitionOperationView:
    operation_id: UUID
    operation_type: str
    status: str
    failure_code: str | None


@dataclass(frozen=True, slots=True)
class TransitionContextView:
    current_context_version: int
    acknowledged_context_version: int
    target_context_version: int
    patch_id: UUID | None
    patch_hash: str | None
    patch_acknowledged: bool


@dataclass(frozen=True, slots=True)
class TransitionInputDispositionView:
    policy: str
    queued_input_ids: tuple[UUID, ...]
    superseded_input_ids: tuple[UUID, ...]
    queued_count: int
    superseded_count: int


@dataclass(frozen=True, slots=True)
class TransitionDependencyView:
    upstream_worker_item_id: UUID
    downstream_worker_item_id: UUID
    upstream_path: str
    downstream_path: str
    enforcement: str
    upstream_status: str | None
    blocked: bool
    required_artifact_refs: tuple[str, ...]
    required_evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorkerTransitionView:
    worker_transition_id: UUID
    plan_transition_id: UUID
    root_task: TransitionNodeView
    subtask: TransitionNodeView | None
    worker_item: TransitionNodeView
    path: str
    action: str
    fallback_action: str | None
    status: str
    writer_state: str
    failure_code: str | None
    source_execution_id: UUID | None
    source_session_id: UUID | None
    target_execution_id: UUID | None
    target_session_id: UUID | None
    from_subtask_id: UUID | None
    to_subtask_id: UUID | None
    context: TransitionContextView
    operations: tuple[TransitionOperationView, ...]
    queued_input_disposition: TransitionInputDispositionView
    blocked_dependencies: tuple[TransitionDependencyView, ...]
    started_at: datetime | None
    completed_at: datetime | None
    version: int


@dataclass(frozen=True, slots=True)
class PlanTransitionSummaryView:
    plan_transition_id: UUID
    workspace_id: UUID
    root_task: TransitionNodeView
    from_generation: int
    to_generation: int
    status: str
    started_at: datetime
    completed_at: datetime | None
    version: int
    worker_count: int
    blocked_worker_count: int
    unknown_worker_count: int


@dataclass(frozen=True, slots=True)
class PlanTransitionView:
    plan_transition_id: UUID
    workspace_id: UUID
    root_task: TransitionNodeView
    from_generation: int
    to_generation: int
    status: str
    started_at: datetime
    completed_at: datetime | None
    version: int
    workers: tuple[WorkerTransitionView, ...]
    blocked_dependencies: tuple[TransitionDependencyView, ...]
    as_of: datetime


class PlanTransitionQueryStore(Protocol):
    def get_plan_transition(
        self, workspace_id: UUID, plan_transition_id: UUID
    ) -> PlanTransitionView | None:
        raise NotImplementedError

    def list_plan_transitions(
        self, workspace_id: UUID, root_task_id: UUID
    ) -> tuple[PlanTransitionSummaryView, ...]:
        raise NotImplementedError

    def get_worker_transition(
        self, workspace_id: UUID, worker_transition_id: UUID
    ) -> WorkerTransitionView | None:
        raise NotImplementedError


class PlanTransitionQueryService:
    def __init__(self, store: PlanTransitionQueryStore) -> None:
        self._store = store

    def get_plan_transition(
        self, workspace_id: UUID, plan_transition_id: UUID
    ) -> PlanTransitionView:
        result = self._store.get_plan_transition(workspace_id, plan_transition_id)
        if result is None:
            raise NotFound("plan transition not found")
        return result

    def list_plan_transitions(
        self, workspace_id: UUID, root_task_id: UUID
    ) -> tuple[PlanTransitionSummaryView, ...]:
        return self._store.list_plan_transitions(workspace_id, root_task_id)

    def get_worker_transition(
        self, workspace_id: UUID, worker_transition_id: UUID
    ) -> WorkerTransitionView:
        result = self._store.get_worker_transition(
            workspace_id, worker_transition_id
        )
        if result is None:
            raise NotFound("worker transition not found")
        return result
