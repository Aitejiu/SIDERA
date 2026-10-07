import type { components } from "@/shared/api/schema";

export type WorkerWorkspace = components["schemas"]["WorkerWorkspaceProjectionResponse"];
export type WorkerItemCollaborationSummary = NonNullable<WorkerWorkspace["collaboration"]>;
export type WorkerActivity = components["schemas"]["WorkerActivityProjectionResponse"];
export type WorkerActivityPage = components["schemas"]["WorkerItemActivityPageResponse"];
export type WorkerRuntime = components["schemas"]["WorkerRuntimeProjectionResponse"];
export type WorkerExecution = components["schemas"]["WorkerExecutionSummaryProjectionResponse"];
export type WorkerPlacement = components["schemas"]["WorkerItemPlacementProjectionResponse"];
export type WorkerTransition = components["schemas"]["WorkerTransitionResponse"];
export type RecoverWorkerTransitionPayload = components["schemas"]["RecoverWorkerTransitionPayload"];
export type HandoffCandidateList = components["schemas"]["HandoffCandidateListResponse"];
export type HandoffCandidate = components["schemas"]["HandoffCandidateResponse"];
export type HandoffProposal = components["schemas"]["HandoffProposalResponse"];
export type HandoffTransition = components["schemas"]["HandoffTransitionResponse"];
export type NativeHandoffProposal = components["schemas"]["NativeHandoffProposalResponse"];
export type NativeHandoffTransition = components["schemas"]["NativeHandoffTransitionResponse"];
export type PublicHandoffProposal = HandoffProposal | NativeHandoffProposal;
export type PublicHandoffTransition = HandoffTransition | NativeHandoffTransition;
export type ActiveHandoff = components["schemas"]["ActiveHandoffProjectionResponse"];
export type HandoffTraceDescriptor = components["schemas"]["TraceDescriptorProjectionResponse"];
export type PrepareWorkerHandoffPayload = components["schemas"]["PrepareWorkerHandoffPayload"];
export type ConfirmWorkerHandoffPayload = components["schemas"]["ConfirmWorkerHandoffPayload"];
export type CancelWorkerHandoffPayload = components["schemas"]["CancelWorkerHandoffPayload"];
export type RecoverWorkerHandoffPayload = components["schemas"]["RecoverWorkerHandoffPayload"];
export type CommandRequest = components["schemas"]["CommandRequest"];
export type CommandResponse = components["schemas"]["CommandResponse"];

export function isNativeHandoffProposal(value: unknown): value is NativeHandoffProposal {
  return isRecord(value)
    && value.proposal_schema_version === 2
    && nonblank(value.id)
    && nonblank(value.workspace_id)
    && nonblank(value.worker_item_id)
    && nonblank(value.root_task_id)
    && nonblank(value.subtask_id)
    && positiveInteger(value.source_worker_item_version)
    && positiveInteger(value.version)
    && nonblank(value.proposal_hash)
    && typeof value.status === "string"
    && isProfileRef(value.source_profile_ref)
    && isNativeAssignee(value.target_assignee)
    && (value.target_profile_ref === null || isProfileRef(value.target_profile_ref))
    && value.target_worker_id === null
    && isNativeProposalContext(value.context)
    && isPlanBase(value.plan_base)
    && nonblank(value.source_execution_id)
    && nonblank(value.source_session_id)
    && nonblank(value.source_run_id)
    && positiveInteger(value.source_binding_version)
    && typeof value.handoff_reason === "string"
    && typeof value.handoff_instruction === "string"
    && typeof value.created_at === "string"
    && typeof value.expires_at === "string"
    && (value.confirmed_at === null || typeof value.confirmed_at === "string")
    && (value.cancelled_at === null || typeof value.cancelled_at === "string")
    && (value.stale_reason === null || typeof value.stale_reason === "string");
}

