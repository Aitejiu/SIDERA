from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sidera_control_plane.application.errors import NotFound


@dataclass(frozen=True, slots=True)
class PlanningCapabilityView:
    structured_output: str
    clarification: bool
    idempotency: str
    cancellation: str
    usage_reporting: str
    max_context_bytes: int
    stability: str
    interface_version: str
    proposal_schema_versions: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class PlanningProfileView:
    id: UUID
    workspace_id: UUID
    profile_key: str
    version: int
    display_name: str
    planner_provider: str
    planner_module_instance_id: UUID
    planner_module_config_version: int
    planner_interface_version: str
    status: str
    user_selectable: bool
    capabilities: PlanningCapabilityView
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PlanningBriefView:
    planning_request_id: UUID
    revision: int
    objective: str
    constraints: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    context_refs: tuple[str, ...]
    requested_outputs: tuple[str, ...]
    user_feedback: str | None
    source_correlation: UUID
    created_by: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PlanningUsageView:
    input_tokens: int
    output_tokens: int
    cost_microunits: int
    runtime_seconds: int
    context_bytes: int


@dataclass(frozen=True, slots=True)
class PlanningRunView:
    id: UUID
    planning_request_id: UUID
    workspace_id: UUID
    brief_revision: int
    attempt: int
    operation_id: UUID
    status: str
    result_kind: str | None
    capability_snapshot: PlanningCapabilityView
    usage: PlanningUsageView
    provider_ref: str | None
    error_code: str | None
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    version: int


@dataclass(frozen=True, slots=True)
class PlanningClarificationView:
    id: UUID
    planning_request_id: UUID
    planning_run_id: UUID
    question: str
    rationale: str
    answer_type: str
    options: tuple[str, ...]
    blocking: bool
    answer: str | None
    answered_by: str | None
    answered_at: datetime | None
    answer_brief_revision: int | None
    created_at: datetime
    version: int


@dataclass(frozen=True, slots=True)
class PlanningRequestSummaryView:
    id: UUID
    workspace_id: UUID
    kind: str
    status: str
    source_type: str
    source_ref: str | None
    current_brief_revision: int
    current_proposal_revision: int | None
    current_proposal_content_hash: str | None
    planning_profile_id: UUID
    planning_profile_version: int
    profile_key: str | None
    current_run_id: UUID | None
    requires_attention: bool
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PlanningRequestDetailView:
    summary: PlanningRequestSummaryView
    profile: PlanningProfileView | None
    active_run: PlanningRunView | None
    pending_clarifications: tuple[PlanningClarificationView, ...]
    usage: PlanningUsageView
    last_change_cursor: int


@dataclass(frozen=True, slots=True)
class PlanningProposalView:
    id: UUID
    planning_request_id: UUID
    revision: int
    brief_revision: int
    planning_run_id: UUID
    proposal_schema_version: int
    kind: str
    profile_version: int
    created_at: datetime
    content_hash: str
    derived_diff: dict[str, object]
    display: dict[str, object]


@dataclass(frozen=True, slots=True)
class PlanningMaterializationView:
    id: UUID
    workspace_id: UUID
    planning_request_id: UUID
    proposal_revision_id: UUID
    root_task_id: UUID
    plan_snapshot_id: UUID
    plan_generation: int
    subtask_task_mapping: dict[str, str]
    worker_item_mapping: dict[str, str]
    execution_request_mapping: dict[str, str]
    deep_link: str
    confirmed_by: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PlanningPage[T]:
    items: tuple[T, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class PlanningRequestPage:
    items: tuple[PlanningRequestSummaryView, ...]
    next_cursor: str | None
    last_change_cursor: int


class PlanningQueryStore(Protocol):
    def list_profiles(
        self, workspace_id: UUID, *, limit: int, cursor: str | None
    ) -> PlanningPage[PlanningProfileView]: ...

    def list_requests(
        self,
        workspace_id: UUID,
        *,
        limit: int,
        cursor: str | None,
        status: str | None,
        closed_status: bool | None,
        requires_attention: bool,
    ) -> PlanningRequestPage: ...

    def get_request_detail(
        self, workspace_id: UUID, request_id: UUID
    ) -> PlanningRequestDetailView | None: ...

    def list_briefs(
        self, workspace_id: UUID, request_id: UUID
    ) -> tuple[PlanningBriefView, ...] | None: ...

    def list_runs(
        self, workspace_id: UUID, request_id: UUID
    ) -> tuple[PlanningRunView, ...] | None: ...

    def list_clarifications(
        self, workspace_id: UUID, request_id: UUID
    ) -> tuple[PlanningClarificationView, ...] | None: ...

    def get_proposal(
        self, workspace_id: UUID, request_id: UUID, revision: int
    ) -> PlanningProposalView | None: ...

    def get_materialization(
        self, workspace_id: UUID, request_id: UUID
    ) -> PlanningMaterializationView | None: ...


class PlanningQueryService:
    def __init__(self, store: PlanningQueryStore) -> None:
        self._store = store

    def list_profiles(
        self, workspace_id: UUID, *, limit: int, cursor: str | None
    ) -> PlanningPage[PlanningProfileView]:
        _validate_limit(limit)
        return self._store.list_profiles(workspace_id, limit=limit, cursor=cursor)

    def list_requests(
        self,
        workspace_id: UUID,
        *,
        limit: int,
        cursor: str | None,
        status: str | None,
        closed_status: bool | None,
        requires_attention: bool,
    ) -> PlanningRequestPage:
        _validate_limit(limit)
        return self._store.list_requests(
            workspace_id,
            limit=limit,
            cursor=cursor,
            status=status,
            closed_status=closed_status,
            requires_attention=requires_attention,
        )

    def get_request_detail(
        self, workspace_id: UUID, request_id: UUID
    ) -> PlanningRequestDetailView:
        detail = self._store.get_request_detail(workspace_id, request_id)
        if detail is None:
            raise NotFound("planning request not found")
        return detail

    def list_briefs(
        self, workspace_id: UUID, request_id: UUID
    ) -> tuple[PlanningBriefView, ...]:
        briefs = self._store.list_briefs(workspace_id, request_id)
        if briefs is None:
            raise NotFound("planning request not found")
        return briefs

    def list_runs(
        self, workspace_id: UUID, request_id: UUID
    ) -> tuple[PlanningRunView, ...]:
        runs = self._store.list_runs(workspace_id, request_id)
        if runs is None:
            raise NotFound("planning request not found")
        return runs

    def list_clarifications(
        self, workspace_id: UUID, request_id: UUID
    ) -> tuple[PlanningClarificationView, ...]:
        clarifications = self._store.list_clarifications(workspace_id, request_id)
        if clarifications is None:
            raise NotFound("planning request not found")
        return clarifications

    def get_proposal(
        self, workspace_id: UUID, request_id: UUID, revision: int
    ) -> PlanningProposalView:
        proposal = self._store.get_proposal(workspace_id, request_id, revision)
        if proposal is None:
            raise NotFound("planning proposal not found")
        return proposal

    def get_materialization(
        self, workspace_id: UUID, request_id: UUID
    ) -> PlanningMaterializationView:
        materialization = self._store.get_materialization(workspace_id, request_id)
        if materialization is None:
            raise NotFound("planning materialization not found")
        return materialization


def _validate_limit(limit: int) -> None:
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
