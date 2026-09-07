from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from fastapi.encoders import jsonable_encoder
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_serializer,
    model_validator,
)

from sidera_control_plane.application.collaboration_metrics import (
    CollaborationMetricsView,
)
from sidera_control_plane.application.command_queries import CommandStatusView
from sidera_control_plane.application.context_package_queries import (
    ContextPackageAcknowledgementView,
    ContextPackageAssemblyView,
    ContextPackageMaterializationAuditView,
    ContextPackageView,
    KnowledgeSubmissionPageView,
    KnowledgeSubmissionView,
)
from sidera_control_plane.application.module_queries import ModuleSummaryView
from sidera_control_plane.application.notification_delivery import (
    NotificationDeliveryEnvelope as NotificationDeliveryEnvelopeView,
)
from sidera_control_plane.application.notification_delivery import (
    NotificationDeliverySettlement,
)
from sidera_control_plane.application.notification_metrics import (
    NotificationMetricsView,
)
from sidera_control_plane.application.notification_queries import (
    NotificationAttemptView,
    NotificationDeliveryView,
    NotificationFactView,
    NotificationSummaryView,
    NotificationThreadDetailView,
    NotificationThreadView,
)
from sidera_control_plane.application.plan_revision_queries import (
    AcceptedTransitionPolicyView,
    ActivePlanHeadView,
    RevisionBaseView,
    RevisionDependencyChangeView,
    RevisionDiffView,
    RevisionFieldChangeView,
    RevisionFreshnessView,
    RevisionImpactCountsView,
    RevisionInputGroupChangeView,
    RevisionSubtaskChangeView,
    RevisionTargetDescriptorView,
    RevisionTransitionPreviewView,
    RevisionWorkerItemChangeView,
)
from sidera_control_plane.application.planning_metrics import (
    PlanningMetricsView,
)
from sidera_control_plane.application.planning_queries import (
    PlanningBriefView,
    PlanningCapabilityView,
    PlanningClarificationView,
    PlanningCollaborationView,
    PlanningMaterializationView,
    PlanningProfileView,
    PlanningProposalView,
    PlanningRequestDetailView,
    PlanningRequestSummaryView,
    PlanningRunView,
    PlanningUsageView,
)
from sidera_control_plane.application.resource_queries import (
    ArtifactView,
    EvidenceClaimView,
    ExecutionBindingSummaryView,
    ExecutionBindingView,
    ExecutionEventView,
    ExecutionInputView,
    ExecutionSessionView,
    ExecutionView,
    ExecutionWorkspaceView,
    LeaseView,
    WorkerSummaryView,
    WorkerView,
)
from sidera_control_plane.application.runtime_metrics import (
    RuntimeMetricsView,
)
from sidera_control_plane.application.task_queries import (
    TaskPage,
    TaskView,
    TimelineItem,
)
from sidera_control_plane.application.work_context_queries import (
    WorkContextDetailView,
    WorkContextRevisionSummaryView,
    WorkContextRevisionView,
)
from sidera_control_plane.collaboration_readiness import ReadinessReport
from sidera_control_plane.domain.commands import CommandEnvelope, CommandResult
from sidera_control_plane.domain.context_packages import (
    ContextPackageManifestV2,
    ContextPackageManifestV3,
)
from sidera_control_plane.domain.plans import TaskPlanSnapshot
from sidera_control_plane.domain.result_inputs import (
    InputGroupCandidateV1,
    InputGroupV1,
)
from sidera_control_plane.domain.work_context import WorkContextSnapshotV1
from sidera_control_plane.notification_readiness import NotificationReadinessReport


def _plain_revision_value(value: object) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_revision_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain_revision_value(item) for item in value]
    return value


NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
InputText = Annotated[str, StringConstraints(min_length=1)]
AggregateType = Literal[
    "task",
    "worker",
    "execution",
    "execution_session",
    "lease",
    "workspace",
    "planning_request",
    "planning_run",
    "work_context",
    "worker_runtime_configuration",
    "module_definition",
    "module_instance",
    "context_assembly",
    "context_package",
    "worker_transition",
    "worker_item",
    "handoff_proposal",
    "handoff_transition",
    "collaboration_governance_hold",
    "collaboration_release_proposal",
    "notification_delivery",
    "notification_route",
    "notification_thread",
]


class ActorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: NonEmptyString
    id: NonEmptyString


class CreateTaskPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: NonEmptyString
    description: str
    acceptance_criteria: tuple[NonEmptyString, ...] = Field(min_length=1)
    priority: int = Field(ge=0, le=100)


class SetTaskPriorityPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    priority: int = Field(ge=0, le=100)


class EmptyPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReworkSuggestionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: UUID
    candidate_hash: NonEmptyString
    issue_keys: tuple[NonEmptyString, ...] = Field(default_factory=tuple)
    target_worker_item_ids: tuple[UUID, ...] = Field(min_length=1)
    reason: NonEmptyString = Field(max_length=2000)


class RequestTaskReworkPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: UUID
    candidate_hash: NonEmptyString
    mode: Literal["automatic", "user"]
    issue_keys: tuple[NonEmptyString, ...] = Field(default_factory=tuple)
    target_worker_item_ids: tuple[UUID, ...] = Field(default_factory=tuple)
    impact_worker_item_ids: tuple[UUID, ...] = Field(default_factory=tuple)
    reason: str = Field(default="", max_length=2000)
    suggestion: ReworkSuggestionPayload | None = None


class AuthorizeAdditionalReworkPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    additional_rounds: int = Field(ge=0)
    additional_seconds: int = Field(ge=0)
    reason: NonEmptyString = Field(max_length=2000)


class PlannerCapabilityPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    structured_output: NonEmptyString
    clarification: bool
    idempotency: NonEmptyString
    cancellation: NonEmptyString
    usage_reporting: NonEmptyString
    max_context_bytes: int = Field(gt=0)
    stability: NonEmptyString
    interface_version: NonEmptyString
    proposal_schema_versions: tuple[int, ...] = Field(min_length=1)
    context_package_versions: tuple[int, ...] = Field(default_factory=tuple)


class RegisterPlannerModulePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: NonEmptyString
    interface_version: NonEmptyString
    config_schema_version: int = Field(ge=1)
    capabilities: PlannerCapabilityPayload
    instance_name: NonEmptyString
    config_namespace: NonEmptyString
    config: dict[str, Any] = Field(default_factory=dict)
    secret_refs: tuple[NonEmptyString, ...] = Field(default_factory=tuple)


class CollaborationRuntimeCapabilityPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interface_version: NonEmptyString
    projection_schema_versions: tuple[int, ...] = Field(min_length=1)
    decision_schema_versions: tuple[int, ...] = Field(min_length=1)
    strategy_ids: tuple[NonEmptyString, ...] = Field(min_length=1)
    idempotency: NonEmptyString
    max_projection_bytes: int = Field(gt=0)
    max_decision_bytes: int = Field(gt=0)
    max_intents_per_decision: int = Field(gt=0)
    max_strategy_state_bytes: int = Field(gt=0)
    stability: NonEmptyString


class RegisterCollaborationRuntimeModulePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: NonEmptyString
    interface_version: NonEmptyString
    config_schema_version: int = Field(ge=1)
    capabilities: CollaborationRuntimeCapabilityPayload
    instance_name: NonEmptyString
    config_namespace: NonEmptyString
    config: dict[str, Any] = Field(default_factory=dict)
    secret_refs: tuple[NonEmptyString, ...] = Field(default_factory=tuple)


class PlanningBudgetPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_context_bytes: int = Field(gt=0)
    max_tokens: int = Field(gt=0)
    max_cost_microunits: int = Field(gt=0)
    max_runtime_seconds: int = Field(gt=0)
    max_revisions: int = Field(gt=0)
    max_nodes: int = Field(gt=0)
    max_concurrency: int = Field(gt=0)


class PlanningRetryPolicyPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_retries: int = Field(ge=0)
    backoff_seconds: tuple[int, ...] = Field(default_factory=tuple)
    unknown_outcome_policy: NonEmptyString
    invalid_output_policy: NonEmptyString


class RegisterPlanningProfilePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_key: NonEmptyString
    version: int = Field(ge=1)
    display_name: NonEmptyString
    planner_module_instance_id: UUID
    planner_module_config_version: int = Field(ge=1)
    planner_interface_version: NonEmptyString
    prompt_policy_version: NonEmptyString
    system_policy: dict[str, Any] = Field(default_factory=dict)
    proposal_schema_version: int = Field(ge=1)
    allowed_capabilities: tuple[NonEmptyString, ...] = Field(min_length=1)
    adapter_preferences: tuple[NonEmptyString, ...] = Field(default_factory=tuple)
    adapter_forbidden: tuple[NonEmptyString, ...] = Field(default_factory=tuple)
    budget: PlanningBudgetPayload
    retry_policy: PlanningRetryPolicyPayload
    default_priority: int = Field(ge=0, le=100)
    execution_max_attempts: int = Field(ge=1)
    execution_budget_level: NonEmptyString
    user_selectable: bool = True
    planner_secret_refs: tuple[NonEmptyString, ...] = Field(default_factory=tuple)
    collaboration_runtime_definition_id: UUID | None = None
    collaboration_runtime_instance_id: UUID | None = None
    collaboration_runtime_config_version: int | None = Field(default=None, ge=1)


class SubmitPlanningRequestPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: UUID
    profile_version: int = Field(ge=1)
    objective: NonEmptyString
    constraints: tuple[NonEmptyString, ...] = Field(default_factory=tuple)
    acceptance_criteria: tuple[NonEmptyString, ...] = Field(min_length=1)
    context_refs: tuple[NonEmptyString, ...] = Field(default_factory=tuple)
    requested_outputs: tuple[NonEmptyString, ...] = Field(min_length=1)
    source_type: NonEmptyString
    source_ref: NonEmptyString | None = None


class SubmitPlanRevisionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root_task_id: UUID
    expected_base_snapshot_id: UUID
    expected_base_generation: int = Field(gt=0)
    expected_base_content_hash: NonEmptyString
    objective: NonEmptyString
    constraints: tuple[NonEmptyString, ...] = Field(default_factory=tuple)
    acceptance_criteria: tuple[NonEmptyString, ...] = Field(min_length=1)
    planning_profile_id: UUID
    planning_profile_version: int = Field(gt=0)


class CancelPlanningRequestPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlanningClarificationAnswerPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clarification_id: UUID
    answer: NonEmptyString


class AnswerPlanningClarificationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answers: tuple[PlanningClarificationAnswerPayload, ...] = Field(min_length=1)


class RequestProposalRevisionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_proposal_revision: int = Field(ge=1)
    current_proposal_content_hash: NonEmptyString
    feedback: NonEmptyString


class RejectPlanningProposalPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_proposal_revision: int = Field(ge=1)
    current_proposal_content_hash: NonEmptyString
    reason: NonEmptyString


class ConfirmPlanningProposalPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_revision: int = Field(ge=1)
    proposal_content_hash: NonEmptyString
    profile_id: UUID
    profile_version: int = Field(ge=1)


class ConfirmPlanRevisionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_proposal_revision: int = Field(ge=1)
    expected_proposal_content_hash: NonEmptyString
    expected_diff_hash: NonEmptyString
    expected_base_snapshot_id: UUID
    expected_base_content_hash: NonEmptyString
    profile_id: UUID
    profile_version: int = Field(ge=1)


class RetryBlockedPlanningRunPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unknown_outcome_decision: Literal["retry"]


class RetryContextAssemblyPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class RevokeContextPackagePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_package_status: Literal["active", "revoked", "corrupt"]
    expected_package_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    revocation_scope: Literal["new_starts", "all_materialization"]
    reason: NonEmptyString


class AcceptedDecisionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: NonEmptyString
    statement: NonEmptyString
    source: NonEmptyString
    ordinal: int = Field(ge=0)


class UnresolvedQuestionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: NonEmptyString
    question: NonEmptyString
    blocking: bool
    ordinal: int = Field(ge=0)


class WorkReferencePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref_id: UUID
    ref_type: NonEmptyString
    ordinal: int = Field(ge=0)


class KnowledgeRevisionRefPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_id: UUID
    revision_id: UUID
    revision_hash: NonEmptyString
    section_key: NonEmptyString
    section_hash: NonEmptyString
    source_ids: tuple[UUID, ...] = Field(default_factory=tuple)
    ordinal: int = Field(ge=0)


class WorkContextSemanticPatchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objective: NonEmptyString | None = None
    constraints: tuple[NonEmptyString, ...] | None = None
    acceptance_criteria: tuple[NonEmptyString, ...] | None = None
    requested_outputs: tuple[NonEmptyString, ...] | None = None
    accepted_decisions: tuple[AcceptedDecisionPayload, ...] = Field(
        default_factory=tuple
    )
    unresolved_questions: tuple[UnresolvedQuestionPayload, ...] = Field(
        default_factory=tuple
    )
    artifact_refs: tuple[WorkReferencePayload, ...] = Field(default_factory=tuple)
    evidence_refs: tuple[WorkReferencePayload, ...] = Field(default_factory=tuple)
    checkpoint_refs: tuple[WorkReferencePayload, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def require_change(self) -> "WorkContextSemanticPatchPayload":
        if not any(
            value is not None and value != ()
            for value in (
                self.objective,
                self.constraints,
                self.acceptance_criteria,
                self.requested_outputs,
                self.accepted_decisions,
                self.unresolved_questions,
                self.artifact_refs,
                self.evidence_refs,
                self.checkpoint_refs,
            )
        ):
            raise ValueError("semantic patch must contain at least one change")
        return self


class ReviseWorkContextPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_revision: int = Field(ge=1)
    base_content_hash: NonEmptyString
    reason: Literal["replanned", "user_intervention"]
    patch: WorkContextSemanticPatchPayload


class AttachKnowledgeRevisionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    knowledge_ref: KnowledgeRevisionRefPayload
    selection_reason: NonEmptyString


class ArchiveWorkContextPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_revision: int = Field(ge=1)
    current_content_hash: NonEmptyString


class AcknowledgeAttentionItemPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attention_key: NonEmptyString
    source_correlation_id: UUID
    source_resource_version: int = Field(ge=1)


class MarkNotificationThreadReadPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_latest_fact_id: UUID


class ArchiveNotificationThreadPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RequeueNotificationDeliveryPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_status: Literal["exhausted"]
    expected_route_version: int = Field(ge=1)
    reason: NonEmptyString = Field(max_length=256)


class ArtifactContentDescriptorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: UUID
    workspace_id: UUID
    media_type: str
    byte_size: int = Field(ge=0)
    content_hash: str
    download_name: NonEmptyString
    content_available: bool
    unavailable_reason: str | None


class DownloadTokenResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: NonEmptyString


class AttentionItemProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    kind: str
    title: str
    summary: str
    resource_id: UUID
    resource_version: int
    correlation_id: UUID
    occurred_at: datetime
    worker_item_id: UUID | None = None
    deep_link: str | None = None


class PlanHeadProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active_snapshot_id: UUID
    active_generation: int
    active_snapshot_hash: str
    version: int
    updated_at: datetime


class PlanRevisionProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    planning_request_id: UUID
    status: str
    proposal_revision: int | None
    proposal_content_hash: str | None
    base_generation: int | None
    target_generation: int | None
    version: int
    correlation_id: UUID
    updated_at: datetime
    subtask_path: tuple[str, ...]
    summary: str | None
    action: str | None


class PlanRevisionAttentionProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    planning_request_id: UUID
    status: str
    subtask_path: tuple[str, ...]
    summary: str
    action: str
    deep_link: str
    correlation_id: UUID
    resource_version: int
    occurred_at: datetime


class PlanConvergenceProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_transition_id: UUID | None
    from_generation: int | None
    target_generation: int | None
    status: str
    total_count: int
    converged_count: int
    blocked_count: int
    unknown_count: int
    in_flight_count: int
    has_in_flight: bool
    has_unknown: bool
    has_active_fence: bool
    confirmation_blocked: bool
    started_at: datetime | None
    completed_at: datetime | None


class SubtaskConvergenceProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subtask_key: str
    status: str
    plan_transition_id: UUID | None
    source_generation: int | None
    target_generation: int | None
    total_count: int
    converged_count: int
    blocked_count: int
    unknown_count: int
    blocked_predecessor_paths: tuple[tuple[str, ...], ...]


class WorkerPlanSyncProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_transition_id: UUID
    plan_transition_id: UUID
    action: str
    status: str
    writer_state: str
    source_generation: int
    target_generation: int
    current_context_version: int
    acknowledged_context_version: int
    target_context_version: int
    patch_id: UUID | None
    patch_acknowledged: bool
    source_execution_id: UUID | None
    source_session_id: UUID | None
    target_execution_id: UUID | None
    target_session_id: UUID | None
    continuation_execution_id: UUID | None
    continuation_session_id: UUID | None
    queued_input_ids: tuple[UUID, ...]
    superseded_input_ids: tuple[UUID, ...]
    failure_code: str | None
    started_at: datetime | None
    completed_at: datetime | None
    trace_href: str | None


class TaskOverviewProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

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
    attention_items: tuple[AttentionItemProjectionResponse, ...]
    last_change_cursor: int
    as_of: datetime
    plan_head: PlanHeadProjectionResponse | None = None
    active_revision: PlanRevisionProjectionResponse | None = None
    convergence: PlanConvergenceProjectionResponse | None = None
    revision_attention: PlanRevisionAttentionProjectionResponse | None = None
    handoff_running_count: int = 0
    handoff_blocked_count: int = 0


class TaskNodeProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    work_item_key: str
    task_id: UUID | None
    kind: str
    title: str
    goal_summary: str
    milestone_key: str | None
    worker_slot_key: str | None
    required_capabilities: tuple[str, ...]
    state: str
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
    plan_sync: WorkerPlanSyncProjectionResponse | None = None
    handoff_running: bool = False
    handoff_blocked: bool = False
    handoff_worker_item_id: UUID | None = None


class TaskEdgeProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    upstream_work_item_key: str
    downstream_work_item_key: str
    kind: str
    enforcement: str
    scheduler_enforced: bool
    required_evidence_refs: tuple[str, ...]


class MilestoneProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    title: str
    state: str
    materialized_count: int
    completed_count: int
    planned_count: int
    order_hint: int


class TaskGraphProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int
    overview: TaskOverviewProjectionResponse
    nodes: tuple[TaskNodeProjectionResponse, ...]
    edges: tuple[TaskEdgeProjectionResponse, ...]
    milestones: tuple[MilestoneProjectionResponse, ...]


class TaskConsoleAttentionProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    kind: str
    task_title: str
    subtask_key: str
    subtask_title: str
    reason: str
    resource_id: UUID
    resource_version: int
    correlation_id: UUID
    occurred_at: datetime
    waiting_reason: str | None = None
    waiting_details_summary: str | None = None
    requires_operator: bool = False
    blocked_since: datetime | None = None
    worker_item_id: UUID | None = None
    deep_link: str | None = None


class CollaborationLinkResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subtask_key: str
    title: str
    href: str


class BlockedSubtaskResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subtask_key: str
    title: str
    reason: str
    last_failure_at: datetime | None
    href: str


class RootCollaborationSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    status: Literal["running", "waiting", "attention", "completed"]
    strategy_label: str
    run_id: UUID
    plan_generation: int
    released_count: int
    running_count: int
    waiting_count: int
    completed_count: int
    blocked_subtask_count: int
    blocked_subtasks: tuple[BlockedSubtaskResponse, ...]
    latest_decision_summary: str | None
    latest_decision_at: datetime | None
    attention_required: bool
    detail_href: str
    paused_worker_item_count: int = 0
    pending_release_proposal_count: int = 0
    attention_subtasks: tuple["CollaborationAttentionSubtaskResponse", ...] = ()


class SubtaskCollaborationSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    effective_strategy: str
    required_input_count: int
    satisfied_input_count: int
    waiting_upstream_subtasks: tuple[CollaborationLinkResponse, ...]
    missing_artifact_refs: tuple[str, ...]
    missing_evidence_refs: tuple[str, ...]
    released_worker_item_count: int
    waiting_worker_item_count: int


class CollaborationReleaseProposalSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: UUID
    status: Literal["pending", "approved", "rejected", "stale"]
    resource_version: int
    reason_code: str
    summary: str
    created_at: datetime
    target_request_version: int
    resolved_at: datetime | None = None
    input_selection_id: UUID | None = None
    input_selection_hash: str | None = None


class CollaborationGovernanceHistoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal[
        "hold",
        "runtime_intent",
        "application",
        "proposal",
        "resolution",
        "release",
    ]
    resource_id: UUID
    status: Literal[
        "active",
        "resumed",
        "superseded",
        "created",
        "deferred",
        "pending",
        "applied",
        "approved",
        "rejected",
        "stale",
        "duplicate",
        "issued",
    ]
    summary: str
    occurred_at: datetime
    result_code: str | None = None
    proposal_id: UUID | None = None
    resolution_action: Literal["approve", "reject"] | None = None
    resolution_result: Literal["applied", "stale"] | None = None
    resolution_actor: str | None = None
    resolution_transport: Literal["web", "main_agent"] | None = None
    release_id: UUID | None = None


class WorkerItemCollaborationSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    state: Literal[
        "unmanaged",
        "waiting_dependency",
        "waiting_artifact",
        "waiting_evidence",
        "waiting_release",
        "released",
        "materialized",
    ]
    waiting_summary: str | None
    upstream_links: tuple[CollaborationLinkResponse, ...]
    governance_mode: Literal["automatic", "paused"] = "automatic"
    active_hold_id: UUID | None = None
    hold_resource_version: int | None = None
    hold_reason: str | None = None
    hold_created_at: datetime | None = None
    release_state: Literal["unreleased", "proposed", "released"] = "unreleased"
    active_proposal: CollaborationReleaseProposalSummaryResponse | None = None
    available_actions: tuple[
        Literal["pause", "approve", "reject", "resume_automatic"], ...
    ] = ()
    governance_history: tuple[CollaborationGovernanceHistoryResponse, ...] = ()
    attention_state: Literal["none", "decision_required"] = "none"
    proposal_summary: str | None = None


class CollaborationDecisionApplicationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent_id: UUID
    status: Literal["pending", "applied", "rejected", "stale", "duplicate"]
    result_code: str
    result_summary: str
    release_id: UUID | None
    error_code: str | None
    proposal_id: UUID | None = None
    proposal_status: Literal["pending", "approved", "rejected", "stale"] | None = None
    resolution_action: Literal["approve", "reject"] | None = None
    resolution_result: Literal["applied", "stale"] | None = None
    resolution_actor: str | None = None
    resolution_transport: Literal["web", "main_agent"] | None = None
    proposal_created_at: datetime | None = None
    proposal_resolved_at: datetime | None = None
    resolution_created_at: datetime | None = None


class CollaborationDecisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_id: UUID
    sequence: int
    outcome: Literal["intents", "no_change"]
    reason_code: str
    summary: str
    created_at: datetime
    projection_id: UUID
    projection_hash: str
    target_paths: tuple[CollaborationLinkResponse, ...]
    applications: tuple[CollaborationDecisionApplicationResponse, ...]


class CollaborationDetailResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    status: Literal["running", "waiting", "attention", "completed"]
    strategy_label: str
    run_id: UUID
    plan_generation: int
    released_count: int
    running_count: int
    waiting_count: int
    completed_count: int
    blocked_subtask_count: int
    blocked_subtasks: tuple[BlockedSubtaskResponse, ...]
    latest_decision_summary: str | None
    latest_decision_at: datetime | None
    attention_required: bool
    detail_href: str
    provider: str
    interface_version: str
    config_version: int
    config_summary: str
    paused_worker_item_count: int = 0
    pending_release_proposal_count: int = 0
    attention_subtasks: tuple["CollaborationAttentionSubtaskResponse", ...] = ()


class CollaborationAttentionSubtaskResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subtask_key: str
    subtask_title: str
    pending_count: int
    first_worker_item_title: str
    deep_link: str
    subtask_id: UUID | None = None


class CollaborationGovernanceWorkerItemResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID
    root_task_id: UUID
    subtask_id: UUID | None
    subtask_key: str
    subtask_title: str
    worker_item_id: UUID
    worker_item_key: str
    worker_item_title: str
    worker_item_role: str
    worker_item_responsibility: str
    governance: WorkerItemCollaborationSummaryResponse


class CollaborationGovernanceProposalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID
    root_task_id: UUID
    subtask_id: UUID | None
    subtask_key: str
    subtask_title: str
    worker_item_id: UUID
    worker_item_key: str
    worker_item_title: str
    proposal_id: UUID
    status: Literal["pending", "approved", "rejected", "stale"]
    resource_version: int
    target_request_version: int
    reason_code: str
    summary: str
    created_at: datetime
    resolved_at: datetime | None
    hold_id: UUID
    hold_resource_version: int
    governance_mode: Literal["automatic", "paused"]
    available_actions: tuple[
        Literal["pause", "approve", "reject", "resume_automatic"], ...
    ]
    deep_link: str
    input_selection_id: UUID | None = None
    input_selection_hash: str | None = None


class ResultInputArtifactResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: UUID
    artifact_type: str
    content_hash: str
    size_bytes: int = Field(ge=0)
    media_type: str


class ResultInputEvidenceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: UUID
    acceptance_criterion_ref: str
    artifact_ids: tuple[UUID, ...]


class ResultInputCandidateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_item_key: str
    worker_item_id: UUID | None
    worker_item_title: str
    subtask_key: str | None
    subtask_path: tuple[str, ...]
    priority: int = Field(ge=1)
    ready: bool
    reason_code: str | None
    missing_artifact_refs: tuple[str, ...]
    missing_evidence_refs: tuple[str, ...]
    retry_possible: bool
    outcome_unknown: bool
    execution_request_id: UUID | None
    execution_id: UUID | None
    attempt: int | None
    execution_status: str | None
    artifacts: tuple[ResultInputArtifactResponse, ...]
    evidence: tuple[ResultInputEvidenceResponse, ...]


class ResultInputSelectedSourceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_item_key: str
    worker_item_id: UUID
    worker_item_title: str
    subtask_key: str | None
    subtask_path: tuple[str, ...]
    effective_request_id: UUID
    effective_lineage_id: UUID
    effective_execution_id: UUID
    attempt: int = Field(ge=1)
    priority: int = Field(ge=1)
    inheritance_refs: tuple[str, ...]
    source_basis_hash: str
    artifacts: tuple[ResultInputArtifactResponse, ...]
    evidence: tuple[ResultInputEvidenceResponse, ...]


class ResultInputGroupResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group_key: str
    title: str
    mode: str
    minimum_count: int | None
    required_count: int = Field(ge=0)
    ready_count: int = Field(ge=0)
    reason_code: str | None
    candidates: tuple[ResultInputCandidateResponse, ...]
    selected_sources: tuple[ResultInputSelectedSourceResponse, ...]
    selection_id: UUID | None
    selection_hash: str | None


class ResultInputHardDependencyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dependency_key: str
    upstream_request_id: UUID
    upstream_worker_item_id: UUID | None
    upstream_worker_item_key: str
    upstream_worker_item_title: str
    required_artifact_refs: tuple[str, ...]
    required_evidence_refs: tuple[str, ...]
    ready: bool
    reason_code: str | None
    selected_source: ResultInputSelectedSourceResponse | None


class ResultInputRequestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_request_id: UUID
    worker_item_id: UUID
    worker_item_key: str
    subtask_key: str
    version: int = Field(ge=1)
    status: str
    collaboration_gate_mode: str
    gate_reason: str


class ResultInputExecutionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_request_id: UUID
    request_status: str
    request_version: int = Field(ge=1)
    collaboration_gate_mode: str
    gate_reason: str
    execution_id: UUID | None
    lineage_id: UUID | None
    attempt: int | None
    status: str | None
    failure_code: str | None
    started_at: datetime | None
    finished_at: datetime | None
    release_status: str
    released_at: datetime | None


class ResultInputGovernanceHistoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    resource_id: UUID
    status: str
    summary: str
    occurred_at: datetime
    result_code: str | None
    proposal_id: UUID | None
    resolution_action: str | None
    resolution_result: str | None


class ResultInputGovernanceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    state: str
    waiting_summary: str | None
    governance_mode: str
    release_state: str
    active_hold_id: UUID | None
    hold_resource_version: int | None
    active_proposal_id: UUID | None
    active_proposal_version: int | None
    active_proposal_input_selection_id: UUID | None
    active_proposal_input_selection_hash: str | None
    attention_state: str
    available_action_names: tuple[str, ...]
    history: tuple[ResultInputGovernanceHistoryResponse, ...]


class ResultInputDeliveryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    consumer_type: str
    consumer_id: UUID | None
    package_id: UUID | None
    context_version: int | None
    package_hash: str | None
    package_status: str | None
    assembly_request_id: UUID | None
    assembly_status: str | None
    assembly_attempt: int | None
    assembly_version: int | None
    assembly_request_fingerprint: str | None
    materialization_attempt: int | None
    materialization_outcome: str | None
    materialization_failure_code: str | None
    materialization_occurred_at: datetime | None
    acknowledgement_status: str
    acknowledged_package_hash: str | None
    acknowledged_at: datetime | None
    reason_code: str | None


class ResultInputActionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str
    command_type: str
    target_type: str
    target_id: UUID
    expected_version: int | None
    expected_fingerprint: str | None
    expected_plan_generation: int | None
    expected_input_selection_id: UUID | None
    expected_input_selection_hash: str | None


class WorkerResultInputsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int
    workspace_id: UUID
    root_task_id: UUID
    worker_item_id: UUID
    worker_item_key: str
    worker_item_title: str
    request: ResultInputRequestResponse | None
    plan_snapshot_id: UUID | None
    plan_generation: int | None
    plan_content_hash: str | None
    is_historical: bool
    groups: tuple[ResultInputGroupResponse, ...]
    hard_dependencies: tuple[ResultInputHardDependencyResponse, ...]
    governance: ResultInputGovernanceResponse
    delivery: ResultInputDeliveryResponse
    execution: ResultInputExecutionResponse | None
    available_actions: tuple[ResultInputActionResponse, ...]


class InputSelectionConsumerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    consumer_type: str
    consumer_id: UUID
    execution_request_id: UUID | None
    worker_item_id: UUID | None
    package_id: UUID | None
    context_version: int | None
    package_hash: str | None
    package_status: str | None
    assembly_request_id: UUID | None
    assembly_status: str | None
    assembly_attempt: int | None
    assembly_version: int | None
    materialization_attempt: int | None
    materialization_outcome: str | None
    materialization_failure_code: str | None
    acknowledgement_status: str
    acknowledged_at: datetime | None


class InputSelectionGroupResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group_key: str
    rule_hash: str
    sources: tuple[ResultInputSelectedSourceResponse, ...]


class InputSelectionHardInputResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dependency_key: str
    rule_hash: str
    source: ResultInputSelectedSourceResponse


class InputSelectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int
    selection_id: UUID
    workspace_id: UUID
    root_task_id: UUID
    run_id: UUID
    plan_snapshot_id: UUID
    plan_generation: int
    plan_content_hash: str
    target_worker_item_id: UUID
    target_execution_request_id: UUID
    target_request_version: int
    projection_snapshot_id: UUID
    projection_hash: str
    decision_id: UUID
    intent_id: UUID
    groups: tuple[InputSelectionGroupResponse, ...]
    hard_inputs: tuple[InputSelectionHardInputResponse, ...]
    selection_hash: str
    created_at: datetime
    consumers: tuple[InputSelectionConsumerResponse, ...]
    consumers_truncated: bool


class CollaborationDecisionPageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: tuple[CollaborationDecisionResponse, ...]
    next_cursor: str | None


class TaskConsoleOverviewProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int
    workspace_id: UUID
    root_task_id: UUID
    plan_snapshot_id: UUID
    generation: int
    title: str
    goal_summary: str
    acceptance_refs: tuple[str, ...]
    current_milestone_key: str | None
    subtask_count: int
    planned_subtask_count: int
    running_subtask_count: int
    completed_subtask_count: int
    failed_subtask_count: int
    next_subtask_keys: tuple[str, ...]
    attention_items: tuple[TaskConsoleAttentionProjectionResponse, ...]
    last_change_cursor: int
    as_of: datetime
    plan_head: PlanHeadProjectionResponse | None = None
    active_revision: PlanRevisionProjectionResponse | None = None
    convergence: PlanConvergenceProjectionResponse | None = None
    revision_attention: PlanRevisionAttentionProjectionResponse | None = None
    handoff_running_count: int = 0
    handoff_blocked_count: int = 0
    collaboration: RootCollaborationSummaryResponse | None = None


class SubtaskNodeProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subtask_key: str
    task_id: UUID | None
    kind: str
    title: str
    goal_summary: str
    state: str
    milestone_key: str | None
    worker_item_count: int
    running_worker_item_count: int
    attention_worker_item_count: int
    evidence_required: int
    evidence_satisfied: int
    current_action: str | None
    waiting_reason: str | None = None
    waiting_details_summary: str | None = None
    requires_operator: bool = False
    blocked_since: datetime | None = None
    handoff_running_count: int = 0
    handoff_blocked_count: int = 0
    handoff_worker_item_ids: tuple[UUID, ...] = ()


class SubtaskEdgeProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    upstream_subtask_key: str
    downstream_subtask_key: str
    kind: str


class SubtaskMilestoneProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    title: str
    state: str
    subtask_count: int
    completed_count: int
    planned_count: int
    order_hint: int


class TaskConsoleGraphProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int
    overview: TaskConsoleOverviewProjectionResponse
    nodes: tuple[SubtaskNodeProjectionResponse, ...]
    edges: tuple[SubtaskEdgeProjectionResponse, ...]
    milestones: tuple[SubtaskMilestoneProjectionResponse, ...]


class SubtaskSummaryProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    task_id: UUID | None
    kind: str
    title: str
    goal_summary: str
    state: str
    milestone_key: str | None
    acceptance_refs: tuple[str, ...]
    evidence_required: int
    evidence_satisfied: int
    current_action: str | None
    convergence: SubtaskConvergenceProjectionResponse | None = None


class RelatedSubtaskProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subtask_key: str
    title: str
    state: str


class WorkerItemSummaryProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_item_id: UUID
    stable_key: str
    title: str
    responsibility: str
    role: str
    status: str
    context_version: int
    current_step: str | None
    blocker: str | None
    current_agent_summary: str | None
    recent_activity_at: datetime | None
    plan_sync: WorkerPlanSyncProjectionResponse | None = None
    collaboration_state: str | None = None
    collaboration_waiting_summary: str | None = None
    governance_mode: Literal["automatic", "paused"] | None = None
    attention_state: Literal["none", "decision_required"] | None = None
    proposal_summary: str | None = None
    worker_item_href: str | None = None


class WorkerItemGraphEdgeProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    upstream_worker_item_id: UUID
    downstream_worker_item_id: UUID
    kind: str


class WorkerItemGraphProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_item_ids: tuple[UUID, ...]
    edges: tuple[WorkerItemGraphEdgeProjectionResponse, ...]


class SubtaskArtifactSummaryProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: UUID
    artifact_type: str
    media_type: str
    size_bytes: int
    content_available: bool


class SubtaskEvidenceSummaryProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: UUID
    acceptance_ref: str
    conclusion: str
    artifact_ids: tuple[UUID, ...]


class SubtaskCheckpointSummaryProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_id: UUID
    sequence: int
    title: str
    artifact_ids: tuple[UUID, ...]
    occurred_at: datetime


class SubtaskAttentionProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    kind: str
    worker_item_id: UUID | None
    worker_item_title: str | None
    reason: str
    occurred_at: datetime
    waiting_reason: str | None = None
    waiting_details_summary: str | None = None
    requires_operator: bool = False
    blocked_since: datetime | None = None


class SubtaskActivityProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_type: str
    source_id: UUID
    occurred_at: datetime
    kind: str
    summary: str
    correlation_id: UUID
    worker_item_id: UUID | None


class SubtaskWorkspaceProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID
    root_task_id: UUID
    task_title: str
    plan_snapshot_id: UUID
    plan_generation: int
    subtask: SubtaskSummaryProjectionResponse
    upstream_subtasks: tuple[RelatedSubtaskProjectionResponse, ...]
    downstream_subtasks: tuple[RelatedSubtaskProjectionResponse, ...]
    worker_items: tuple[WorkerItemSummaryProjectionResponse, ...]
    worker_graph: WorkerItemGraphProjectionResponse
    artifacts: tuple[SubtaskArtifactSummaryProjectionResponse, ...]
    evidence: tuple[SubtaskEvidenceSummaryProjectionResponse, ...]
    checkpoints: tuple[SubtaskCheckpointSummaryProjectionResponse, ...]
    attention_items: tuple[SubtaskAttentionProjectionResponse, ...]
    recent_activity: tuple[SubtaskActivityProjectionResponse, ...]
    last_change_cursor: int
    as_of: datetime
    convergence: SubtaskConvergenceProjectionResponse | None = None
    collaboration: SubtaskCollaborationSummaryResponse | None = None


class WorkerItemIdentityProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    root_task_id: UUID
    stable_key: str
    title: str
    responsibility: str
    status: str
    context_version: int
    acknowledged_context_version: int
    version: int
    updated_at: datetime


class WorkerItemPlacementProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_snapshot_id: UUID
    plan_generation: int
    worker_item_key: str
    subtask_key: str
    subtask_task_id: UUID | None
    role: str
    capability_requirements: tuple[str, ...]


class WorkerRuntimeProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_id: UUID
    task_id: UUID
    execution_status: str
    attempt: int
    session_id: UUID | None
    session_status: str | None
    session_version: int | None
    input_mode: str | None
    worker_id: UUID | None
    worker_display_name: str | None
    adapter_type: str | None
    lease_id: UUID | None
    fencing_token: int | None


class WorkerExecutionSummaryProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_id: UUID
    task_id: UUID
    status: str
    attempt: int
    worker_id: UUID | None
    session_id: UUID | None
    failure_code: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime
    version: int
    capability_requirements: tuple[str, ...]
    adapter_preferences: tuple[str, ...]
    adapter_forbidden: tuple[str, ...]
    scheduling_status: str
    waiting_reason: str | None
    waiting_details_summary: str | None
    requires_operator: bool
    blocked_since: datetime | None
    pinned_worker_id: UUID | None
    assigned_worker_id: UUID | None
    pinned_worker_summary: "WorkerSummaryResponse | None"
    assigned_worker_summary: "WorkerSummaryResponse | None"
    binding_summary: "ExecutionBindingSummaryResponse | None"


class WorkerCheckpointSummaryProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_id: UUID
    sequence: int
    title: str
    artifact_ids: tuple[UUID, ...]
    occurred_at: datetime


class UsageSummaryProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int
    output_tokens: int
    cached_tokens: int
    cost_microunits: int


class WorkerInterventionContextProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_item_id: UUID
    plan_generation: int
    context_version: int
    execution_id: UUID
    session_id: UUID
    session_version: int
    worker_id: UUID


class TraceDescriptorProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root_task_id: UUID
    execution_id: UUID
    session_id: UUID
    route: str
    raw_ref: str | None


class HandoffCandidateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_id: UUID
    display_name: str
    adapter_type: str
    eligible: bool
    recommended: bool
    ineligibility_code: str | None
    runtime_snapshot: dict[str, Any] | None
    capability_snapshot_hash: str | None
    policy_reason: dict[str, Any]
    workspace_compatibility: dict[str, Any]
    runtime_profile_label: str | None
    capabilities_satisfied: tuple[str, ...]
    health_summary: str
    policy_reasons: tuple[dict[str, Any], ...]
    inspected_at: datetime


class HandoffCandidateListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID
    worker_item_id: UUID
    source_execution_id: UUID
    source_session_id: UUID
    source_worker_id: UUID
    source_worker_item_version: int
    candidates: tuple[HandoffCandidateResponse, ...]
    recommended_worker_id: UUID | None
    blocking_codes: tuple[str, ...]
    inspected_at: datetime


class ActiveHandoffProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transition_id: UUID
    proposal_id: UUID
    status: str
    phase: str
    writer_state: str
    source_execution_id: UUID
    source_session_id: UUID
    source_worker_id: UUID
    target_worker_id: UUID
    target_execution_id: UUID | None
    target_session_id: UUID | None
    attention_required: bool
    failure_code: str | None
    blocked_operation_id: UUID | None
    available_recovery_strategies: tuple[str, ...]
    source_trace_descriptor: TraceDescriptorProjectionResponse | None
    target_trace_descriptor: TraceDescriptorProjectionResponse | None
    started_at: datetime | None
    updated_at: datetime


class HandoffRuntimeSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adapter_module_instance_id: UUID | None
    adapter_module_config_version: int | None
    workspace_module_instance_id: UUID | None
    workspace_module_config_version: int | None
    capability_snapshot_hash: str | None


class HandoffWorkerSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_id: UUID
    display_name: str
    adapter_type: str
    version: int
    status: str
    desired_state: str
    current_load: int
    capacity: int
    capabilities: tuple[str, ...]
    runtime_profile_label: str | None
    capability_snapshot_hash: str | None


class HandoffExecutionSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_id: UUID
    task_id: UUID
    status: str
    attempt: int
    version: int
    session_id: UUID | None
    session_status: str | None
    session_version: int | None
    last_sequence: int | None
    trace_descriptor: TraceDescriptorProjectionResponse | None


class HandoffParticipantResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker: HandoffWorkerSummaryResponse
    runtime: HandoffRuntimeSummaryResponse | None
    execution: HandoffExecutionSummaryResponse | None
    worker_item_id: UUID
    context_version: int | None
    workspace_manifest_id: UUID | None
    workspace_manifest_version: int | None


class HandoffWorkerItemSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_item_id: UUID
    title: str
    responsibility: str
    status: str
    version: int
    context_version: int


class HandoffWorkspaceSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transfer_mode: str
    source_manifest_id: UUID
    source_manifest_version: int
    source_lifecycle: str | None
    target_manifest_id: UUID | None
    target_manifest_version: int | None
    target_lifecycle: str | None
    compatibility: dict[str, Any]


class HandoffContextSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_package_id: UUID
    source_package_hash: str
    source_context_version: int
    target_package_id: UUID | None
    target_package_hash: str | None
    target_context_version: int | None


class HandoffPlanBaseResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: UUID
    generation: int
    head_version: int


class HandoffProposalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    workspace_id: UUID
    root_task_id: UUID
    subtask_id: UUID
    task_title: str | None
    subtask_title: str | None
    worker_item: HandoffWorkerItemSummaryResponse
    status: str
    stale_reason: str | None
    version: int
    proposal_hash: str
    source_channel: str
    created_by_type: str
    created_by_id: str
    created_at: datetime
    expires_at: datetime
    confirmed_at: datetime | None
    cancelled_at: datetime | None
    handoff_reason: str
    handoff_instruction: str
    transfer_mode: str
    source: HandoffParticipantResponse
    target: HandoffParticipantResponse
    workspace: HandoffWorkspaceSummaryResponse
    context: HandoffContextSummaryResponse
    plan_base: HandoffPlanBaseResponse
    input_plan: dict[str, Any]
    impact_summary: dict[str, Any]


class HandoffTransitionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    workspace_id: UUID
    root_task_id: UUID
    subtask_id: UUID
    task_title: str | None
    subtask_title: str | None
    worker_item: HandoffWorkerItemSummaryResponse
    proposal_id: UUID
    proposal_status: str
    proposal_hash: str
    version: int
    status: str
    phase: str
    writer_state: str
    logical_attempt: int
    source_input_cutoff_sequence: int
    target_context_version: int
    failure_code: str | None
    blocked_operation_id: UUID | None
    next_attempt_at: datetime | None
    confirmed_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    attention_required: bool
    available_recovery_strategies: tuple[str, ...]
    source: HandoffParticipantResponse
    target: HandoffParticipantResponse
    workspace: HandoffWorkspaceSummaryResponse
    context: HandoffContextSummaryResponse
    source_trace_descriptor: TraceDescriptorProjectionResponse | None
    target_trace_descriptor: TraceDescriptorProjectionResponse | None
    recovery_evidence_refs: tuple[str, ...]
    recovery_inspected_at: datetime | None


class WorkerArtifactSummaryProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: UUID
    task_id: UUID
    execution_id: UUID
    artifact_type: str
    media_type: str
    size_bytes: int
    content_available: bool
    created_at: datetime


class WorkerEvidenceSummaryProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: UUID
    task_id: UUID
    execution_id: UUID
    acceptance_ref: str
    conclusion: str
    artifact_ids: tuple[UUID, ...]
    created_at: datetime


class WorkerActivityProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    activity_id: UUID
    source_type: str
    source_id: UUID
    execution_id: UUID | None
    session_id: UUID | None
    occurred_at: datetime
    kind: str
    title: str
    summary: str
    correlation_id: UUID
    body: str | None
    delivery_status: str | None
    artifact_ids: tuple[UUID, ...]
    trace_descriptor: TraceDescriptorProjectionResponse | None


class WorkerWorkspaceProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID
    worker_item: WorkerItemIdentityProjectionResponse
    current_placement: WorkerItemPlacementProjectionResponse
    placement_history: tuple[WorkerItemPlacementProjectionResponse, ...]
    current_runtime: WorkerRuntimeProjectionResponse | None
    execution_history: tuple[WorkerExecutionSummaryProjectionResponse, ...]
    current_step: str | None
    next_action: str | None
    latest_checkpoint: WorkerCheckpointSummaryProjectionResponse | None
    usage: UsageSummaryProjectionResponse
    artifacts: tuple[WorkerArtifactSummaryProjectionResponse, ...]
    evidence: tuple[WorkerEvidenceSummaryProjectionResponse, ...]
    can_intervene: bool
    read_only_reason: str | None
    intervention_context: WorkerInterventionContextProjectionResponse | None
    trace_descriptor: TraceDescriptorProjectionResponse | None
    last_change_cursor: int
    as_of: datetime
    plan_sync: WorkerPlanSyncProjectionResponse | None = None
    active_handoff: ActiveHandoffProjectionResponse | None = None
    collaboration: WorkerItemCollaborationSummaryResponse | None = None


class WorkerItemActivityPageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    last_change_cursor: int
    items: tuple[WorkerActivityProjectionResponse, ...]
    next_cursor: str | None


class ExecutionSummaryProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_id: UUID
    session_id: UUID | None
    worker_id: UUID | None
    status: str
    attempt: int
    started_at: datetime | None
    finished_at: datetime | None


class ArtifactSummaryProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: UUID
    artifact_type: str
    media_type: str
    size_bytes: int
    content_available: bool


class EvidenceSummaryProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: UUID
    acceptance_ref: str
    conclusion: str
    artifact_ids: tuple[UUID, ...]


class CheckpointSummaryProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence: int
    title: str
    artifact_ids: tuple[UUID, ...]
    occurred_at: datetime


class TaskDetailProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int
    last_change_cursor: int
    node: TaskNodeProjectionResponse
    acceptance_refs: tuple[str, ...]
    expected_outputs: tuple[str, ...]
    dependencies: tuple[TaskEdgeProjectionResponse, ...]
    executions: tuple[ExecutionSummaryProjectionResponse, ...]
    artifacts: tuple[ArtifactSummaryProjectionResponse, ...]
    evidence: tuple[EvidenceSummaryProjectionResponse, ...]
    checkpoints: tuple[CheckpointSummaryProjectionResponse, ...]
    plan_sync: WorkerPlanSyncProjectionResponse | None = None


class ActivityItemProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_type: str
    source_id: UUID
    occurred_at: datetime
    kind: str
    title: str
    summary: str
    correlation_id: UUID
    causal_parent_id: UUID | None
    scheduler_enforced: bool


class TaskActivityProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int
    last_change_cursor: int
    items: tuple[ActivityItemProjectionResponse, ...]
    next_cursor: str | None


class TaskConsoleActivityItemProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_type: str
    source_id: UUID
    occurred_at: datetime
    kind: str
    title: str
    summary: str
    correlation_id: UUID
    subtask_key: str | None
    subtask_title: str | None


class TaskConsoleActivityProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int
    last_change_cursor: int
    items: tuple[TaskConsoleActivityItemProjectionResponse, ...]
    next_cursor: str | None


class PlanDiffEntryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    work_item_key: str
    change: Literal["unchanged", "patched", "added", "removed"]
    changed_fields: tuple[str, ...]
    reuse_candidate: bool
    context_patch_candidate: bool
    portable_replay_candidate: bool
    runtime_execution_id: UUID | None


class PlanDiffResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int
    last_change_cursor: int
    root_task_id: UUID
    left_generation: int
    right_generation: int
    entries: tuple[PlanDiffEntryResponse, ...]


class TransitionNodeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    key: str | None
    title: str
    path: str


class TransitionOperationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: UUID
    operation_type: str
    status: str
    failure_code: str | None


class TransitionContextResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_context_version: int
    acknowledged_context_version: int
    target_context_version: int
    patch_id: UUID | None
    patch_hash: str | None
    patch_acknowledged: bool


class TransitionInputDispositionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy: str
    queued_input_ids: tuple[UUID, ...]
    superseded_input_ids: tuple[UUID, ...]
    queued_count: int
    superseded_count: int


class TransitionDependencyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    upstream_worker_item_id: UUID
    downstream_worker_item_id: UUID
    upstream_path: str
    downstream_path: str
    enforcement: str
    upstream_status: str | None
    blocked: bool
    required_artifact_refs: tuple[str, ...]
    required_evidence_refs: tuple[str, ...]


class WorkerTransitionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_transition_id: UUID
    plan_transition_id: UUID
    root_task: TransitionNodeResponse
    subtask: TransitionNodeResponse | None
    worker_item: TransitionNodeResponse
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
    context: TransitionContextResponse
    operations: tuple[TransitionOperationResponse, ...]
    queued_input_disposition: TransitionInputDispositionResponse
    blocked_dependencies: tuple[TransitionDependencyResponse, ...]
    started_at: datetime | None
    completed_at: datetime | None
    version: int


class PlanTransitionSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_transition_id: UUID
    workspace_id: UUID
    root_task: TransitionNodeResponse
    from_generation: int
    to_generation: int
    status: str
    started_at: datetime
    completed_at: datetime | None
    version: int
    worker_count: int
    blocked_worker_count: int
    unknown_worker_count: int


class PlanTransitionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_transition_id: UUID
    workspace_id: UUID
    root_task: TransitionNodeResponse
    from_generation: int
    to_generation: int
    status: str
    started_at: datetime
    completed_at: datetime | None
    version: int
    workers: tuple[WorkerTransitionResponse, ...]
    blocked_dependencies: tuple[TransitionDependencyResponse, ...]
    as_of: datetime


class TraceUsageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int | None
    output_tokens: int | None
    cached_tokens: int | None
    cost_microunits: int | None


class TraceTimingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_ms: int
    model_ms: int = Field(title="model_ms")
    tools_ms: int
    total_ms: int


class WorkerTraceEventResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int
    workspace_id: UUID
    root_task_id: UUID
    work_item_key: str | None
    plan_snapshot_id: UUID | None
    execution_id: UUID
    session_id: UUID
    sequence: int
    occurred_at: datetime
    category: str
    status: str
    title: str
    summary: str
    duration_ms: int | None
    usage: TraceUsageResponse | None
    artifact_refs: tuple[UUID, ...]
    provider_payload_ref: str | None
    provider_extensions: dict[str, Any]


class WorkerContextBranchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

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


class WorkerTraceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

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
    timing: TraceTimingResponse
    events: tuple[WorkerTraceEventResponse, ...]
    previous_before_sequence: int | None
    context_branch: WorkerContextBranchResponse | None


class PublishPlanSnapshotPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_current_generation: int = Field(ge=0)
    snapshot: dict[str, Any]


class RegisterWorkerPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adapter_type: NonEmptyString
    display_name: NonEmptyString
    capabilities: tuple[NonEmptyString, ...] = Field(min_length=1)
    capacity: int = Field(ge=1, le=1_000)
    cost_per_hour_microunits: int = Field(ge=0)
    startup_latency_ms: int = Field(ge=0)


class HeartbeatWorkerPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    healthy: bool


class ConfigureWorkerRuntimePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adapter_module_instance_id: UUID
    adapter_module_config_version: int = Field(ge=1)
    workspace_module_instance_id: UUID
    workspace_module_config_version: int = Field(ge=1)


class SetModuleLifecyclePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lifecycle: Literal["active", "draining", "disabled"]


class PinExecutionWorkerPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_id: UUID


class ClearExecutionWorkerPinPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateExecutionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    worker_item_id: UUID | None = None
    purpose: NonEmptyString
    capability_requirements: tuple[NonEmptyString, ...] = Field(min_length=1)
    adapter_preferences: tuple[NonEmptyString, ...] = Field(default_factory=tuple)
    adapter_forbidden: tuple[NonEmptyString, ...] = Field(default_factory=tuple)
    pinned_worker_id: UUID | None = None
    max_attempts: int = Field(ge=1, le=10)
    not_before: datetime


class RetryExecutionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    not_before: datetime


class LeaseOwnerPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_id: UUID
    fencing_token: int = Field(ge=1)


class ReportExecutionStartedPayload(LeaseOwnerPayload):
    lease_id: UUID
    adapter_session_id: NonEmptyString


class ReportExecutionFinishedPayload(LeaseOwnerPayload):
    lease_id: UUID


class ReportExecutionFailedPayload(ReportExecutionFinishedPayload):
    failure_code: NonEmptyString = Field(max_length=100)


class ExecutionInputTargetPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_id: UUID
    client_idempotency_key: NonEmptyString


class ExecutionContextAddressPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_item_id: UUID
    plan_generation: int = Field(ge=1)
    context_version: int = Field(ge=1)
    execution_id: UUID
    session_id: UUID
    session_version: int = Field(ge=1)
    worker_id: UUID


class SendExecutionInputPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: ExecutionContextAddressPayload
    client_idempotency_key: NonEmptyString
    text: InputText
    attachment_refs: tuple[NonEmptyString, ...] = Field(default_factory=tuple)


class UpdatePendingExecutionInputPayload(ExecutionInputTargetPayload):
    text: InputText


class CancelExecutionInputPayload(ExecutionInputTargetPayload):
    pass


class RetryBlockedExecutionInputPayload(ExecutionInputTargetPayload):
    pass


class ReconcileWorkerTransitionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: NonEmptyString
    evidence_refs: tuple[NonEmptyString, ...] = Field(min_length=1)
    operation_id: UUID | None = None
    patch_hash: NonEmptyString | None = None


class ConfirmWorkerTransitionStoppedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: NonEmptyString
    evidence_refs: tuple[NonEmptyString, ...] = Field(min_length=1)
    source_execution_id: UUID | None = None
    source_session_id: UUID | None = None


class RecoverWorkerTransitionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: Literal[
        "reconcile_adapter",
        "continue_after_confirmed_stop",
        "fail_closed_branch",
        "continue_after_confirmed_patch_absence",
    ]
    reason: NonEmptyString
    evidence_refs: tuple[NonEmptyString, ...] = Field(min_length=1)
    operation_id: UUID | None = None
    patch_hash: NonEmptyString | None = None


class PrepareWorkerHandoffPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_worker_id: UUID
    handoff_reason: NonEmptyString
    handoff_instruction: NonEmptyString
    input_plan: dict[str, Any] = Field(default_factory=dict)
    proposal_ttl_seconds: int = Field(gt=0, le=86_400)


class ConfirmWorkerHandoffPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: UUID
    expected_proposal_version: int = Field(ge=1)
    expected_proposal_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_worker_item_version: int = Field(ge=1)


class CancelWorkerHandoffPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: NonEmptyString


class RecoverWorkerHandoffPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transition_id: UUID
    expected_transition_version: int = Field(ge=1)
    strategy: Literal[
        "retry_current_phase",
        "retry_same_target",
        "select_new_target",
        "continue_with_source_agent",
        "confirm_source_stopped",
        "confirm_target_absent",
        "confirm_target_active",
    ]
    reason: NonEmptyString
    evidence_refs: tuple[NonEmptyString, ...] = Field(min_length=1)
    replacement_target_worker_id: UUID | None = None


class PauseCollaborationReleasePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transport: Literal["web", "main_agent"]
    reason: str | None = Field(default=None, max_length=2000)
    target_plan_generation: int = Field(ge=1)


class ApproveCollaborationReleaseProposalPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transport: Literal["web", "main_agent"]
    reason: str | None = Field(default=None, max_length=2000)
    expected_input_selection_id: UUID | None = None
    expected_input_selection_hash: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def validate_input_selection_expectation(
        self,
    ) -> "ApproveCollaborationReleaseProposalPayload":
        if (self.expected_input_selection_id is None) != (
            self.expected_input_selection_hash is None
        ):
            raise ValueError(
                "expected input selection id and hash must be provided together"
            )
        return self


class RejectCollaborationReleaseProposalPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transport: Literal["web", "main_agent"]
    reason: str | None = Field(default=None, max_length=2000)


class ResumeAutomaticCollaborationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transport: Literal["web", "main_agent"]
    reason: str | None = Field(default=None, max_length=2000)


class ContinueExecutionSessionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    not_before: datetime


class CommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: UUID
    workspace_id: UUID
    command_type: NonEmptyString
    target_type: AggregateType
    actor: ActorRequest
    target_id: UUID | None
    expected_version: int | None
    idempotency_key: NonEmptyString
    correlation_id: UUID
    payload: dict[str, Any]

    @model_validator(mode="after")
    def validate_command_shape(self) -> "CommandRequest":
        task_commands = {
            "CreateTask",
            "SetTaskPriority",
            "PauseTask",
            "ResumeTask",
            "CancelTask",
            "CompleteTask",
            "PublishPlanSnapshot",
        }
        if self.command_type in task_commands and self.target_type != "task":
            raise ValueError("Task commands require target_type=task")
        return self._validate_payload()

    def _validate_payload(self) -> "CommandRequest":
        if self.command_type == "CreateTask":
            self._require_create("task")
            parsed: BaseModel = CreateTaskPayload.model_validate(self.payload)
        elif self.command_type == "RegisterPlannerModule":
            self._require_create("workspace")
            parsed = RegisterPlannerModulePayload.model_validate(self.payload)
        elif self.command_type == "RegisterCollaborationRuntimeModule":
            self._require_create("workspace")
            parsed = RegisterCollaborationRuntimeModulePayload.model_validate(
                self.payload
            )
        elif self.command_type == "RegisterPlanningProfile":
            self._require_create("workspace")
            parsed = RegisterPlanningProfilePayload.model_validate(self.payload)
        elif self.command_type == "SubmitPlanningRequest":
            self._require_create("workspace")
            parsed = SubmitPlanningRequestPayload.model_validate(self.payload)
        elif self.command_type == "SubmitPlanRevision":
            self._require_mutation_target("task")
            parsed = SubmitPlanRevisionPayload.model_validate(self.payload)
            if parsed.root_task_id != self.target_id:
                raise ValueError("SubmitPlanRevision root_task_id must match target_id")
        elif self.command_type == "CancelPlanningRequest":
            self._require_mutation_target("planning_request")
            parsed = CancelPlanningRequestPayload.model_validate(self.payload)
        elif self.command_type in {
            "AnswerPlanningClarification",
            "RequestProposalRevision",
            "RejectPlanningProposal",
            "ConfirmPlanningProposal",
            "ConfirmPlanRevision",
        }:
            self._require_mutation_target("planning_request")
            payload_types = {
                "AnswerPlanningClarification": AnswerPlanningClarificationPayload,
                "RequestProposalRevision": RequestProposalRevisionPayload,
                "RejectPlanningProposal": RejectPlanningProposalPayload,
                "ConfirmPlanningProposal": ConfirmPlanningProposalPayload,
                "ConfirmPlanRevision": ConfirmPlanRevisionPayload,
            }
            parsed = payload_types[self.command_type].model_validate(self.payload)
        elif self.command_type == "RetryBlockedPlanningRun":
            self._require_mutation_target("planning_run")
            parsed = RetryBlockedPlanningRunPayload.model_validate(self.payload)
        elif self.command_type == "RetryContextAssembly":
            self._require_mutation_target("context_assembly")
            parsed = RetryContextAssemblyPayload.model_validate(self.payload)
        elif self.command_type == "RevokeContextPackage":
            self._require_mutation_target("context_package")
            parsed = RevokeContextPackagePayload.model_validate(self.payload)
        elif self.command_type in {
            "ReviseWorkContext",
            "AttachKnowledgeRevision",
            "ArchiveWorkContext",
        }:
            self._require_mutation_target("work_context")
            payload_types = {
                "ReviseWorkContext": ReviseWorkContextPayload,
                "AttachKnowledgeRevision": AttachKnowledgeRevisionPayload,
                "ArchiveWorkContext": ArchiveWorkContextPayload,
            }
            parsed = payload_types[self.command_type].model_validate(self.payload)
        elif self.command_type == "PublishPlanSnapshot":
            self._require_mutation_target("task")
            parsed = PublishPlanSnapshotPayload.model_validate(self.payload)
        elif self.command_type == "AcknowledgeAttentionItem":
            self._require_mutation_target("task")
            parsed = AcknowledgeAttentionItemPayload.model_validate(self.payload)
        elif self.command_type == "RequestTaskRework":
            self._require_mutation_target("task")
            parsed = RequestTaskReworkPayload.model_validate(self.payload)
        elif self.command_type == "AuthorizeAdditionalRework":
            self._require_mutation_target("task")
            parsed = AuthorizeAdditionalReworkPayload.model_validate(self.payload)
        elif self.command_type == "RequeueNotificationDelivery":
            self._require_requeue_target()
            parsed = RequeueNotificationDeliveryPayload.model_validate(self.payload)
        elif self.command_type == "SetTaskPriority":
            self._require_mutation_target()
            parsed = SetTaskPriorityPayload.model_validate(self.payload)
        elif self.command_type in {
            "PauseTask",
            "ResumeTask",
            "CancelTask",
            "CompleteTask",
        }:
            self._require_mutation_target()
            parsed = EmptyPayload.model_validate(self.payload)
        elif self.command_type == "RegisterWorker":
            self._require_create("worker")
            parsed = RegisterWorkerPayload.model_validate(self.payload)
        elif self.command_type == "HeartbeatWorker":
            self._require_mutation_target("worker")
            parsed = HeartbeatWorkerPayload.model_validate(self.payload)
        elif self.command_type == "DrainWorker":
            self._require_mutation_target("worker")
            parsed = EmptyPayload.model_validate(self.payload)
        elif self.command_type == "ConfigureWorkerRuntime":
            self._require_runtime_configuration_target()
            parsed = ConfigureWorkerRuntimePayload.model_validate(self.payload)
        elif self.command_type == "SetModuleLifecycle":
            if self.target_type not in {"module_definition", "module_instance"}:
                raise ValueError(
                    "SetModuleLifecycle target_type must be module_definition or module_instance"
                )
            self._require_mutation_target()
            parsed = SetModuleLifecyclePayload.model_validate(self.payload)
        elif self.command_type == "PinExecutionWorker":
            self._require_mutation_target("execution")
            parsed = PinExecutionWorkerPayload.model_validate(self.payload)
        elif self.command_type == "ClearExecutionWorkerPin":
            self._require_mutation_target("execution")
            parsed = ClearExecutionWorkerPinPayload.model_validate(self.payload)
        elif self.command_type == "CreateExecution":
            self._require_create("execution")
            parsed = CreateExecutionPayload.model_validate(self.payload)
        elif self.command_type == "RetryExecution":
            self._require_mutation_target("execution")
            parsed = RetryExecutionPayload.model_validate(self.payload)
        elif self.command_type in {"CancelExecution", "StopExecution"}:
            self._require_mutation_target("execution")
            parsed = EmptyPayload.model_validate(self.payload)
        elif self.command_type == "ContinueExecutionSession":
            self._require_mutation_target("execution_session")
            parsed = ContinueExecutionSessionPayload.model_validate(self.payload)
        elif self.command_type == "RenewLease":
            self._require_mutation_target("lease")
            parsed = LeaseOwnerPayload.model_validate(self.payload)
        elif self.command_type == "ReportExecutionStarted":
            self._require_mutation_target("execution")
            parsed = ReportExecutionStartedPayload.model_validate(self.payload)
        elif self.command_type == "ReportExecutionSucceeded":
            self._require_mutation_target("execution")
            parsed = ReportExecutionFinishedPayload.model_validate(self.payload)
        elif self.command_type == "ReportExecutionFailed":
            self._require_mutation_target("execution")
            parsed = ReportExecutionFailedPayload.model_validate(self.payload)
        elif self.command_type == "SendExecutionInput":
            self._require_mutation_target("execution_session")
            parsed = SendExecutionInputPayload.model_validate(self.payload)
        elif self.command_type == "UpdatePendingExecutionInput":
            self._require_mutation_target("execution_session")
            parsed = UpdatePendingExecutionInputPayload.model_validate(self.payload)
        elif self.command_type == "CancelExecutionInput":
            self._require_mutation_target("execution_session")
            parsed = CancelExecutionInputPayload.model_validate(self.payload)
        elif self.command_type == "RetryBlockedExecutionInput":
            self._require_mutation_target("execution_session")
            parsed = RetryBlockedExecutionInputPayload.model_validate(self.payload)
        elif self.command_type == "PrepareWorkerHandoff":
            self._require_mutation_target("worker_item")
            parsed = PrepareWorkerHandoffPayload.model_validate(self.payload)
        elif self.command_type == "ConfirmWorkerHandoff":
            self._require_mutation_target("handoff_proposal")
            parsed = ConfirmWorkerHandoffPayload.model_validate(self.payload)
            if parsed.proposal_id != self.target_id:
                raise ValueError(
                    "ConfirmWorkerHandoff proposal_id must match target_id"
                )
            if parsed.expected_proposal_version != self.expected_version:
                raise ValueError(
                    "ConfirmWorkerHandoff expected_proposal_version must match "
                    "expected_version"
                )
        elif self.command_type == "CancelWorkerHandoff":
            self._require_mutation_target("handoff_proposal")
            parsed = CancelWorkerHandoffPayload.model_validate(self.payload)
        elif self.command_type == "RecoverWorkerHandoff":
            self._require_mutation_target("handoff_transition")
            parsed = RecoverWorkerHandoffPayload.model_validate(self.payload)
            if parsed.transition_id != self.target_id:
                raise ValueError(
                    "RecoverWorkerHandoff transition_id must match target_id"
                )
            if parsed.expected_transition_version != self.expected_version:
                raise ValueError(
                    "RecoverWorkerHandoff expected_transition_version must match "
                    "expected_version"
                )
        elif self.command_type in {
            "PauseCollaborationRelease",
            "ApproveCollaborationReleaseProposal",
            "RejectCollaborationReleaseProposal",
            "ResumeAutomaticCollaboration",
        }:
            target_types: dict[str, AggregateType] = {
                "PauseCollaborationRelease": "worker_item",
                "ApproveCollaborationReleaseProposal": "collaboration_release_proposal",
                "RejectCollaborationReleaseProposal": "collaboration_release_proposal",
                "ResumeAutomaticCollaboration": "collaboration_governance_hold",
            }
            self._require_mutation_target(target_types[self.command_type])
            payload_types = {
                "PauseCollaborationRelease": PauseCollaborationReleasePayload,
                "ApproveCollaborationReleaseProposal": ApproveCollaborationReleaseProposalPayload,
                "RejectCollaborationReleaseProposal": RejectCollaborationReleaseProposalPayload,
                "ResumeAutomaticCollaboration": ResumeAutomaticCollaborationPayload,
            }
            parsed = payload_types[self.command_type].model_validate(self.payload)
        elif self.command_type in {
            "ReconcileWorkerTransition",
            "ConfirmWorkerTransitionStopped",
            "RecoverWorkerTransition",
        }:
            self._require_mutation_target("worker_transition")
            payload_types = {
                "ReconcileWorkerTransition": ReconcileWorkerTransitionPayload,
                "ConfirmWorkerTransitionStopped": ConfirmWorkerTransitionStoppedPayload,
                "RecoverWorkerTransition": RecoverWorkerTransitionPayload,
            }
            parsed = payload_types[self.command_type].model_validate(self.payload)
        else:
            return self
        self.payload = dict(
            parsed.model_dump(
                mode="json",
                exclude_none=self.command_type == "RecoverWorkerHandoff",
            )
        )
        return self

    def _require_create(self, target_type: AggregateType) -> None:
        if self.target_type != target_type:
            raise ValueError(f"create command requires target_type={target_type}")
        if self.target_id is not None or self.expected_version is not None:
            raise ValueError("create commands cannot target an existing aggregate")

    def _require_mutation_target(
        self, target_type: AggregateType | None = None
    ) -> None:
        if target_type is not None and self.target_type != target_type:
            raise ValueError(f"mutation command requires target_type={target_type}")
        if self.target_id is None or self.expected_version is None:
            raise ValueError("mutation commands require target_id and expected_version")
        if self.expected_version < 1:
            raise ValueError("expected_version must be positive")

    def _require_runtime_configuration_target(self) -> None:
        if self.target_type != "worker_runtime_configuration":
            raise ValueError(
                "ConfigureWorkerRuntime requires target_type=worker_runtime_configuration"
            )
        if self.target_id is None:
            raise ValueError("ConfigureWorkerRuntime requires target_id")
        if self.expected_version is not None and self.expected_version < 1:
            raise ValueError("expected_version must be positive")

    def _require_requeue_target(self) -> None:
        if self.target_type != "notification_delivery":
            raise ValueError(
                "RequeueNotificationDelivery requires "
                "target_type=notification_delivery"
            )
        if self.target_id is None:
            raise ValueError("RequeueNotificationDelivery requires target_id")
        if self.expected_version is not None:
            raise ValueError(
                "RequeueNotificationDelivery does not accept expected_version"
            )

    def to_envelope(self) -> CommandEnvelope:
        return CommandEnvelope(
            id=self.command_id,
            workspace_id=self.workspace_id,
            command_type=self.command_type,
            target_type=self.target_type,
            actor_type=self.actor.type,
            actor_id=self.actor.id,
            target_id=self.target_id,
            payload=self.payload,
            expected_version=self.expected_version,
            idempotency_key=self.idempotency_key,
            correlation_id=self.correlation_id,
        )


class CommandResponse(BaseModel):
    command_id: UUID
    aggregate_id: UUID
    aggregate_version: int
    event_ids: tuple[UUID, ...]
    data: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_result(cls, result: CommandResult) -> "CommandResponse":
        return cls(
            command_id=result.command_id,
            aggregate_id=result.aggregate_id,
            aggregate_version=result.aggregate_version,
            event_ids=result.event_ids,
            data=result.data,
        )


class CommandStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: UUID
    status: Literal["pending", "applied", "rejected"]
    result: CommandResponse | None = None
    error_code: str | None = None

    @classmethod
    def from_view(cls, view: CommandStatusView) -> "CommandStatusResponse":
        return cls(
            command_id=view.command_id,
            status=view.status,
            result=(
                CommandResponse.model_validate(view.result)
                if view.result is not None
                else None
            ),
            error_code=view.error_code,
        )


class WorkItemResponse(BaseModel):
    key: str
    task_id: UUID | None
    kind: str
    title: str
    goal_summary: str
    acceptance_refs: tuple[str, ...]
    milestone_key: str | None
    worker_slot_key: str | None
    expected_outputs: tuple[str, ...]
    source_refs: tuple[str, ...]


class WorkerSlotResponse(BaseModel):
    key: str
    role: str
    capability_requirements: tuple[str, ...]
    worker_policy_ref: str | None
    configuration_ref: str | None
    concurrency_limit: int | None


class MilestoneResponse(BaseModel):
    key: str
    title: str
    work_item_keys: tuple[str, ...]
    acceptance_refs: tuple[str, ...]
    order_hint: int


class DependencyResponse(BaseModel):
    upstream_work_item_key: str
    downstream_work_item_key: str
    kind: str
    required_evidence_refs: tuple[str, ...]
    enforcement: str


class TaskPlanSnapshotResponse(BaseModel):
    id: UUID
    schema_version: int
    workspace_id: UUID
    root_task_id: UUID
    generation: int
    parent_snapshot_id: UUID | None
    source_type: str
    source_ref: str
    title: str
    goal_summary: str
    acceptance_refs: tuple[str, ...]
    work_items: tuple[WorkItemResponse, ...]
    worker_slots: tuple[WorkerSlotResponse, ...]
    milestones: tuple[MilestoneResponse, ...]
    dependencies: tuple[DependencyResponse, ...]
    published_by: str
    published_at: datetime

    @field_serializer("published_at")
    def serialize_published_at(self, value: datetime) -> str:
        return value.isoformat()

    @classmethod
    def from_snapshot(cls, snapshot: TaskPlanSnapshot) -> "TaskPlanSnapshotResponse":
        return cls(
            id=snapshot.id,
            schema_version=snapshot.schema_version,
            workspace_id=snapshot.workspace_id,
            root_task_id=snapshot.root_task_id,
            generation=snapshot.generation,
            parent_snapshot_id=snapshot.parent_snapshot_id,
            source_type=snapshot.source_type,
            source_ref=snapshot.source_ref,
            title=snapshot.title,
            goal_summary=snapshot.goal_summary,
            acceptance_refs=snapshot.acceptance_refs,
            work_items=tuple(
                WorkItemResponse(
                    key=item.key,
                    task_id=item.task_id,
                    kind=item.kind.value,
                    title=item.title,
                    goal_summary=item.goal_summary,
                    acceptance_refs=item.acceptance_refs,
                    milestone_key=item.milestone_key,
                    worker_slot_key=item.worker_slot_key,
                    expected_outputs=item.expected_outputs,
                    source_refs=item.source_refs,
                )
                for item in snapshot.work_items
            ),
            worker_slots=tuple(
                WorkerSlotResponse(
                    key=slot.key,
                    role=slot.role,
                    capability_requirements=slot.capability_requirements,
                    worker_policy_ref=slot.worker_policy_ref,
                    configuration_ref=slot.configuration_ref,
                    concurrency_limit=slot.concurrency_limit,
                )
                for slot in snapshot.worker_slots
            ),
            milestones=tuple(
                MilestoneResponse(
                    key=milestone.key,
                    title=milestone.title,
                    work_item_keys=milestone.work_item_keys,
                    acceptance_refs=milestone.acceptance_refs,
                    order_hint=milestone.order_hint,
                )
                for milestone in snapshot.milestones
            ),
            dependencies=tuple(
                DependencyResponse(
                    upstream_work_item_key=dependency.upstream_work_item_key,
                    downstream_work_item_key=dependency.downstream_work_item_key,
                    kind=dependency.kind.value,
                    required_evidence_refs=dependency.required_evidence_refs,
                    enforcement=dependency.enforcement.value,
                )
                for dependency in snapshot.dependencies
            ),
            published_by=snapshot.published_by,
            published_at=snapshot.published_at,
        )


class RevisionBaseResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int
    workspace_id: UUID
    root_task_id: UUID
    plan_snapshot_id: UUID
    plan_generation: int
    plan_content_hash: str
    root_task_version: int
    captured_at: datetime

    @classmethod
    def from_view(cls, view: RevisionBaseView) -> "RevisionBaseResponse":
        return cls(
            schema_version=view.schema_version,
            workspace_id=view.workspace_id,
            root_task_id=view.root_task_id,
            plan_snapshot_id=view.plan_snapshot_id,
            plan_generation=view.plan_generation,
            plan_content_hash=view.plan_content_hash,
            root_task_version=view.root_task_version,
            captured_at=view.captured_at,
        )


class RevisionFreshnessResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base: RevisionBaseResponse
    current_plan_snapshot_id: UUID | None
    current_plan_generation: int | None
    current_plan_content_hash: str | None
    current_root_task_version: int | None
    base_fresh: bool
    stale_reasons: tuple[str, ...]
    checked_at: datetime

    @classmethod
    def from_view(cls, view: RevisionFreshnessView) -> "RevisionFreshnessResponse":
        return cls(
            base=RevisionBaseResponse.from_view(view.base),
            current_plan_snapshot_id=view.current_plan_snapshot_id,
            current_plan_generation=view.current_plan_generation,
            current_plan_content_hash=view.current_plan_content_hash,
            current_root_task_version=view.current_root_task_version,
            base_fresh=view.base_fresh,
            stale_reasons=view.stale_reasons,
            checked_at=view.checked_at,
        )


class RevisionTargetDescriptorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_generation: int
    parent_snapshot_id: UUID
    target_plan_content_hash: str

    @classmethod
    def from_view(
        cls, view: RevisionTargetDescriptorView
    ) -> "RevisionTargetDescriptorResponse":
        return cls(
            target_generation=view.target_generation,
            parent_snapshot_id=view.parent_snapshot_id,
            target_plan_content_hash=view.target_plan_content_hash,
        )


class RevisionFieldChangeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_path: str
    before: Any
    after: Any

    @classmethod
    def from_view(cls, view: RevisionFieldChangeView) -> "RevisionFieldChangeResponse":
        return cls(
            field_path=view.field_path,
            before=view.before,
            after=view.after,
        )


class RevisionInputGroupChangeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    consumer_worker_item_key: str
    group_key: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    reason: str

    @classmethod
    def from_view(
        cls, view: RevisionInputGroupChangeView
    ) -> "RevisionInputGroupChangeResponse":
        return cls(
            consumer_worker_item_key=view.consumer_worker_item_key,
            group_key=view.group_key,
            before=(
                _plain_revision_value(view.before) if view.before is not None else None
            ),
            after=(
                _plain_revision_value(view.after) if view.after is not None else None
            ),
            reason=view.reason,
        )


class PlanningCollaborationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str
    default_strategy: str | None
    subtask_overrides: tuple[dict[str, Any], ...]
    provider: str | None
    runtime_interface: str | None
    runtime_config_version: int | None
    input_groups: tuple["PlanningInputGroupResponse", ...]

    @classmethod
    def from_view(
        cls, view: PlanningCollaborationView
    ) -> "PlanningCollaborationResponse":
        return cls(
            mode=view.mode,
            default_strategy=view.default_strategy,
            subtask_overrides=tuple(dict(item) for item in view.subtask_overrides),
            provider=view.provider,
            runtime_interface=view.runtime_interface,
            runtime_config_version=view.runtime_config_version,
            input_groups=tuple(
                PlanningInputGroupResponse.from_view(item)
                for item in view.input_groups
            ),
        )


class PlanningInputGroupCandidateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_item_key: str
    priority: int

    @classmethod
    def from_view(cls, candidate: InputGroupCandidateV1) -> "PlanningInputGroupCandidateResponse":
        return cls(
            worker_item_key=candidate.worker_item_key,
            priority=candidate.priority,
        )


class PlanningInputGroupRequirementsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_status: str
    required_artifact_refs: tuple[str, ...]
    required_evidence_refs: tuple[str, ...]
    evidence_status: str

    @classmethod
    def from_view(
        cls, group: InputGroupV1
    ) -> "PlanningInputGroupRequirementsResponse":
        return cls(
            execution_status=group.requirements.execution_status.value,
            required_artifact_refs=group.requirements.required_artifact_refs,
            required_evidence_refs=group.requirements.required_evidence_refs,
            evidence_status=group.requirements.evidence_status.value,
        )


class PlanningInputGroupResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group_key: str
    title: str
    consumer_worker_item_key: str
    candidates: tuple[PlanningInputGroupCandidateResponse, ...]
    requirements: PlanningInputGroupRequirementsResponse
    mode: str
    minimum_count: int | None
    selection_strategy: str

    @classmethod
    def from_view(cls, group: InputGroupV1) -> "PlanningInputGroupResponse":
        return cls(
            group_key=group.group_key,
            title=group.title,
            consumer_worker_item_key=group.consumer_worker_item_key,
            candidates=tuple(
                PlanningInputGroupCandidateResponse.from_view(candidate)
                for candidate in group.candidates
            ),
            requirements=PlanningInputGroupRequirementsResponse.from_view(group),
            mode=group.mode.value,
            minimum_count=group.minimum_count,
            selection_strategy=group.selection_strategy.value,
        )


class RevisionSubtaskChangeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_subtask_key: str
    path: str
    existing_task_id: UUID | None
    strategy: str
    field_changes: tuple[RevisionFieldChangeResponse, ...]
    reason: str

    @classmethod
    def from_view(
        cls, view: RevisionSubtaskChangeView
    ) -> "RevisionSubtaskChangeResponse":
        return cls(
            target_subtask_key=view.target_subtask_key,
            path=view.path,
            existing_task_id=view.existing_task_id,
            strategy=view.strategy,
            field_changes=tuple(
                RevisionFieldChangeResponse.from_view(item)
                for item in view.field_changes
            ),
            reason=view.reason,
        )


class RevisionWorkerItemChangeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_worker_item_key: str
    path: str
    existing_worker_item_id: UUID | None
    strategy: str
    from_subtask_key: str | None
    to_subtask_key: str
    context_disposition: str
    field_changes: tuple[RevisionFieldChangeResponse, ...]
    reasons: tuple[str, ...]

    @classmethod
    def from_view(
        cls, view: RevisionWorkerItemChangeView
    ) -> "RevisionWorkerItemChangeResponse":
        return cls(
            target_worker_item_key=view.target_worker_item_key,
            path=view.path,
            existing_worker_item_id=view.existing_worker_item_id,
            strategy=view.strategy,
            from_subtask_key=view.from_subtask_key,
            to_subtask_key=view.to_subtask_key,
            context_disposition=view.context_disposition,
            field_changes=tuple(
                RevisionFieldChangeResponse.from_view(item)
                for item in view.field_changes
            ),
            reasons=view.reasons,
        )


class RevisionDependencyChangeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    graph_scope: str
    upstream_key: str
    downstream_key: str
    upstream_path: str
    downstream_path: str
    dependency_kind: str
    change_type: str

    @classmethod
    def from_view(
        cls, view: RevisionDependencyChangeView
    ) -> "RevisionDependencyChangeResponse":
        return cls(
            graph_scope=view.graph_scope,
            upstream_key=view.upstream_key,
            downstream_key=view.downstream_key,
            upstream_path=view.upstream_path,
            downstream_path=view.downstream_path,
            dependency_kind=view.dependency_kind,
            change_type=view.change_type,
        )


class RevisionTransitionPreviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_worker_item_key: str
    path: str
    worker_item_id: UUID | None
    allowed_actions: tuple[str, ...]
    fallback_action: str | None
    reason_codes: tuple[str, ...]

    @classmethod
    def from_view(
        cls, view: RevisionTransitionPreviewView
    ) -> "RevisionTransitionPreviewResponse":
        return cls(
            target_worker_item_key=view.target_worker_item_key,
            path=view.path,
            worker_item_id=view.worker_item_id,
            allowed_actions=view.allowed_actions,
            fallback_action=view.fallback_action,
            reason_codes=view.reason_codes,
        )


class RevisionImpactCountsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    preserve: int
    move: int
    patch: int
    continue_: int = Field(
        serialization_alias="continue",
        validation_alias="continue",
    )
    create: int
    derive: int
    retire: int
    affected_branches: int

    @classmethod
    def from_view(
        cls, view: RevisionImpactCountsView
    ) -> "RevisionImpactCountsResponse":
        return cls(
            preserve=view.preserve,
            move=view.move,
            patch=view.patch,
            continue_=view.continue_,
            create=view.create,
            derive=view.derive,
            retire=view.retire,
            affected_branches=view.affected_branches,
        )


class AcceptedTransitionPolicyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    activate_immediately: bool
    budget_level: str
    failure_policy: str
    manual_gate: bool
    revision_operation: str | None

    @classmethod
    def from_view(
        cls, view: AcceptedTransitionPolicyView
    ) -> "AcceptedTransitionPolicyResponse":
        return cls(
            activate_immediately=view.activate_immediately,
            budget_level=view.budget_level,
            failure_policy=view.failure_policy,
            manual_gate=view.manual_gate,
            revision_operation=view.revision_operation,
        )


class RevisionDiffResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID
    root_task_id: UUID
    planning_request_id: UUID
    proposal_revision_id: UUID
    proposal_revision: int
    proposal_content_hash: str
    base: RevisionBaseResponse
    target_descriptor_preview: RevisionTargetDescriptorResponse
    root_changes: tuple[RevisionFieldChangeResponse, ...]
    collaboration_policy_changes: tuple[RevisionFieldChangeResponse, ...]
    input_group_changes: tuple[RevisionInputGroupChangeResponse, ...]
    collaboration: PlanningCollaborationResponse
    subtask_changes: tuple[RevisionSubtaskChangeResponse, ...]
    worker_item_changes: tuple[RevisionWorkerItemChangeResponse, ...]
    subtask_dependency_changes: tuple[RevisionDependencyChangeResponse, ...]
    worker_dependency_changes: tuple[RevisionDependencyChangeResponse, ...]
    transition_preview: tuple[RevisionTransitionPreviewResponse, ...]
    impact_counts: RevisionImpactCountsResponse
    affected_branch_keys: tuple[str, ...]
    affected_branch_paths: tuple[str, ...]
    risk_flags: tuple[str, ...]
    diff_hash: str
    accepted_transition_policy: AcceptedTransitionPolicyResponse
    freshness: RevisionFreshnessResponse

    @classmethod
    def from_view(cls, view: RevisionDiffView) -> "RevisionDiffResponse":
        return cls(
            workspace_id=view.workspace_id,
            root_task_id=view.root_task_id,
            planning_request_id=view.planning_request_id,
            proposal_revision_id=view.proposal_revision_id,
            proposal_revision=view.proposal_revision,
            proposal_content_hash=view.proposal_content_hash,
            base=RevisionBaseResponse.from_view(view.base),
            target_descriptor_preview=RevisionTargetDescriptorResponse.from_view(
                view.target_descriptor_preview
            ),
            root_changes=tuple(
                RevisionFieldChangeResponse.from_view(item)
                for item in view.root_changes
            ),
            collaboration_policy_changes=tuple(
                RevisionFieldChangeResponse.from_view(item)
                for item in view.collaboration_policy_changes
            ),
            input_group_changes=tuple(
                RevisionInputGroupChangeResponse.from_view(item)
                for item in view.input_group_changes
            ),
            collaboration=PlanningCollaborationResponse.from_view(view.collaboration),
            subtask_changes=tuple(
                RevisionSubtaskChangeResponse.from_view(item)
                for item in view.subtask_changes
            ),
            worker_item_changes=tuple(
                RevisionWorkerItemChangeResponse.from_view(item)
                for item in view.worker_item_changes
            ),
            subtask_dependency_changes=tuple(
                RevisionDependencyChangeResponse.from_view(item)
                for item in view.subtask_dependency_changes
            ),
            worker_dependency_changes=tuple(
                RevisionDependencyChangeResponse.from_view(item)
                for item in view.worker_dependency_changes
            ),
            transition_preview=tuple(
                RevisionTransitionPreviewResponse.from_view(item)
                for item in view.transition_preview
            ),
            impact_counts=RevisionImpactCountsResponse.from_view(view.impact_counts),
            affected_branch_keys=view.affected_branch_keys,
            affected_branch_paths=view.affected_branch_paths,
            risk_flags=view.risk_flags,
            diff_hash=view.diff_hash,
            accepted_transition_policy=AcceptedTransitionPolicyResponse.from_view(
                view.accepted_transition_policy
            ),
            freshness=RevisionFreshnessResponse.from_view(view.freshness),
        )


class ActivePlanHeadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID
    root_task_id: UUID
    active_snapshot_id: UUID
    active_generation: int
    active_snapshot_hash: str
    version: int
    updated_at: datetime
    snapshot: TaskPlanSnapshotResponse

    @classmethod
    def from_view(cls, view: ActivePlanHeadView) -> "ActivePlanHeadResponse":
        return cls(
            workspace_id=view.workspace_id,
            root_task_id=view.root_task_id,
            active_snapshot_id=view.active_snapshot_id,
            active_generation=view.active_generation,
            active_snapshot_hash=view.active_snapshot_hash,
            version=view.version,
            updated_at=view.updated_at,
            snapshot=TaskPlanSnapshotResponse.from_snapshot(view.snapshot),
        )


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any]