export function isNativeHandoffTransition(value: unknown): value is NativeHandoffTransition {
  return isRecord(value)
    && value.handoff_schema_version === 2
    && nonblank(value.id)
    && nonblank(value.workspace_id)
    && nonblank(value.worker_item_id)
    && nonblank(value.proposal_id)
    && positiveInteger(value.version)
    && typeof value.status === "string"
    && typeof value.phase === "string"
    && typeof value.writer_state === "string"
    && isProfileRef(value.source_profile_ref)
    && isNativeAssignee(value.target_assignee)
    && (value.target_profile_ref === null || isProfileRef(value.target_profile_ref))
    && (value.target_worker_id === null || nonblank(value.target_worker_id))
    && (value.target_execution_id === null || nonblank(value.target_execution_id))
    && (value.target_session_id === null || nonblank(value.target_session_id))
    && (value.target_run_id === null || nonblank(value.target_run_id))
    && (value.target_binding_version === null || positiveInteger(value.target_binding_version))
    && positiveInteger(value.target_context_version)
    && isNativeProposalContext(value.context)
    && isPlanBase(value.plan_base)
    && nonblank(value.source_execution_id)
    && nonblank(value.source_session_id)
    && nonblank(value.source_run_id)
    && positiveInteger(value.source_binding_version)
    && typeof value.source_input_cutoff_sequence === "number"
    && Number.isSafeInteger(value.source_input_cutoff_sequence)
    && value.source_input_cutoff_sequence >= 0
    && Array.isArray(value.available_recovery_strategies)
    && value.available_recovery_strategies.length === 0
    && (value.failure_code === null || typeof value.failure_code === "string")
    && (value.blocked_operation_id === null || typeof value.blocked_operation_id === "string")
    && (value.completed_at === null || typeof value.completed_at === "string")
    && typeof value.confirmed_at === "string"
    && typeof value.logical_attempt === "number"
    && Number.isSafeInteger(value.logical_attempt)
    && value.logical_attempt > 0
    && (value.next_attempt_at === null || typeof value.next_attempt_at === "string")
    && (value.started_at === null || typeof value.started_at === "string");
}

export function isLegacyHandoffProposal(value: unknown): value is HandoffProposal {
  if (!isRecord(value) || looksLikeNativeProposal(value)) return false;
  return nonblank(value.id)
    && nonblank(value.workspace_id)
    && typeof value.status === "string"
    && positiveInteger(value.version)
    && isRecord(value.source) && isLegacyParticipant(value.source)
    && isRecord(value.target) && isLegacyParticipant(value.target)
    && isRecord(value.workspace)
    && isRecord(value.worker_item)
    && isRecord(value.context)
    && isPlanBase(value.plan_base)
    && typeof value.transfer_mode === "string";
}

export function isLegacyHandoffTransition(value: unknown): value is HandoffTransition {
  if (!isRecord(value) || looksLikeNativeTransition(value)) return false;
  return nonblank(value.id)
    && nonblank(value.workspace_id)
    && nonblank(value.proposal_id)
    && typeof value.status === "string"
    && typeof value.phase === "string"
    && typeof value.writer_state === "string"
    && positiveInteger(value.version)
    && isRecord(value.source) && isLegacyParticipant(value.source)
    && isRecord(value.target) && isLegacyParticipant(value.target)
    && isRecord(value.workspace)
    && isRecord(value.worker_item)
    && isRecord(value.context)
    && typeof value.proposal_hash === "string"
    && Array.isArray(value.available_recovery_strategies)
    && Array.isArray(value.recovery_evidence_refs);
}

export function looksLikeNativeProposal(value: unknown): boolean {
  return isRecord(value) && [
    "proposal_schema_version",
    "source_profile_ref",
    "source_execution_id",
    "source_session_id",
    "source_run_id",
    "source_binding_version",
    "target_assignee",
  ].some((key) => Object.prototype.hasOwnProperty.call(value, key));
}

export function looksLikeNativeTransition(value: unknown): boolean {
  return isRecord(value) && [
    "handoff_schema_version",
    "source_profile_ref",
    "source_execution_id",
    "source_session_id",
    "source_run_id",
    "source_binding_version",
    "target_assignee",
    "target_profile_ref",
  ].some((key) => Object.prototype.hasOwnProperty.call(value, key));
}

function isNativeProposalContext(value: unknown): boolean {
  if (!isRecord(value) || !nonblank(value.source_package_id)
    || !nonblank(value.source_package_hash) || !positiveInteger(value.source_context_version)
    || !(value.target_package_id === null || nonblank(value.target_package_id))
    || !(value.target_package_hash === null || nonblank(value.target_package_hash))
    || !(value.target_context_version === null || positiveInteger(value.target_context_version))) {
    return false;
  }
  return (value.target_package_id === null) === (value.target_package_hash === null);
}

function isNativeAssignee(value: unknown): value is NativeHandoffProposal["target_assignee"] {
  return isRecord(value) && (value.kind === "human" || value.kind === "profile") && nonblank(value.id);
}

function isProfileRef(value: unknown): value is NativeHandoffProposal["source_profile_ref"] {
  return isRecord(value) && nonblank(value.workspace_id) && nonblank(value.profile_id)
    && positiveInteger(value.revision);
}

function isPlanBase(value: unknown): boolean {
  return isRecord(value) && nonblank(value.snapshot_id)
    && positiveInteger(value.generation) && positiveInteger(value.head_version);
}

function isLegacyParticipant(value: Record<string, unknown>): boolean {
  return isRecord(value.worker) && nonblank(value.worker.worker_id)
    && typeof value.worker.display_name === "string"
    && typeof value.worker.adapter_type === "string";
}

function nonblank(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function positiveInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
