from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from sidera_control_plane.domain.plans import (
    DependencyEnforcement,
    DependencyKind,
    WorkItemKind,
)

SUPPORTED_PROJECTION_SCHEMA_VERSIONS = frozenset({1})


@dataclass(frozen=True, slots=True)
class ProjectionChange:
    workspace_id: UUID
    cursor: int
    schema_version: int
    projection_type: str
    resource_id: UUID | None
    change_type: str
    resource_version: int | None
    occurred_at: datetime
    correlation_id: UUID

    def to_payload(self) -> dict[str, Any]:
        return {
            "workspace_id": str(self.workspace_id),
            "cursor": self.cursor,
            "schema_version": self.schema_version,
            "projection_type": self.projection_type,
            "resource_id": str(self.resource_id) if self.resource_id else None,
            "change_type": self.change_type,
            "resource_version": self.resource_version,
            "occurred_at": self.occurred_at.astimezone(UTC).isoformat(),
            "correlation_id": str(self.correlation_id),
        }


class TaskNodeState(StrEnum):
    PLANNED = "planned"
    MATERIALIZED = "materialized"
    READY = "ready"
    QUEUED = "queued"
    ASSIGNED = "assigned"
    RUNNING = "running"
    WAITING_DEPENDENCY = "waiting_dependency"
    WAITING_USER = "waiting_user"
    BLOCKED = "blocked"
    RETRYING = "retrying"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


@dataclass(frozen=True, slots=True)
class TaskEdgeProjection:
    upstream_work_item_key: str
    downstream_work_item_key: str
    kind: DependencyKind
    enforcement: DependencyEnforcement
    scheduler_enforced: bool
    required_evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AttentionItemProjection:
    key: str
    kind: str
    title: str
    summary: str
    resource_id: UUID
    resource_version: int
    correlation_id: UUID
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class TaskNodeProjection:
    work_item_key: str
    task_id: UUID | None
    kind: WorkItemKind
    title: str
    goal_summary: str
    milestone_key: str | None
    worker_slot_key: str | None
    required_capabilities: tuple[str, ...]
    state: TaskNodeState
    execution_id: UUID | None
    session_id: UUID | None
    worker_id: UUID | None
    worker_display_name: str | None
    execution_horizon: bool
    critical_path: bool
    waiting_on_plan_dependency: bool
    scheduler_enforced: bool
    current_action: str | None
    evidence_satisfied: int
    evidence_required: int
    updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class MilestoneProjection:
    key: str
    title: str
    state: str
    materialized_count: int
    completed_count: int
    planned_count: int
    order_hint: int


@dataclass(frozen=True, slots=True)
class TaskOverviewProjection:
    schema_version: int
    workspace_id: UUID
    root_task_id: UUID
    plan_snapshot_id: UUID
    generation: int
    title: str
    goal_summary: str
    acceptance_refs: tuple[str, ...]
    current_milestone_key: str | None
    materialized_count: int
    planned_count: int
    running_count: int
    completed_count: int
    failed_count: int
    next_work_item_keys: tuple[str, ...]
    attention_items: tuple[AttentionItemProjection, ...]
    last_change_cursor: int
    as_of: datetime


@dataclass(frozen=True, slots=True)
class TaskGraphProjection:
    schema_version: int
    overview: TaskOverviewProjection
    nodes: tuple[TaskNodeProjection, ...]
    edges: tuple[TaskEdgeProjection, ...]
    milestones: tuple[MilestoneProjection, ...]


@dataclass(frozen=True, slots=True)
class ExecutionSummaryProjection:
    execution_id: UUID
    session_id: UUID | None
    worker_id: UUID | None
    status: str
    attempt: int
    started_at: datetime | None
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class ArtifactSummaryProjection:
    artifact_id: UUID
    artifact_type: str
    media_type: str
    size_bytes: int
    content_available: bool