class ErrorResponse(BaseModel):
    error: ErrorBody


class TaskResponse(BaseModel):
    id: UUID
    workspace_id: UUID
    title: str
    description: str
    acceptance_criteria: tuple[str, ...]
    status: str
    priority: int
    requested_by: str
    version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_view(cls, task: TaskView) -> "TaskResponse":
        return cls(
            id=task.id,
            workspace_id=task.workspace_id,
            title=task.title,
            description=task.description,
            acceptance_criteria=task.acceptance_criteria,
            status=task.status,
            priority=task.priority,
            requested_by=task.requested_by,
            version=task.version,
            created_at=task.created_at,
            updated_at=task.updated_at,
        )


class ModuleSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    instance_name: str
    provider: str
    module_type: str
    config_version: int
    definition_lifecycle: str
    instance_lifecycle: str

    @classmethod
    def from_view(cls, module: ModuleSummaryView) -> "ModuleSummaryResponse":
        return cls(
            id=module.id,
            instance_name=module.instance_name,
            provider=module.provider,
            module_type=module.module_type,
            config_version=module.config_version,
            definition_lifecycle=module.definition_lifecycle,
            instance_lifecycle=module.instance_lifecycle,
        )


class WorkerSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    display_name: str
    adapter_type: str
    status: str
    desired_state: str
    current_load: int
    capacity: int

    @classmethod
    def from_view(cls, worker: WorkerSummaryView) -> "WorkerSummaryResponse":
        return cls(
            id=worker.id,
            display_name=worker.display_name,
            adapter_type=worker.adapter_type,
            status=worker.status,
            desired_state=worker.desired_state,
            current_load=worker.current_load,
            capacity=worker.capacity,
        )


class ExecutionBindingSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    worker_id: UUID
    adapter_module_id: UUID
    workspace_module_id: UUID
    adapter_config_version: int
    workspace_config_version: int
    fencing_token: int
    version: int

    @classmethod
    def from_view(
        cls, binding: ExecutionBindingSummaryView
    ) -> "ExecutionBindingSummaryResponse":
        return cls(
            id=binding.id,
            worker_id=binding.worker_id,
            adapter_module_id=binding.adapter_module_id,
            workspace_module_id=binding.workspace_module_id,
            adapter_config_version=binding.adapter_config_version,
            workspace_config_version=binding.workspace_config_version,
            fencing_token=binding.fencing_token,
            version=binding.version,
        )


class WorkerResponse(BaseModel):
    id: UUID
    workspace_id: UUID
    adapter_type: str
    display_name: str
    status: str
    desired_state: str
    capabilities: tuple[str, ...]
    capacity: int
    current_load: int
    cost_per_hour_microunits: int
    startup_latency_ms: int
    last_heartbeat_at: datetime
    version: int
    created_at: datetime
    updated_at: datetime
    runtime_configuration_status: str
    runtime_configuration_reason: str | None
    runtime_configuration_version: int | None
    adapter_module_summary: ModuleSummaryResponse | None
    workspace_module_summary: ModuleSummaryResponse | None
    runtime_configured_at: datetime | None

    @classmethod
    def from_view(cls, worker: WorkerView) -> "WorkerResponse":
        return cls(
            id=worker.id,
            workspace_id=worker.workspace_id,
            adapter_type=worker.adapter_type,
            display_name=worker.display_name,
            status=worker.status,
            desired_state=worker.desired_state,
            capabilities=tuple(sorted(worker.capabilities)),
            capacity=worker.capacity,
            current_load=worker.current_load,
            cost_per_hour_microunits=worker.cost_per_hour_microunits,
            startup_latency_ms=worker.startup_latency_ms,
            last_heartbeat_at=worker.last_heartbeat_at,
            version=worker.version,
            created_at=worker.created_at,
            updated_at=worker.updated_at,
            runtime_configuration_status=worker.runtime_configuration_status,
            runtime_configuration_reason=worker.runtime_configuration_reason,
            runtime_configuration_version=worker.runtime_configuration_version,
            adapter_module_summary=(
                ModuleSummaryResponse.from_view(worker.adapter_module_summary)
                if worker.adapter_module_summary is not None
                else None
            ),
            workspace_module_summary=(
                ModuleSummaryResponse.from_view(worker.workspace_module_summary)
                if worker.workspace_module_summary is not None
                else None
            ),
            runtime_configured_at=worker.runtime_configured_at,
        )


