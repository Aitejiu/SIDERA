"""Small, secret-free contracts used by the staged R6 acceptance runner."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

EvidenceStatus = Literal["NOT_RUN", "PASS", "FAIL", "INCOMPLETE"]
EvidenceKind = Literal["contract", "real_worker", "real_user_flow"]


class ScenarioEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    run_id: UUID
    scenario: str = Field(min_length=1, max_length=64)
    status: EvidenceStatus
    evidence_kind: EvidenceKind
    code_commit: str = Field(min_length=40, max_length=40)
    runtime_fingerprint: str = Field(min_length=64, max_length=64)
    root_task_id: UUID | None = None
    command_ids: tuple[UUID, ...] = ()
    execution_ids: tuple[UUID, ...] = ()
    session_ids: tuple[UUID, ...] = ()
    notification_fact_ids: tuple[UUID, ...] = ()
    notification_thread_ids: tuple[UUID, ...] = ()
    notification_intent_ids: tuple[UUID, ...] = ()
    notification_delivery_ids: tuple[UUID, ...] = ()
    notification_attempt_ids: tuple[UUID, ...] = ()
    provider_receipt_refs: tuple[str, ...] = ()
    check_codes: tuple[str, ...] = ()
    failure_code: str | None = None


class ClientOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal[
        "accepted", "observed", "conflict", "rejected", "incomplete"
    ]
    command_id: UUID | None = None
    body: dict[str, object] | None = None
    current_version: int | None = None
    diagnostic_code: str = Field(min_length=1, max_length=96)


class ReservationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["reserved", "replay", "blocked"]
    operation_id: str
    scenario: str
    role: str
    started_at: str
    deadline: str
    diagnostic_code: str
    resource_ids: tuple[str, ...] = ()


class PlanningBrief(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objective: str = Field(min_length=1, max_length=4000)
    constraints: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = Field(min_length=1)
    context_refs: tuple[str, ...] = ()
    requested_outputs: tuple[str, ...] = Field(min_length=1)
    implementation_worker_keys: tuple[str, str] = ("validation", "formatting")
    integration_worker_key: str = "integration"


class ConfirmedPlanEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    workspace_id: UUID
    main_agent_thread_id: str = Field(min_length=1, max_length=256)
    main_agent_turn_id: str = Field(min_length=1, max_length=256)
    planning_request_id: UUID
    proposal_id: UUID
    proposal_revision: int = Field(ge=1)
    proposal_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmation_command_id: UUID
    root_task_id: UUID
    confirmed_by: str = Field(min_length=1, max_length=128)
    pre_confirmation_status: str = Field(min_length=1, max_length=64)
    post_confirmation_status: str = Field(min_length=1, max_length=64)
    check_codes: tuple[str, ...] = ()


class ExecutionWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_id: UUID
    started_at: datetime
    finished_at: datetime


class UserFlowSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    run_id: UUID
    workspace_id: UUID
    root_task_id: UUID
    source_execution_ids: tuple[UUID, ...]
    source_session_ids: tuple[UUID, ...]
    integration_execution_id: UUID
    integration_session_id: UUID
    input_selection_id: UUID
    input_selection_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_id: UUID
    candidate_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_selection_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    verification_id: UUID | None
    verification_candidate_id: UUID | None
    verification_candidate_hash: str | None
    verification_status: Literal["passed", "failed", "unknown"]
    acceptance_id: UUID | None
    accepted_candidate_id: UUID | None
    accepted_candidate_hash: str | None
    accepted: bool
    accepted_tree_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    restored_tree_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    restore_check_passed: bool
    dependency_satisfied: bool
    source_windows: tuple[ExecutionWindow, ...]


class InterventionSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    run_id: UUID
    root_task_id: UUID
    worker_item_id: UUID
    sibling_worker_item_ids: tuple[UUID, ...]
    plan_generation_before: int = Field(ge=1)
    plan_generation_after: int = Field(ge=1)
    input_command_id: UUID
    input_id: UUID
    input_target_worker_item_id: UUID
    input_status: Literal[
        "accepted",
        "queued",
        "dispatching",
        "delivered",
        "blocked",
        "failed",
        "cancelled",
        "superseded",
        "unknown",
    ]
    adapter_acknowledged: bool
    observed_output_id: UUID | None
    observed_output_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    sibling_input_ids_before: tuple[UUID, ...]
    sibling_input_ids_after: tuple[UUID, ...]
    source_candidate_id: UUID
    source_candidate_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    failed_check_id: UUID | None
    failed_check_candidate_id: UUID | None
    failed_check_candidate_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    rework_record_id: UUID | None
    rework_execution_id: UUID | None
    rework_session_id: UUID | None
    rework_worker_item_id: UUID | None
    rework_generation: int | None = Field(default=None, ge=1)
    replacement_candidate_id: UUID | None
    replacement_candidate_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    rework_mode: Literal["automatic", "user"]
    rework_round: int | None = Field(default=None, ge=1)
    rework_round_limit: int | None = Field(default=None, ge=1)
    rework_elapsed_seconds: int = Field(ge=0)
    rework_time_limit_seconds: int | None = Field(default=None, ge=1)
    unaffected_result_ids_before: tuple[UUID, ...]
    unaffected_result_ids_after: tuple[UUID, ...]


class RevisionSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    run_id: UUID
    root_task_id: UUID
    main_agent_thread_id_before: str = Field(min_length=1, max_length=256)
    main_agent_thread_id_after: str = Field(min_length=1, max_length=256)
    main_agent_tool_call_observed: bool
    tool_call_id: str | None = Field(default=None, max_length=256)
    old_plan_id: UUID
    old_plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    old_generation: int = Field(ge=1)
    new_plan_id: UUID | None
    new_plan_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    new_generation: int | None = Field(default=None, ge=1)
    revision_id: UUID | None
    revision_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    confirmation_command_id: UUID | None
    transition_id: UUID | None
    old_plan_still_queryable: bool
    reused_worker_item_ids: tuple[UUID, ...]
    changed_worker_item_ids: tuple[UUID, ...]
    added_worker_item_ids: tuple[UUID, ...]
    old_worker_item_ids: tuple[UUID, ...]
    source_result_ids_before: tuple[UUID, ...]
    source_result_ids_after: tuple[UUID, ...]
    unimpacted_result_ids: tuple[UUID, ...]
    transition_worker_item_ids: tuple[UUID, ...]
    old_inputs_rejected_for_new_plan: bool
    transition_status: Literal["confirmed", "converged", "unknown", "failed"]


class RecoverySnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    run_id: UUID
    root_task_id: UUID
    command_id: UUID
    operation_id: UUID
    operation_id_after_restart: UUID
    restarted_components: tuple[str, ...] = Field(min_length=1)
    same_run_identity: bool
    plan_status_before: str = Field(min_length=1, max_length=64)
    plan_status_after: str = Field(min_length=1, max_length=64)
    candidate_hash_before: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    candidate_hash_after: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    pending_acceptance_before: bool
    pending_acceptance_after: bool
    unknown_outcome_observed: bool
    unknown_reconciled: bool
    duplicate_execution_count: int = Field(ge=0)
    provider_identity_proven: bool
    replacement_started: bool
    execution_count: int = Field(ge=0)
    execution_limit: int = Field(ge=1)
    elapsed_seconds: int = Field(ge=0)
    deadline_seconds: int = Field(ge=1)
    state_after: Literal["reconciled", "pending", "unknown", "failed"]


class SecondAdapterSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    run_id: UUID
    root_task_id: UUID
    workspace_id: UUID
    source_worker_item_id: UUID
    target_worker_item_id: UUID
    source_adapter_type: str = Field(min_length=1, max_length=128)
    target_adapter_type: str = Field(min_length=1, max_length=128)
    source_implementation: str = Field(min_length=1, max_length=256)
    target_implementation: str = Field(min_length=1, max_length=256)
    source_execution_id: UUID
    target_execution_id: UUID
    source_session_id: UUID
    target_session_id: UUID
    source_real_session: bool
    target_real_session: bool
    source_checkpoint_observed: bool
    source_stop_observed: bool
    source_unknown_reconciled: bool
    target_started_held: bool
    target_prompt_count_before_enable: int = Field(ge=0)
    target_enable_observed: bool
    target_prompt_count_after_enable: int = Field(ge=0)
    target_single_writer_observed: bool
    workspace_reused_exact: bool
    context_binding_observed: bool
    source_context_marker_observed: bool
    target_context_marker_observed: bool
    target_result_observed: bool
    trace_usage_gap_explicit: bool


class NotificationSnapshot(BaseModel):
    """Secret-free facts collected from the normal notification path."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    run_id: UUID
    root_task_id: UUID
    fact_id: UUID
    thread_id: UUID
    intent_id: UUID
    delivery_id: UUID
    attempt_id: UUID
    provider_receipt_ref: str = Field(min_length=1, max_length=512)
    source_ref_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    private_source_ref_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    web_root_task_id: UUID
    private_root_task_id: UUID
    web_subtask_id: UUID | None = None
    private_subtask_id: UUID | None = None
    web_worker_item_id: UUID | None = None
    private_worker_item_id: UUID | None = None
    route_status: Literal["authorized", "missing", "unknown", "revoked"]
    route_scope_valid: bool
    normal_business_path_observed: bool
    gateway_claim_observed: bool
    gateway_settle_observed: bool
    message_has_task_context: bool
    deep_link_targets_exact_resource: bool
    web_mail_matches_private: bool
    provider_status: Literal["accepted", "rejected", "unknown"]
    user_acknowledged: bool
    main_agent_re_read_latest_fact: bool
    ack_loss_recovered: bool
    business_state_preserved_after_recovery: bool
    only_same_source_route_delivered: bool


class WebNotificationSnapshot(BaseModel):
    """Facts for the local inbox channel; no external IM receipt is implied."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    run_id: UUID
    root_task_id: UUID
    subtask_id: UUID | None = None
    worker_item_id: UUID | None = None
    event_id: UUID
    inbox_item_id: UUID
    channel: Literal["web_inbox"]
    deep_link: str = Field(min_length=1, max_length=512)
    deep_link_resource_id: UUID
    event_observed: bool
    inbox_created: bool
    exact_mapping: bool
    user_read: bool
    user_acknowledged: bool
    duplicate_count: int = Field(ge=0)
    task_state_preserved: bool


class RuntimeLimitEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    run_id: UUID
    configured_worker_capacity: int = Field(ge=1)
    observed_peak_worker_capacity: int = Field(ge=0)
    configured_retry_limit: int = Field(ge=0)
    observed_retry_count: int = Field(ge=0)
    session_deadline_seconds: int = Field(ge=1)
    observed_elapsed_seconds: int = Field(ge=0)
    deadline_preserved_across_restart: bool
    unknown_relaunch_blocked: bool
    usage_status: Literal["available", "unavailable", "unknown"]
