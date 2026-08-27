from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sidera_control_plane.application.errors import NotFound
from sidera_control_plane.domain.plans import TaskPlanSnapshot


@dataclass(frozen=True, slots=True)
class RevisionBaseView:
    schema_version: int
    workspace_id: UUID
    root_task_id: UUID
    plan_snapshot_id: UUID
    plan_generation: int
    plan_content_hash: str
    root_task_version: int
    captured_at: datetime


@dataclass(frozen=True, slots=True)
class RevisionFreshnessView:
    base: RevisionBaseView
    current_plan_snapshot_id: UUID | None
    current_plan_generation: int | None
    current_plan_content_hash: str | None
    current_root_task_version: int | None
    base_fresh: bool
    stale_reasons: tuple[str, ...]
    checked_at: datetime


@dataclass(frozen=True, slots=True)
class RevisionTargetDescriptorView:
    target_generation: int
    parent_snapshot_id: UUID
    target_plan_content_hash: str


@dataclass(frozen=True, slots=True)
class RevisionFieldChangeView:
    field_path: str
    before: object
    after: object


@dataclass(frozen=True, slots=True)
class RevisionSubtaskChangeView:
    target_subtask_key: str
    path: str
    existing_task_id: UUID | None
    strategy: str
    field_changes: tuple[RevisionFieldChangeView, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class RevisionWorkerItemChangeView:
    target_worker_item_key: str
    path: str
    existing_worker_item_id: UUID | None
    strategy: str
    from_subtask_key: str | None
    to_subtask_key: str
    context_disposition: str
    field_changes: tuple[RevisionFieldChangeView, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RevisionDependencyChangeView:
    graph_scope: str
    upstream_key: str
    downstream_key: str
    upstream_path: str
    downstream_path: str
    dependency_kind: str
    change_type: str


@dataclass(frozen=True, slots=True)
class RevisionTransitionPreviewView:
    target_worker_item_key: str
    path: str
    worker_item_id: UUID | None
    allowed_actions: tuple[str, ...]
    fallback_action: str | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RevisionImpactCountsView:
    preserve: int
    move: int
    patch: int
    continue_: int
    create: int
    derive: int
    retire: int
    affected_branches: int


@dataclass(frozen=True, slots=True)
class AcceptedTransitionPolicyView:
    activate_immediately: bool
    budget_level: str
    failure_policy: str
    manual_gate: bool
    revision_operation: str | None


@dataclass(frozen=True, slots=True)
class RevisionDiffView:
    workspace_id: UUID
    root_task_id: UUID
    planning_request_id: UUID
    proposal_revision_id: UUID
    proposal_revision: int
    proposal_content_hash: str
    base: RevisionBaseView
    target_descriptor_preview: RevisionTargetDescriptorView
    root_changes: tuple[RevisionFieldChangeView, ...]
    subtask_changes: tuple[RevisionSubtaskChangeView, ...]
    worker_item_changes: tuple[RevisionWorkerItemChangeView, ...]
    subtask_dependency_changes: tuple[RevisionDependencyChangeView, ...]
    worker_dependency_changes: tuple[RevisionDependencyChangeView, ...]
    transition_preview: tuple[RevisionTransitionPreviewView, ...]
    impact_counts: RevisionImpactCountsView
    affected_branch_keys: tuple[str, ...]
    affected_branch_paths: tuple[str, ...]
    risk_flags: tuple[str, ...]
    diff_hash: str
    accepted_transition_policy: AcceptedTransitionPolicyView
    freshness: RevisionFreshnessView


@dataclass(frozen=True, slots=True)
class ActivePlanHeadView:
    workspace_id: UUID
    root_task_id: UUID
    active_snapshot_id: UUID
    active_generation: int
    active_snapshot_hash: str
    version: int
    updated_at: datetime
    snapshot: TaskPlanSnapshot


class PlanRevisionQueryStore(Protocol):
    def get_revision_freshness(
        self, workspace_id: UUID, request_id: UUID
    ) -> RevisionFreshnessView | None: ...

    def get_revision_diff(
        self, workspace_id: UUID, request_id: UUID
    ) -> RevisionDiffView | None: ...

    def get_active_plan(
        self, workspace_id: UUID, root_task_id: UUID
    ) -> ActivePlanHeadView | None: ...


class PlanRevisionQueryService:
    def __init__(self, store: PlanRevisionQueryStore) -> None:
        self._store = store

    def get_revision_freshness(
        self, workspace_id: UUID, request_id: UUID
    ) -> RevisionFreshnessView | None:
        return self._store.get_revision_freshness(workspace_id, request_id)

    def get_revision_diff(
        self, workspace_id: UUID, request_id: UUID
    ) -> RevisionDiffView:
        diff = self._store.get_revision_diff(workspace_id, request_id)
        if diff is None:
            raise NotFound("revision diff not found")
        return diff

    def get_active_plan(
        self, workspace_id: UUID, root_task_id: UUID
    ) -> ActivePlanHeadView:
        active_plan = self._store.get_active_plan(workspace_id, root_task_id)
        if active_plan is None:
            raise NotFound("task plan not found")
        return active_plan