class ExecutionResponse(BaseModel):
    id: UUID
    workspace_id: UUID
    task_id: UUID
    worker_item_id: UUID | None
    lineage_id: UUID
    attempt: int
    max_attempts: int
    purpose: str
    status: str
    capability_requirements: tuple[str, ...]
    pinned_worker_id: UUID | None
    assigned_worker_id: UUID | None
    active_lease_id: UUID | None
    last_fencing_token: int
    not_before: datetime
    failure_code: str | None
    started_at: datetime | None
    finished_at: datetime | None
    version: int
    stop_requested: bool
    source_execution_id: UUID | None
    source_session_id: UUID | None
    created_at: datetime
    updated_at: datetime
    adapter_preferences: tuple[str, ...]
    adapter_forbidden: tuple[str, ...]
    scheduling_status: str
    waiting_reason: str | None
    waiting_details_summary: str | None
    requires_operator: bool
    blocked_since: datetime | None
    pinned_worker_summary: WorkerSummaryResponse | None
    assigned_worker_summary: WorkerSummaryResponse | None
    binding_summary: ExecutionBindingSummaryResponse | None

    @classmethod
    def from_view(cls, execution: ExecutionView) -> "ExecutionResponse":
        return cls(
            id=execution.id,
            workspace_id=execution.workspace_id,
            task_id=execution.task_id,
            worker_item_id=execution.worker_item_id,
            lineage_id=execution.lineage_id,
            attempt=execution.attempt,
            max_attempts=execution.max_attempts,
            purpose=execution.purpose,
            status=execution.status,
            capability_requirements=tuple(sorted(execution.capability_requirements)),
            pinned_worker_id=execution.pinned_worker_id,
            assigned_worker_id=execution.assigned_worker_id,
            active_lease_id=execution.active_lease_id,
            last_fencing_token=execution.last_fencing_token,
            not_before=execution.not_before,
            failure_code=execution.failure_code,
            started_at=execution.started_at,
            finished_at=execution.finished_at,
            version=execution.version,
            stop_requested=execution.stop_requested,
            source_execution_id=execution.source_execution_id,
            source_session_id=execution.source_session_id,
            created_at=execution.created_at,
            updated_at=execution.updated_at,
            adapter_preferences=execution.adapter_preferences,
            adapter_forbidden=execution.adapter_forbidden,
            scheduling_status=execution.scheduling_status,
            waiting_reason=execution.waiting_reason,
            waiting_details_summary=execution.waiting_details_summary,
            requires_operator=execution.requires_operator,
            blocked_since=execution.blocked_since,
            pinned_worker_summary=(
                WorkerSummaryResponse.from_view(execution.pinned_worker_summary)
                if execution.pinned_worker_summary is not None
                else None
            ),
            assigned_worker_summary=(
                WorkerSummaryResponse.from_view(execution.assigned_worker_summary)
                if execution.assigned_worker_summary is not None
                else None
            ),
            binding_summary=(
                ExecutionBindingSummaryResponse.from_view(execution.binding_summary)
                if execution.binding_summary is not None
                else None
            ),
        )


