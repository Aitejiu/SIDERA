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
    accepted_tree_hash: str = Field(pattern=r"^[0-9a-f]{40}$")
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
    input_status: Literal["queued", "accepted", "delivered", "applied", "failed", "unknown"]
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
    rework_round: int = Field(ge=0)
    rework_round_limit: int = Field(ge=1)
    rework_elapsed_seconds: int = Field(ge=0)
    rework_time_limit_seconds: int = Field(ge=1)
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