@dataclass(frozen=True, slots=True)
class EvidenceSummaryProjection:
    evidence_id: UUID
    acceptance_ref: str
    conclusion: str
    artifact_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class CheckpointSummaryProjection:
    sequence: int
    title: str
    artifact_ids: tuple[UUID, ...]
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class TaskDetailProjection:
    schema_version: int
    last_change_cursor: int
    node: TaskNodeProjection
    acceptance_refs: tuple[str, ...]
    expected_outputs: tuple[str, ...]
    dependencies: tuple[TaskEdgeProjection, ...]
    executions: tuple[ExecutionSummaryProjection, ...]
    artifacts: tuple[ArtifactSummaryProjection, ...]
    evidence: tuple[EvidenceSummaryProjection, ...]
    checkpoints: tuple[CheckpointSummaryProjection, ...]


@dataclass(frozen=True, slots=True)
class ActivityItemProjection:
    source_type: str
    source_id: UUID
    occurred_at: datetime
    kind: str
    title: str
    summary: str
    correlation_id: UUID
    causal_parent_id: UUID | None
    scheduler_enforced: bool


@dataclass(frozen=True, slots=True)
class TaskActivityProjection:
    schema_version: int
    last_change_cursor: int
    items: tuple[ActivityItemProjection, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class PlanDiffEntryProjection:
    work_item_key: str
    change: Literal["unchanged", "patched", "added", "removed"]
    changed_fields: tuple[str, ...]
    reuse_candidate: bool
    context_patch_candidate: bool
    portable_replay_candidate: bool
    runtime_execution_id: UUID | None


@dataclass(frozen=True, slots=True)
class PlanDiffProjection:
    schema_version: int
    last_change_cursor: int
    root_task_id: UUID
    left_generation: int
    right_generation: int
    entries: tuple[PlanDiffEntryProjection, ...]


@dataclass(frozen=True, slots=True)
class MainAgentSlotProjection:
    available: bool
    request_revision_supported: bool
    entry_uri: str | None


@dataclass(frozen=True, slots=True)
class WorkspaceCapabilitiesProjection:
    schema_version: int
    main_agent: MainAgentSlotProjection


class TraceCategory(StrEnum):
    INPUT = "input"
    ASSISTANT_SUMMARY = "assistant_summary"
    MODEL = "model"
    TOOL = "tool"
    SUBTOOL = "subtool"
    ARTIFACT = "artifact"
    CHECKPOINT = "checkpoint"
    SCHEDULER = "scheduler"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class TraceUsage:
    input_tokens: int | None
    output_tokens: int | None
    cached_tokens: int | None
    cost_microunits: int | None


@dataclass(frozen=True, slots=True)
class TraceTimingSummary:
    input_ms: int
    model_ms: int
    tools_ms: int
    total_ms: int


@dataclass(frozen=True, slots=True)
class WorkerTraceEvent:
    schema_version: int
    workspace_id: UUID
    root_task_id: UUID
    work_item_key: str | None
    plan_snapshot_id: UUID | None
    execution_id: UUID
    session_id: UUID
    sequence: int
    occurred_at: datetime
    category: TraceCategory
    status: str
    title: str
    summary: str
    duration_ms: int | None
    usage: TraceUsage | None
    artifact_refs: tuple[UUID, ...]
    provider_payload_ref: str | None
    provider_extensions: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class WorkerTraceProjection:
    schema_version: int
    execution_id: UUID
    session_id: UUID
    worker_id: UUID | None
    status: str
    current_action: str | None
    plan_generation: int | None
    checkpoint_sequence: int | None
    freshness_at: datetime | None
    last_change_cursor: int
    timing: TraceTimingSummary
    events: tuple[WorkerTraceEvent, ...]
    previous_before_sequence: int | None


@dataclass(frozen=True, slots=True)
class WorkerContextBranchProjection:
    root_task_id: UUID
    plan_snapshot_id: UUID
    work_item_key: str
    worker_slot_key: str
    execution_lineage_id: UUID
    current_execution_id: UUID
    current_session_id: UUID
    head_checkpoint_sequence: int | None
    artifact_refs: tuple[UUID, ...]
    replay_capability: Literal["none", "portable_replay", "native_resume"]