class LeaseResponse(BaseModel):
    id: UUID
    execution_id: UUID
    worker_id: UUID
    status: str
    issued_at: datetime
    expires_at: datetime
    renewed_at: datetime | None
    ended_at: datetime | None
    fencing_token: int
    assignment_reason: dict[str, object]
    version: int

    @classmethod
    def from_view(cls, lease: LeaseView) -> "LeaseResponse":
        return cls(
            id=lease.id,
            execution_id=lease.execution_id,
            worker_id=lease.worker_id,
            status=lease.status,
            issued_at=lease.issued_at,
            expires_at=lease.expires_at,
            renewed_at=lease.renewed_at,
            ended_at=lease.ended_at,
            fencing_token=lease.fencing_token,
            assignment_reason=lease.assignment_reason,
            version=lease.version,
        )


class ExecutionBindingResponse(BaseModel):
    id: UUID
    workspace_id: UUID
    execution_id: UUID
    worker_id: UUID
    adapter_module_id: UUID
    workspace_module_id: UUID
    adapter_config_version: int
    workspace_config_version: int
    capability_snapshot: dict[str, object]
    lease_id: UUID
    fencing_token: int
    version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_view(cls, binding: ExecutionBindingView) -> "ExecutionBindingResponse":
        return cls(
            id=binding.id,
            workspace_id=binding.workspace_id,
            execution_id=binding.execution_id,
            worker_id=binding.worker_id,
            adapter_module_id=binding.adapter_module_id,
            workspace_module_id=binding.workspace_module_id,
            adapter_config_version=binding.adapter_config_version,
            workspace_config_version=binding.workspace_config_version,
            capability_snapshot=binding.capability_snapshot,
            lease_id=binding.lease_id,
            fencing_token=binding.fencing_token,
            version=binding.version,
            created_at=binding.created_at,
            updated_at=binding.updated_at,
        )


class ExecutionSessionResponse(BaseModel):
    id: UUID
    workspace_id: UUID
    execution_id: UUID
    worker_id: UUID
    adapter_session_id: str
    status: str
    last_sequence: int
    last_activity_at: datetime
    input_mode: str
    adapter_module_id: UUID | None
    adapter_config_version: int | None
    workspace_module_id: UUID | None
    workspace_config_version: int | None
    capability_snapshot: dict[str, object] | None
    workspace_ref: str | None
    version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_view(cls, session: ExecutionSessionView) -> "ExecutionSessionResponse":
        return cls(
            id=session.id,
            workspace_id=session.workspace_id,
            execution_id=session.execution_id,
            worker_id=session.worker_id,
            adapter_session_id=session.adapter_session_id,
            status=session.status,
            last_sequence=session.last_sequence,
            last_activity_at=session.last_activity_at,
            input_mode=session.input_mode,
            adapter_module_id=session.adapter_module_id,
            adapter_config_version=session.adapter_config_version,
            workspace_module_id=session.workspace_module_id,
            workspace_config_version=session.workspace_config_version,
            capability_snapshot=session.capability_snapshot,
            workspace_ref=session.workspace_ref,
            version=session.version,
            created_at=session.created_at,
            updated_at=session.updated_at,
        )


class ExecutionWorkspaceResponse(BaseModel):
    id: UUID
    workspace_id: UUID
    task_id: UUID
    execution_id: UUID
    checkouts: tuple[dict[str, object], ...]
    location_ref: str
    lifecycle: str
    retention_seconds: int
    retain_until: datetime | None
    fencing_token: int
    frozen_at: datetime | None
    released_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_view(
        cls, workspace: ExecutionWorkspaceView
    ) -> "ExecutionWorkspaceResponse":
        return cls(
            id=workspace.id,
            workspace_id=workspace.workspace_id,
            task_id=workspace.task_id,
            execution_id=workspace.execution_id,
            checkouts=workspace.checkouts,
            location_ref=workspace.location_ref,
            lifecycle=workspace.lifecycle,
            retention_seconds=workspace.retention_seconds,
            retain_until=workspace.retain_until,
            fencing_token=workspace.fencing_token,
            frozen_at=workspace.frozen_at,
            released_at=workspace.released_at,
            version=workspace.version,
            created_at=workspace.created_at,
            updated_at=workspace.updated_at,
        )


class ExecutionEventResponse(BaseModel):
    id: UUID
    workspace_id: UUID
    execution_id: UUID
    session_id: UUID
    sequence: int
    event_type: str
    payload: dict[str, object]
    occurred_at: datetime
    raw_ref: str | None

    @classmethod
    def from_view(cls, event: ExecutionEventView) -> "ExecutionEventResponse":
        return cls(
            id=event.id,
            workspace_id=event.workspace_id,
            execution_id=event.execution_id,
            session_id=event.session_id,
            sequence=event.sequence,
            event_type=event.event_type,
            payload=event.payload,
            occurred_at=event.occurred_at,
            raw_ref=event.raw_ref,
        )


class ArtifactMetadataResponse(BaseModel):
    id: UUID
    workspace_id: UUID
    task_id: UUID
    execution_id: UUID
    session_id: UUID | None
    artifact_type: str
    content_hash: str
    size_bytes: int
    media_type: str
    provenance: dict[str, object]
    created_at: datetime

    @classmethod
    def from_view(cls, artifact: ArtifactView) -> "ArtifactMetadataResponse":
        return cls(
            id=artifact.id,
            workspace_id=artifact.workspace_id,
            task_id=artifact.task_id,
            execution_id=artifact.execution_id,
            session_id=artifact.session_id,
            artifact_type=artifact.artifact_type,
            content_hash=artifact.content_hash,
            size_bytes=artifact.size_bytes,
            media_type=artifact.media_type,
            provenance=artifact.provenance,
            created_at=artifact.created_at,
        )


class EvidenceClaimResponse(BaseModel):
    id: UUID
    workspace_id: UUID
    task_id: UUID
    execution_id: UUID
    acceptance_criterion_ref: str
    conclusion: str
    environment_ref: str
    artifact_ids: tuple[UUID, ...]
    producer: str
    status: str
    created_at: datetime

    @classmethod
    def from_view(cls, claim: EvidenceClaimView) -> "EvidenceClaimResponse":
        return cls(
            id=claim.id,
            workspace_id=claim.workspace_id,
            task_id=claim.task_id,
            execution_id=claim.execution_id,
            acceptance_criterion_ref=claim.acceptance_criterion_ref,
            conclusion=claim.conclusion,
            environment_ref=claim.environment_ref,
            artifact_ids=claim.artifact_ids,
            producer=claim.producer,
            status=claim.status,
            created_at=claim.created_at,
        )


class ExecutionInputStatusResponse(BaseModel):
    id: UUID
    workspace_id: UUID
    execution_id: UUID
    session_id: UUID
    client_idempotency_key: str
    status: str
    operation_id: UUID | None
    failure_code: str | None
    version: int
    text_length: int
    attachment_count: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_view(cls, item: ExecutionInputView) -> "ExecutionInputStatusResponse":
        return cls(
            id=item.id,
            workspace_id=item.workspace_id,
            execution_id=item.execution_id,
            session_id=item.session_id,
            client_idempotency_key=item.client_idempotency_key,
            status=item.status,
            operation_id=item.operation_id,
            failure_code=item.failure_code,
            version=item.version,
            text_length=item.text_length,
            attachment_count=item.attachment_count,
            created_at=item.created_at,
            updated_at=item.updated_at,
        )


class CursorPageResponse(BaseModel):
    next_cursor: str | None


class PlanningCapabilityResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    structured_output: str
    clarification: bool
    idempotency: str
    cancellation: str
    usage_reporting: str
    max_context_bytes: int
    stability: str
    interface_version: str
    proposal_schema_versions: tuple[int, ...]
    context_package_versions: tuple[int, ...]

    @classmethod
    def from_view(cls, view: PlanningCapabilityView) -> "PlanningCapabilityResponse":
        return cls(
            structured_output=view.structured_output,
            clarification=view.clarification,
            idempotency=view.idempotency,
            cancellation=view.cancellation,
            usage_reporting=view.usage_reporting,
            max_context_bytes=view.max_context_bytes,
            stability=view.stability,
            interface_version=view.interface_version,
            proposal_schema_versions=view.proposal_schema_versions,
            context_package_versions=view.context_package_versions,
        )


class PlanningProfileResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

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
    capabilities: PlanningCapabilityResponse
    created_at: datetime

    @classmethod
    def from_view(cls, view: PlanningProfileView) -> "PlanningProfileResponse":
        return cls(
            id=view.id,
            workspace_id=view.workspace_id,
            profile_key=view.profile_key,
            version=view.version,
            display_name=view.display_name,
            planner_provider=view.planner_provider,
            planner_module_instance_id=view.planner_module_instance_id,
            planner_module_config_version=view.planner_module_config_version,
            planner_interface_version=view.planner_interface_version,
            status=view.status,
            user_selectable=view.user_selectable,
            capabilities=PlanningCapabilityResponse.from_view(view.capabilities),
            created_at=view.created_at,
        )


class PlanningBriefResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

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

    @classmethod
    def from_view(cls, view: PlanningBriefView) -> "PlanningBriefResponse":
        return cls(
            planning_request_id=view.planning_request_id,
            revision=view.revision,
            objective=view.objective,
            constraints=view.constraints,
            acceptance_criteria=view.acceptance_criteria,
            context_refs=view.context_refs,
            requested_outputs=view.requested_outputs,
            user_feedback=view.user_feedback,
            source_correlation=view.source_correlation,
            created_by=view.created_by,
            created_at=view.created_at,
        )


class PlanningUsageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int
    output_tokens: int
    cost_microunits: int
    runtime_seconds: int
    context_bytes: int

    @classmethod
    def from_view(cls, view: PlanningUsageView) -> "PlanningUsageResponse":
        return cls(
            input_tokens=view.input_tokens,
            output_tokens=view.output_tokens,
            cost_microunits=view.cost_microunits,
            runtime_seconds=view.runtime_seconds,
            context_bytes=view.context_bytes,
        )


class PlanningRunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    planning_request_id: UUID
    workspace_id: UUID
    brief_revision: int
    attempt: int
    operation_id: UUID
    status: str
    result_kind: str | None
    capability_snapshot: PlanningCapabilityResponse
    usage: PlanningUsageResponse
    provider_ref: str | None
    error_code: str | None
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    version: int

    @classmethod
    def from_view(cls, view: PlanningRunView) -> "PlanningRunResponse":
        return cls(
            id=view.id,
            planning_request_id=view.planning_request_id,
            workspace_id=view.workspace_id,
            brief_revision=view.brief_revision,
            attempt=view.attempt,
            operation_id=view.operation_id,
            status=view.status,
            result_kind=view.result_kind,
            capability_snapshot=PlanningCapabilityResponse.from_view(
                view.capability_snapshot
            ),
            usage=PlanningUsageResponse.from_view(view.usage),
            provider_ref=view.provider_ref,
            error_code=view.error_code,
            queued_at=view.queued_at,
            started_at=view.started_at,
            finished_at=view.finished_at,
            version=view.version,
        )


class PlanningClarificationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

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

    @classmethod
    def from_view(
        cls, view: PlanningClarificationView
    ) -> "PlanningClarificationResponse":
        return cls(
            id=view.id,
            planning_request_id=view.planning_request_id,
            planning_run_id=view.planning_run_id,
            question=view.question,
            rationale=view.rationale,
            answer_type=view.answer_type,
            options=view.options,
            blocking=view.blocking,
            answer=view.answer,
            answered_by=view.answered_by,
            answered_at=view.answered_at,
            answer_brief_revision=view.answer_brief_revision,
            created_at=view.created_at,
            version=view.version,
        )


class PlanningRequestSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

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

    @classmethod
    def from_view(
        cls, view: PlanningRequestSummaryView
    ) -> "PlanningRequestSummaryResponse":
        return cls(
            id=view.id,
            workspace_id=view.workspace_id,
            kind=view.kind,
            status=view.status,
            source_type=view.source_type,
            source_ref=view.source_ref,
            current_brief_revision=view.current_brief_revision,
            current_proposal_revision=view.current_proposal_revision,
            current_proposal_content_hash=view.current_proposal_content_hash,
            planning_profile_id=view.planning_profile_id,
            planning_profile_version=view.planning_profile_version,
            profile_key=view.profile_key,
            current_run_id=view.current_run_id,
            requires_attention=view.requires_attention,
            version=view.version,
            created_at=view.created_at,
            updated_at=view.updated_at,
        )


class PlanningRequestDetailResponse(PlanningRequestSummaryResponse):
    profile: PlanningProfileResponse | None
    active_run: PlanningRunResponse | None
    pending_clarifications: tuple[PlanningClarificationResponse, ...]
    usage: PlanningUsageResponse
    last_change_cursor: int
    revision_base: RevisionBaseResponse | None = None
    revision_freshness: RevisionFreshnessResponse | None = None

    @classmethod
    def from_detail_view(
        cls,
        view: PlanningRequestDetailView,
        *,
        revision_freshness: RevisionFreshnessView | None = None,
    ) -> "PlanningRequestDetailResponse":
        summary = PlanningRequestSummaryResponse.from_view(view.summary)
        return cls(
            **summary.model_dump(),
            profile=(
                PlanningProfileResponse.from_view(view.profile)
                if view.profile is not None
                else None
            ),
            active_run=(
                PlanningRunResponse.from_view(view.active_run)
                if view.active_run is not None
                else None
            ),
            pending_clarifications=tuple(
                PlanningClarificationResponse.from_view(item)
                for item in view.pending_clarifications
            ),
            usage=PlanningUsageResponse.from_view(view.usage),
            last_change_cursor=view.last_change_cursor,
            revision_base=(
                RevisionBaseResponse.from_view(revision_freshness.base)
                if revision_freshness is not None
                else None
            ),
            revision_freshness=(
                RevisionFreshnessResponse.from_view(revision_freshness)
                if revision_freshness is not None
                else None
            ),
        )


class PlanningProposalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

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
    derived_diff: dict[str, Any]
    display: dict[str, Any]
    collaboration: PlanningCollaborationResponse

    @classmethod
    def from_view(cls, view: PlanningProposalView) -> "PlanningProposalResponse":
        return cls(
            id=view.id,
            planning_request_id=view.planning_request_id,
            revision=view.revision,
            brief_revision=view.brief_revision,
            planning_run_id=view.planning_run_id,
            proposal_schema_version=view.proposal_schema_version,
            kind=view.kind,
            profile_version=view.profile_version,
            created_at=view.created_at,
            content_hash=view.content_hash,
            derived_diff=view.derived_diff,
            display=view.display,
            collaboration=PlanningCollaborationResponse.from_view(view.collaboration),
        )


class PlanningMaterializationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

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

    @classmethod
    def from_view(
        cls, view: PlanningMaterializationView
    ) -> "PlanningMaterializationResponse":
        return cls(
            id=view.id,
            workspace_id=view.workspace_id,
            planning_request_id=view.planning_request_id,
            proposal_revision_id=view.proposal_revision_id,
            root_task_id=view.root_task_id,
            plan_snapshot_id=view.plan_snapshot_id,
            plan_generation=view.plan_generation,
            subtask_task_mapping=view.subtask_task_mapping,
            worker_item_mapping=view.worker_item_mapping,
            execution_request_mapping=view.execution_request_mapping,
            deep_link=view.deep_link,
            confirmed_by=view.confirmed_by,
            created_at=view.created_at,
        )


class WorkContextTextItemResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    text: str
    ordinal: int


class WorkContextClarificationDecisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    question: str
    answer: str
    answered_by: str
    ordinal: int


class WorkContextAcceptedDecisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    statement: str
    source: str
    ordinal: int


class WorkContextUnresolvedQuestionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    question: str
    blocking: bool
    ordinal: int


class WorkContextKnowledgeRefResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_id: UUID
    revision_id: UUID
    revision_hash: str
    section_key: str
    section_hash: str
    source_ids: tuple[UUID, ...]
    ordinal: int


class WorkContextReferenceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref_id: UUID
    ref_type: str
    ordinal: int


class WorkContextSnapshotResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int
    objective: str
    constraints: tuple[WorkContextTextItemResponse, ...]
    acceptance_criteria: tuple[WorkContextTextItemResponse, ...]
    requested_outputs: tuple[WorkContextTextItemResponse, ...]
    clarification_decisions: tuple[WorkContextClarificationDecisionResponse, ...]
    accepted_decisions: tuple[WorkContextAcceptedDecisionResponse, ...]
    unresolved_questions: tuple[WorkContextUnresolvedQuestionResponse, ...]
    knowledge_refs: tuple[WorkContextKnowledgeRefResponse, ...]
    artifact_refs: tuple[WorkContextReferenceResponse, ...]
    evidence_refs: tuple[WorkContextReferenceResponse, ...]
    checkpoint_refs: tuple[WorkContextReferenceResponse, ...]
    missing_fact_codes: tuple[str, ...] = ()

    @classmethod
    def from_snapshot(
        cls, snapshot: WorkContextSnapshotV1
    ) -> "WorkContextSnapshotResponse":
        return cls.model_validate(snapshot.to_mapping())


class WorkContextRevisionSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

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

    @classmethod
    def from_view(
        cls, view: WorkContextRevisionSummaryView
    ) -> "WorkContextRevisionSummaryResponse":
        return cls(
            id=view.id,
            work_context_id=view.work_context_id,
            workspace_id=view.workspace_id,
            revision_number=view.revision_number,
            brief_planning_request_id=view.brief_planning_request_id,
            brief_revision=view.brief_revision,
            proposal_revision_id=view.proposal_revision_id,
            plan_snapshot_id=view.plan_snapshot_id,
            reason=view.reason,
            content_hash=view.content_hash,
            created_by=view.created_by,
            created_at=view.created_at,
        )


class WorkContextRevisionResponse(WorkContextRevisionSummaryResponse):
    snapshot: WorkContextSnapshotResponse

    @classmethod
    def from_revision_view(
        cls, view: WorkContextRevisionView
    ) -> "WorkContextRevisionResponse":
        summary = WorkContextRevisionSummaryResponse.from_view(view)
        return cls(
            **summary.model_dump(),
            snapshot=WorkContextSnapshotResponse.from_snapshot(view.snapshot),
        )


class WorkContextResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

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

    @classmethod
    def from_view(cls, view: WorkContextDetailView) -> "WorkContextResponse":
        return cls(
            id=view.id,
            workspace_id=view.workspace_id,
            planning_request_id=view.planning_request_id,
            root_task_id=view.root_task_id,
            status=view.status,
            current_revision_id=view.current_revision_id,
            current_revision_number=view.current_revision_number,
            version=view.version,
            created_at=view.created_at,
            updated_at=view.updated_at,
        )


class WorkContextDetailResponse(WorkContextResponse):
    current_revision: WorkContextRevisionResponse
    timeline: tuple[WorkContextRevisionSummaryResponse, ...]
    next_cursor: str | None

    @classmethod
    def from_view(cls, view: WorkContextDetailView) -> "WorkContextDetailResponse":
        summary = WorkContextResponse.from_view(view)
        return cls(
            **summary.model_dump(),
            current_revision=WorkContextRevisionResponse.from_revision_view(
                view.current_revision
            ),
            timeline=tuple(
                WorkContextRevisionSummaryResponse.from_view(item)
                for item in view.timeline
            ),
            next_cursor=view.next_cursor,
        )


class ContextPackageKnowledgeRefResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_id: UUID
    revision_id: UUID
    revision_hash: str
    section_key: str
    section_hash: str
    source_ids: tuple[UUID, ...]
    selection_reason: str
    trust_level: str
    ordinal: int


class ContextPackageReferenceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref_id: UUID
    ref_type: str
    ordinal: int


class ContextPackageBudgetResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: UUID
    profile_version: int
    max_sections: int
    max_chars: int
    max_input_tokens: int
    actual_sections: int
    actual_chars: int
    actual_input_tokens: int


class ContextPackageAssemblyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    status: str
    attempt: int
    next_attempt_at: datetime | None
    error_code: str | None
    request_fingerprint: str
    version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_view(
        cls, view: ContextPackageAssemblyView
    ) -> "ContextPackageAssemblyResponse":
        return cls(
            request_id=view.request_id,
            status=view.status,
            attempt=view.attempt,
            next_attempt_at=view.next_attempt_at,
            error_code=view.error_code,
            request_fingerprint=view.request_fingerprint,
            version=view.version,
            created_at=view.created_at,
            updated_at=view.updated_at,
        )


class ContextPackageAcknowledgementResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    package_id: UUID
    consumer_type: str
    consumer_id: UUID
    context_version: int | None
    package_hash: str
    adapter_module_id: UUID
    adapter_config_version: int
    acknowledged_at: datetime

    @classmethod
    def from_view(
        cls, view: ContextPackageAcknowledgementView
    ) -> "ContextPackageAcknowledgementResponse":
        return cls(
            package_id=view.package_id,
            consumer_type=view.consumer_type,
            consumer_id=view.consumer_id,
            context_version=view.context_version,
            package_hash=view.package_hash,
            adapter_module_id=view.adapter_module_id,
            adapter_config_version=view.adapter_config_version,
            acknowledged_at=view.acknowledged_at,
        )


class ContextPackageMaterializationAuditResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audit_id: UUID
    package_id: UUID
    consumer_type: str
    consumer_id: UUID
    context_version: int | None
    attempt: int
    outcome: str
    section_count: int
    byte_count: int
    input_token_count: int
    failure_code: str | None
    occurred_at: datetime

    @classmethod
    def from_view(
        cls, view: ContextPackageMaterializationAuditView
    ) -> "ContextPackageMaterializationAuditResponse":
        return cls(
            audit_id=view.audit_id,
            package_id=view.package_id,
            consumer_type=view.consumer_type,
            consumer_id=view.consumer_id,
            context_version=view.context_version,
            attempt=view.attempt,
            outcome=view.outcome,
            section_count=view.section_count,
            byte_count=view.byte_count,
            input_token_count=view.input_token_count,
            failure_code=view.failure_code,
            occurred_at=view.occurred_at,
        )


class ContextPackageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    package_id: UUID
    workspace_id: UUID
    purpose: str
    consumer_type: str
    consumer_id: UUID
    worker_item_id: UUID | None
    work_context_revision_id: UUID
    work_context_revision_number: int
    work_context_content_hash: str
    context_version: int | None
    manifest_schema_version: int
    result_input_manifest: dict[str, Any] | None
    code_input_manifest: dict[str, Any] | None
    verification_manifest: dict[str, Any] | None
    control_plane_fact_snapshot: dict[str, Any]
    knowledge_refs: tuple[ContextPackageKnowledgeRefResponse, ...]
    artifact_refs: tuple[ContextPackageReferenceResponse, ...]
    evidence_refs: tuple[ContextPackageReferenceResponse, ...]
    budget: ContextPackageBudgetResponse
    expires_at: datetime
    idempotency_key: str
    package_hash: str
    status: str
    created_at: datetime
    materialized_at: datetime | None
    revoked_at: datetime | None
    revocation_reason: str | None
    revocation_scope: str | None
    assembly: ContextPackageAssemblyResponse
    acknowledgements: tuple[ContextPackageAcknowledgementResponse, ...]
    materialization_audits: tuple[ContextPackageMaterializationAuditResponse, ...]

    @classmethod
    def from_view(cls, view: ContextPackageView) -> "ContextPackageResponse":
        manifest = view.manifest
        return cls(
            package_id=manifest.package_id,
            workspace_id=manifest.workspace_id,
            purpose=manifest.purpose.value,
            consumer_type=manifest.consumer_type.value,
            consumer_id=manifest.consumer_id,
            worker_item_id=manifest.worker_item_id,
            work_context_revision_id=manifest.work_context_revision_id,
            work_context_revision_number=view.work_context_revision_number,
            work_context_content_hash=view.work_context_content_hash,
            context_version=manifest.context_version,
            manifest_schema_version=manifest.manifest_schema_version,
            result_input_manifest=(
                jsonable_encoder(manifest.result_input_manifest.to_mapping())
                if isinstance(manifest, (ContextPackageManifestV2, ContextPackageManifestV3))
                and manifest.result_input_manifest is not None
                else None
            ),
            code_input_manifest=(
                jsonable_encoder(manifest.code_input_manifest.to_mapping())
                if isinstance(manifest, ContextPackageManifestV3)
                and manifest.code_input_manifest is not None
                else None
            ),
            verification_manifest=(
                jsonable_encoder(manifest.verification_manifest.to_mapping())
                if isinstance(manifest, ContextPackageManifestV3)
                and manifest.verification_manifest is not None
                else None
            ),
            control_plane_fact_snapshot=jsonable_encoder(
                manifest.control_plane_fact_snapshot
            ),
            knowledge_refs=tuple(
                ContextPackageKnowledgeRefResponse(
                    page_id=item.page_id,
                    revision_id=item.revision_id,
                    revision_hash=item.revision_hash,
                    section_key=item.section_key,
                    section_hash=item.section_hash,
                    source_ids=item.source_ids,
                    selection_reason=item.selection_reason.value,
                    trust_level=item.trust_level.value,
                    ordinal=item.ordinal,
                )
                for item in manifest.knowledge_refs
            ),
            artifact_refs=tuple(
                ContextPackageReferenceResponse(
                    ref_id=item.ref_id, ref_type=item.ref_type, ordinal=item.ordinal
                )
                for item in manifest.artifact_refs
            ),
            evidence_refs=tuple(
                ContextPackageReferenceResponse(
                    ref_id=item.ref_id, ref_type=item.ref_type, ordinal=item.ordinal
                )
                for item in manifest.evidence_refs
            ),
            budget=ContextPackageBudgetResponse(
                profile_id=manifest.budget.profile_id,
                profile_version=manifest.budget.profile_version,
                max_sections=manifest.budget.max_sections,
                max_chars=manifest.budget.max_chars,
                max_input_tokens=manifest.budget.max_input_tokens,
                actual_sections=manifest.budget.actual_sections,
                actual_chars=manifest.budget.actual_chars,
                actual_input_tokens=manifest.budget.actual_input_tokens,
            ),
            expires_at=manifest.expires_at,
            idempotency_key=manifest.idempotency_key,
            package_hash=manifest.package_hash,
            status=manifest.status.value,
            created_at=manifest.created_at,
            materialized_at=manifest.materialized_at,
            revoked_at=manifest.revoked_at,
            revocation_reason=manifest.revocation_reason,
            revocation_scope=(
                manifest.revocation_scope.value
                if manifest.revocation_scope is not None
                else None
            ),
            assembly=ContextPackageAssemblyResponse.from_view(view.assembly),
            acknowledgements=tuple(
                ContextPackageAcknowledgementResponse.from_view(item)
                for item in view.acknowledgements
            ),
            materialization_audits=tuple(
                ContextPackageMaterializationAuditResponse.from_view(item)
                for item in view.materialization_audits
            ),
        )


class KnowledgeSubmissionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    submission_id: UUID
    workspace_id: UUID
    producer_type: str
    producer_id: UUID
    planning_run_id: UUID | None
    execution_id: UUID | None
    worker_item_id: UUID | None
    task_id: UUID | None
    subtask_task_id: UUID | None
    proposal_hash: str
    status: str
    attempt: int
    knowledge_command_id: UUID | None
    terminal_candidate_id: UUID | None
    candidate_deep_link: str | None
    error_code: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_view(cls, view: KnowledgeSubmissionView) -> "KnowledgeSubmissionResponse":
        return cls(
            submission_id=view.submission_id,
            workspace_id=view.workspace_id,
            producer_type=view.producer_type,
            producer_id=view.producer_id,
            planning_run_id=view.planning_run_id,
            execution_id=view.execution_id,
            worker_item_id=view.worker_item_id,
            task_id=view.task_id,
            subtask_task_id=view.subtask_task_id,
            proposal_hash=view.proposal_hash,
            status=view.status,
            attempt=view.attempt,
            knowledge_command_id=view.knowledge_command_id,
            terminal_candidate_id=view.terminal_candidate_id,
            candidate_deep_link=view.candidate_deep_link,
            error_code=view.error_code,
            created_at=view.created_at,
            updated_at=view.updated_at,
        )


class KnowledgeSubmissionPageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: tuple[KnowledgeSubmissionResponse, ...]
    next_cursor: str | None

    @classmethod
    def from_view(
        cls, view: KnowledgeSubmissionPageView
    ) -> "KnowledgeSubmissionPageResponse":
        return cls(
            items=tuple(
                KnowledgeSubmissionResponse.from_view(item) for item in view.items
            ),
            next_cursor=view.next_cursor,
        )


class MaterializationMetadataResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    package_id: UUID
    package_hash: str
    purpose: str
    consumer_type: str
    consumer_id: UUID
    context_version: int | None
    expires_at: datetime


class MaterializationTextItemResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    text: str
    ordinal: int


class MaterializationSystemConstraintsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objective: str
    constraints: tuple[MaterializationTextItemResponse, ...]
    acceptance_criteria: tuple[MaterializationTextItemResponse, ...]


class MaterializationClarificationDecisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    question: str
    answer: str
    answered_by: str
    ordinal: int


class MaterializationAcceptedDecisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    statement: str
    source: str
    ordinal: int


class MaterializationUnresolvedQuestionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    question: str
    blocking: bool
    ordinal: int


class MaterializationWorkContextResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision_id: UUID
    revision_number: int
    content_hash: str
    snapshot_schema_version: int
    requested_outputs: tuple[MaterializationTextItemResponse, ...]
    clarification_decisions: tuple[MaterializationClarificationDecisionResponse, ...]
    accepted_decisions: tuple[MaterializationAcceptedDecisionResponse, ...]
    unresolved_questions: tuple[MaterializationUnresolvedQuestionResponse, ...]
    checkpoint_refs: tuple[ContextPackageReferenceResponse, ...]
    missing_fact_codes: tuple[str, ...] = ()


class MaterializedContextSourceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: UUID
    workspace_id: UUID
    source_type: str
    label: str
    uri: str | None
    workspace_relative_path: str | None
    git_revision: str | None
    digest: str | None
    metadata: dict[str, Any]
    availability: str
    safe_reason_code: str | None
    last_checked_at: datetime | None
    version: int


class MaterializationKnowledgeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ordinal: int
    page_id: UUID
    revision_id: UUID
    revision_hash: str
    section_key: str
    section_hash: str
    body_markdown: str
    sources: tuple[MaterializedContextSourceResponse, ...]
    trust: str
    section_byte_count: int


class MaterializedContextRefResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str
    ref_id: UUID
    ref_type: str
    ordinal: int


class ContextMaterializationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int
    metadata: MaterializationMetadataResponse
    system_constraints: MaterializationSystemConstraintsResponse
    work_context: MaterializationWorkContextResponse
    control_plane_facts: dict[str, Any]
    project_knowledge: tuple[MaterializationKnowledgeResponse, ...]
    artifact_and_evidence_refs: tuple[MaterializedContextRefResponse, ...]
    result_inputs: tuple[dict[str, Any], ...] = ()
    code_input_manifest: dict[str, Any] | None = None
    verification_manifest: dict[str, Any] | None = None
    total_byte_count: int
    input_token_count: int

    @classmethod
    def from_materialization(
        cls, materialization: Any
    ) -> "ContextMaterializationResponse":
        payload = materialization.to_mapping()
        payload["total_byte_count"] = materialization.total_byte_count
        payload["input_token_count"] = materialization.input_token_count
        return cls.model_validate(payload)


class PlanningProfilePageResponse(CursorPageResponse):
    items: tuple[PlanningProfileResponse, ...]


class PlanningRequestPageResponse(CursorPageResponse):
    items: tuple[PlanningRequestSummaryResponse, ...]
    last_change_cursor: int


class PlanningBriefPageResponse(BaseModel):
    items: tuple[PlanningBriefResponse, ...]


