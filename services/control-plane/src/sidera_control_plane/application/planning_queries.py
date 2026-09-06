from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sidera_control_plane.application.errors import NotFound
from sidera_control_plane.domain.result_inputs import InputGroupInvalid, InputGroupV1


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
    context_package_versions: tuple[int, ...] = ()


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
class PlanningCollaborationView:
    mode: str
    default_strategy: str | None
    subtask_overrides: tuple[dict[str, object], ...]
    provider: str | None
    runtime_interface: str | None
    runtime_config_version: int | None
    input_groups: tuple[InputGroupV1, ...] = ()

    @classmethod
    def from_request_mapping(
        cls,
        request: object,
        profile_snapshot: object,
        *,
        provider: str | None = None,
    ) -> PlanningCollaborationView:
        request_mapping = request if isinstance(request, Mapping) else {}
        mode_value = request_mapping.get("mode")
        mode = (
            mode_value
            if isinstance(mode_value, str) and mode_value in {"disabled", "enabled"}
            else "disabled"
        )
        default_policy = request_mapping.get("default_policy")
        default_mapping = default_policy if isinstance(default_policy, Mapping) else {}
        default_strategy = (
            _safe_text(default_mapping.get("strategy_id"))
            if mode == "enabled"
            else None
        )
        raw_overrides = request_mapping.get("subtask_overrides")
        overrides: list[dict[str, object]] = []
        if isinstance(raw_overrides, (list, tuple)):
            for raw_override in raw_overrides:
                if not isinstance(raw_override, Mapping):
                    continue
                subtask_key = _safe_text(raw_override.get("subtask_key"))
                strategy_id = _safe_text(raw_override.get("strategy_id"))
                enabled = raw_override.get("enabled")
                if (
                    subtask_key is None
                    or strategy_id is None
                    or not isinstance(enabled, bool)
                ):
                    continue
                overrides.append(
                    {
                        "subtask_key": subtask_key,
                        "enabled": enabled,
                        "strategy_id": strategy_id,
                    }
                )

        binding_value = (
            profile_snapshot.get("collaboration_runtime_binding")
            if isinstance(profile_snapshot, Mapping)
            else None
        )
        binding = binding_value if isinstance(binding_value, Mapping) else {}
        runtime_interface = (
            _safe_text(binding.get("runtime_interface_version"))
            if mode == "enabled"
            else None
        )
        config_version = binding.get("runtime_config_version")
        runtime_config_version = (
            config_version
            if mode == "enabled"
            and isinstance(config_version, int)
            and not isinstance(config_version, bool)
            and config_version >= 1
            else None
        )
        safe_provider = _safe_text(provider) if mode == "enabled" else None
        return cls(
            mode=mode,
            default_strategy=default_strategy,
            subtask_overrides=tuple(overrides),
            provider=safe_provider,
            runtime_interface=runtime_interface,
            runtime_config_version=runtime_config_version,
            input_groups=_safe_input_groups(
                request_mapping.get("input_groups") if mode == "enabled" else None
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "default_strategy": self.default_strategy,
            "subtask_overrides": [dict(item) for item in self.subtask_overrides],
            "provider": self.provider,
            "runtime_interface": self.runtime_interface,
            "runtime_config_version": self.runtime_config_version,
            "input_groups": [item.to_mapping() for item in self.input_groups],
        }


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
    collaboration: PlanningCollaborationView


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


def _safe_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _safe_input_groups(value: object) -> tuple[InputGroupV1, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    groups: list[InputGroupV1] = []
    for item in value[:32]:
        if not isinstance(item, Mapping):
            continue
        try:
            groups.append(InputGroupV1.from_mapping(item))
        except InputGroupInvalid:
            continue
    return tuple(groups)


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