class PlanningRunPageResponse(BaseModel):
    items: tuple[PlanningRunResponse, ...]


class PlanningClarificationPageResponse(BaseModel):
    items: tuple[PlanningClarificationResponse, ...]


class PlanningRunMetricsResponse(BaseModel):
    queued: int
    running: int
    needs_clarification: int
    blocked: int
    failed: int
    succeeded: int
    cancelled: int


class PlanningLatencyMetricsResponse(BaseModel):
    queue_seconds: float
    planning_seconds: float
    materialization_seconds: float


class PlanningOutcomeMetricsResponse(BaseModel):
    retry_rate: float
    timeout_rate: float
    unknown_outcome_rate: float
    invalid_output_rate: float


class PlanningUsageMetricsResponse(BaseModel):
    input_tokens: int
    output_tokens: int
    cost_microunits: int
    context_bytes: int


class PlanningLifecycleMetricsResponse(BaseModel):
    clarifications: int
    revisions: int
    confirmed: int
    rejected: int


class PlanningExecutionRequestMetricsResponse(BaseModel):
    blocked: int
    ready: int
    gate_wait_seconds: float


class PlanningMetricReasonResponse(BaseModel):
    reason: str
    count: int


class PlanningWorkerTransitionMetricResponse(BaseModel):
    action: str
    outcome: str
    count: int


class PlanningContextPatchDeliveryMetricResponse(BaseModel):
    adapter: str
    outcome: str
    count: int


class PlanningRevisionMetricsResponse(BaseModel):
    plan_revisions_active: int
    plan_revision_conflicts_total: tuple[PlanningMetricReasonResponse, ...]
    plan_transitions_active: int
    worker_transitions_total: tuple[PlanningWorkerTransitionMetricResponse, ...]
    context_patch_delivery_total: tuple[PlanningContextPatchDeliveryMetricResponse, ...]
    revision_confirmation_blocked_total: tuple[PlanningMetricReasonResponse, ...]


class PlanningMetricsResponse(BaseModel):
    workspace_id: UUID
    as_of: datetime
    runs: PlanningRunMetricsResponse
    latency: PlanningLatencyMetricsResponse
    outcomes: PlanningOutcomeMetricsResponse
    usage: PlanningUsageMetricsResponse
    planning: PlanningLifecycleMetricsResponse
    execution_requests: PlanningExecutionRequestMetricsResponse
    revision: PlanningRevisionMetricsResponse

    @classmethod
    def from_view(cls, view: PlanningMetricsView) -> "PlanningMetricsResponse":
        return cls(
            workspace_id=view.workspace_id,
            as_of=view.as_of,
            runs=PlanningRunMetricsResponse(
                queued=view.runs.queued,
                running=view.runs.running,
                needs_clarification=view.runs.needs_clarification,
                blocked=view.runs.blocked,
                failed=view.runs.failed,
                succeeded=view.runs.succeeded,
                cancelled=view.runs.cancelled,
            ),
            latency=PlanningLatencyMetricsResponse(
                queue_seconds=view.latency.queue_seconds,
                planning_seconds=view.latency.planning_seconds,
                materialization_seconds=view.latency.materialization_seconds,
            ),
            outcomes=PlanningOutcomeMetricsResponse(
                retry_rate=view.outcomes.retry_rate,
                timeout_rate=view.outcomes.timeout_rate,
                unknown_outcome_rate=view.outcomes.unknown_outcome_rate,
                invalid_output_rate=view.outcomes.invalid_output_rate,
            ),
            usage=PlanningUsageMetricsResponse(
                input_tokens=view.usage.input_tokens,
                output_tokens=view.usage.output_tokens,
                cost_microunits=view.usage.cost_microunits,
                context_bytes=view.usage.context_bytes,
            ),
            planning=PlanningLifecycleMetricsResponse(
                clarifications=view.planning.clarifications,
                revisions=view.planning.revisions,
                confirmed=view.planning.confirmed,
                rejected=view.planning.rejected,
            ),
            execution_requests=PlanningExecutionRequestMetricsResponse(
                blocked=view.execution_requests.blocked,
                ready=view.execution_requests.ready,
                gate_wait_seconds=view.execution_requests.gate_wait_seconds,
            ),
            revision=PlanningRevisionMetricsResponse(
                plan_revisions_active=view.revision.plan_revisions_active,
                plan_revision_conflicts_total=tuple(
                    PlanningMetricReasonResponse(
                        reason=item.reason,
                        count=item.count,
                    )
                    for item in view.revision.plan_revision_conflicts_total
                ),
                plan_transitions_active=view.revision.plan_transitions_active,
                worker_transitions_total=tuple(
                    PlanningWorkerTransitionMetricResponse(
                        action=item.action,
                        outcome=item.outcome,
                        count=item.count,
                    )
                    for item in view.revision.worker_transitions_total
                ),
                context_patch_delivery_total=tuple(
                    PlanningContextPatchDeliveryMetricResponse(
                        adapter=item.adapter,
                        outcome=item.outcome,
                        count=item.count,
                    )
                    for item in view.revision.context_patch_delivery_total
                ),
                revision_confirmation_blocked_total=tuple(
                    PlanningMetricReasonResponse(
                        reason=item.reason,
                        count=item.count,
                    )
                    for item in view.revision.revision_confirmation_blocked_total
                ),
            ),
        )


class RuntimeDurationMetricsResponse(BaseModel):
    count: int
    total_seconds: float
    average_seconds: float
    max_seconds: float


class RuntimeMetricsResponse(BaseModel):
    workspace_id: UUID
    as_of: datetime
    scheduler_assignments_total: int
    scheduler_blocks_active: dict[str, int]
    worker_runtime_configuration_status: dict[str, int]
    runtime_binding_failures_total: dict[str, int]
    scheduler_assignment_duration_seconds: RuntimeDurationMetricsResponse

    @classmethod
    def from_view(cls, view: RuntimeMetricsView) -> "RuntimeMetricsResponse":
        return cls(
            workspace_id=view.workspace_id,
            as_of=view.as_of,
            scheduler_assignments_total=view.scheduler_assignments_total,
            scheduler_blocks_active=view.scheduler_blocks_active,
            worker_runtime_configuration_status=view.worker_runtime_configuration_status,
            runtime_binding_failures_total=view.runtime_binding_failures_total,
            scheduler_assignment_duration_seconds=RuntimeDurationMetricsResponse(
                count=view.scheduler_assignment_duration_seconds.count,
                total_seconds=view.scheduler_assignment_duration_seconds.total_seconds,
                average_seconds=view.scheduler_assignment_duration_seconds.average_seconds,
                max_seconds=view.scheduler_assignment_duration_seconds.max_seconds,
            ),
        )


class CollaborationMetricSampleResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    labels: dict[str, str]
    value: float


class CollaborationMetricsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID
    as_of: datetime
    samples: tuple[CollaborationMetricSampleResponse, ...]

    @classmethod
    def from_view(
        cls, view: CollaborationMetricsView
    ) -> "CollaborationMetricsResponse":
        return cls(
            workspace_id=view.workspace_id,
            as_of=view.as_of,
            samples=tuple(
                CollaborationMetricSampleResponse(
                    name=sample.name,
                    labels=dict(sample.labels),
                    value=sample.value,
                )
                for sample in view.samples
            ),
        )


class CollaborationReadinessCheckResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    ready: bool
    code: str
    summary: str


class CollaborationReadinessResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    component: str
    as_of: datetime
    status: Literal["ready", "not_ready"]
    result_input_writes_enabled: bool
    result_input_capabilities_ready: bool
    checks: tuple[CollaborationReadinessCheckResponse, ...]

    @classmethod
    def from_report(cls, report: ReadinessReport) -> "CollaborationReadinessResponse":
        return cls(
            component=report.component,
            as_of=report.as_of,
            status="ready" if report.ready else "not_ready",
            result_input_writes_enabled=report.result_input_writes_enabled,
            result_input_capabilities_ready=report.result_input_capabilities_ready,
            checks=tuple(
                CollaborationReadinessCheckResponse(
                    name=check.name,
                    ready=check.ready,
                    code=check.code,
                    summary=check.summary,
                )
                for check in report.checks
            ),
        )


class NotificationMetricSampleResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    labels: dict[str, str]
    value: float


class NotificationMetricsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID
    as_of: datetime
    samples: tuple[NotificationMetricSampleResponse, ...]

    @classmethod
    def from_view(cls, view: NotificationMetricsView) -> "NotificationMetricsResponse":
        return cls(
            workspace_id=view.workspace_id,
            as_of=view.as_of,
            samples=tuple(
                NotificationMetricSampleResponse(
                    name=sample.name,
                    labels=dict(sample.labels),
                    value=sample.value,
                )
                for sample in view.samples
            ),
        )


class NotificationReadinessCheckResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    ready: bool
    code: str
    summary: str


class NotificationReadinessResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    component: str
    as_of: datetime
    status: Literal["ready", "not_ready"]
    checks: tuple[NotificationReadinessCheckResponse, ...]

    @classmethod
    def from_report(
        cls, report: NotificationReadinessReport
    ) -> "NotificationReadinessResponse":
        return cls(
            component="sidera-notification-worker",
            as_of=report.as_of,
            status="ready" if report.ready else "not_ready",
            checks=tuple(
                NotificationReadinessCheckResponse(
                    name=check.name,
                    ready=check.ready,
                    code=check.code,
                    summary=check.summary,
                )
                for check in report.checks
            ),
        )


class ExecutionSessionPageResponse(CursorPageResponse):
    items: tuple[ExecutionSessionResponse, ...]


class ExecutionEventPageResponse(CursorPageResponse):
    items: tuple[ExecutionEventResponse, ...]


class ArtifactPageResponse(CursorPageResponse):
    items: tuple[ArtifactMetadataResponse, ...]


class EvidenceClaimPageResponse(CursorPageResponse):
    items: tuple[EvidenceClaimResponse, ...]


class ExecutionInputPageResponse(CursorPageResponse):
    items: tuple[ExecutionInputStatusResponse, ...]


class TaskPageResponse(BaseModel):
    items: tuple[TaskResponse, ...]
    total: int
    limit: int
    offset: int

    @classmethod
    def from_page(cls, page: TaskPage) -> "TaskPageResponse":
        return cls(
            items=tuple(TaskResponse.from_view(item) for item in page.items),
            total=page.total,
            limit=page.limit,
            offset=page.offset,
        )


class ActorResponse(BaseModel):
    type: str
    id: str


class TimelineItemResponse(BaseModel):
    id: UUID
    event_type: str
    aggregate_version: int
    actor: ActorResponse
    payload: dict[str, Any]
    causation_id: UUID
    correlation_id: UUID
    created_at: datetime

    @classmethod
    def from_item(cls, item: TimelineItem) -> "TimelineItemResponse":
        return cls(
            id=item.id,
            event_type=item.event_type,
            aggregate_version=item.aggregate_version,
            actor=ActorResponse(type=item.actor_type, id=item.actor_id),
            payload=item.payload,
            causation_id=item.causation_id,
            correlation_id=item.correlation_id,
            created_at=item.created_at,
        )


class NotificationSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipient_type: str
    recipient_id: str
    open_count: int = Field(ge=0)
    unread_count: int = Field(ge=0)
    progress_count: int = Field(ge=0)
    channel_issue_count: int = Field(ge=0)
    last_change_cursor: int = Field(ge=0)

    @classmethod
    def from_view(cls, view: NotificationSummaryView) -> "NotificationSummaryResponse":
        return cls(
            recipient_type=view.recipient_type,
            recipient_id=view.recipient_id,
            open_count=view.open_count,
            unread_count=view.unread_count,
            progress_count=view.progress_count,
            channel_issue_count=view.channel_issue_count,
            last_change_cursor=view.last_change_cursor,
        )


class NotificationFactResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    thread_id: UUID
    policy_version: int = Field(ge=1)
    kind: str
    priority: str
    transition: str
    topic: str
    root_task_id: UUID
    subtask_id: UUID | None
    worker_item_id: UUID | None
    source_projection_cursor: int = Field(ge=0)
    source_projection_type: str
    source_resource_id: UUID
    source_resource_version: int = Field(ge=1)
    title: str
    summary: str
    action: str
    deep_link: str
    correlation_id: str
    baseline_import: bool
    occurred_at: datetime
    created_at: datetime

    @classmethod
    def from_view(cls, view: NotificationFactView) -> "NotificationFactResponse":
        return cls.model_validate(view, from_attributes=True)


class NotificationDeliveryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    delivery_id: UUID
    route_id: UUID
    channel_type: str
    contract_version: int = Field(ge=1)
    status: str
    attempts: int = Field(ge=0)
    next_attempt_at: datetime
    last_error_code: str | None
    delivered_at: datetime | None
    exhausted_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_view(
        cls, view: NotificationDeliveryView
    ) -> "NotificationDeliveryResponse":
        return cls.model_validate(view, from_attributes=True)


class NotificationAttemptResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    delivery_id: UUID
    attempt_number: int = Field(ge=1)
    outcome: str | None
    error_code: str | None
    started_at: datetime
    finished_at: datetime | None

    @classmethod
    def from_view(cls, view: NotificationAttemptView) -> "NotificationAttemptResponse":
        return cls.model_validate(view, from_attributes=True)


class NotificationThreadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    workspace_id: UUID
    recipient_type: str
    recipient_id: str
    category: Literal["action", "progress", "archived"]
    priority: str
    topic: str
    root_task_id: UUID
    root_task_title: str
    subtask_id: UUID | None
    subtask_title: str | None
    worker_item_id: UUID | None
    worker_item_title: str | None
    latest_fact_id: UUID
    latest_fact_occurred_at: datetime
    title: str
    summary: str
    action: str
    deep_link: str
    notification_ref: str
    business_status: Literal["open", "resolved"]
    organization_status: Literal["active", "archived"]
    last_read_fact_id: UUID | None
    unread_count: int = Field(ge=0)
    version: int = Field(ge=1)
    opened_at: datetime
    resolved_at: datetime | None
    archived_at: datetime | None
    updated_at: datetime

    @classmethod
    def from_view(cls, view: NotificationThreadView) -> "NotificationThreadResponse":
        return cls.model_validate(view, from_attributes=True)


class NotificationThreadPageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: tuple[NotificationThreadResponse, ...]
    next_cursor: str | None
    last_change_cursor: int = Field(ge=0)


class NotificationFactPageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: tuple[NotificationFactResponse, ...]
    next_cursor: str | None


class NotificationThreadDetailResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread: NotificationThreadResponse
    facts: NotificationFactPageResponse
    deliveries: tuple[NotificationDeliveryResponse, ...]
    notification_ref: str
    available_commands: tuple[Literal["mark_read", "archive"], ...]
    deep_link: str
    last_change_cursor: int = Field(ge=0)

    @classmethod
    def from_view(
        cls, view: NotificationThreadDetailView
    ) -> "NotificationThreadDetailResponse":
        return cls(
            thread=NotificationThreadResponse.from_view(view.thread),
            facts=NotificationFactPageResponse(
                items=tuple(
                    NotificationFactResponse.from_view(item) for item in view.facts.items
                ),
                next_cursor=view.facts.next_cursor,
            ),
            deliveries=tuple(
                NotificationDeliveryResponse.from_view(item)
                for item in view.deliveries
            ),
            notification_ref=view.thread.notification_ref,
            available_commands=cast(
                tuple[Literal["mark_read", "archive"], ...],
                view.available_commands,
            ),
            deep_link=view.thread.deep_link,
            last_change_cursor=view.last_change_cursor,
        )


class NotificationAttemptPageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: tuple[NotificationAttemptResponse, ...]
    next_cursor: str | None


class NotificationDeliveryClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    module_instance_id: UUID
    supported_contract_versions: tuple[int, ...] = Field(min_length=1, max_length=16)
    batch_size: int = Field(ge=1, le=200)
    lease_seconds: int = Field(ge=5, le=300)


class NotificationDeliveryEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    delivery_id: UUID
    notification_id: UUID
    thread_id: UUID
    contract_version: int = Field(ge=1)
    claim_token: UUID
    fencing_token: int = Field(ge=1)
    idempotency_key: str
    channel_type: str
    source_ref: str
    kind: str
    priority: str
    root_task_title: str
    subtask_path: tuple[str, ...]
    worker_item_title: str | None
    title: str
    summary: str
    next_action: str
    deep_link: str
    correlation_id: str
    notification_ref: str
    created_at: datetime

    @classmethod
    def from_view(
        cls, view: NotificationDeliveryEnvelopeView
    ) -> "NotificationDeliveryEnvelope":
        return cls.model_validate(view, from_attributes=True)


class NotificationDeliveryClaimResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deliveries: tuple[NotificationDeliveryEnvelope, ...]


class NotificationDeliveryAckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_token: UUID
    fencing_token: int = Field(ge=1)
    completed_at: datetime
    provider_message_ref: str | None = Field(default=None, max_length=512)


class NotificationDeliveryNackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_token: UUID
    fencing_token: int = Field(ge=1)
    outcome: Literal["retryable_failure", "terminal_failure", "unknown_result"]
    stable_error_code: Literal[
        "transport_unavailable",
        "timeout",
        "rate_limited",
        "provider_unavailable",
        "unknown_result",
        "route_not_found",
        "route_revoked",
        "permission_denied",
        "contract_unsupported",
        "payload_rejected",
    ]
    retry_after_seconds: float | None = Field(default=None, ge=0, le=86_400)
    provider_message_ref: str | None = Field(default=None, max_length=512)


class NotificationDeliverySettlementResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    delivery_id: UUID
    status: str
    attempt_number: int = Field(ge=1)
    outcome: str
    provider_message_ref: str | None
    next_attempt_at: datetime | None
    fencing_token: int = Field(ge=1)
    error_code: str | None

    @classmethod
    def from_result(
        cls, result: NotificationDeliverySettlement
    ) -> "NotificationDeliverySettlementResponse":
        return cls(
            delivery_id=result.delivery_id,
            status=result.status.value,
            attempt_number=result.attempt_number,
            outcome=result.outcome.value,
            provider_message_ref=result.provider_message_ref,
            next_attempt_at=result.next_attempt_at,
            fencing_token=result.fencing_token,
            error_code=result.error_code.value if result.error_code else None,
        )
