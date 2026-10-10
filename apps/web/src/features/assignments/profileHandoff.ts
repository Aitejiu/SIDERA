import type { ProfileRef } from "@/features/agents/types";
import { sessionObservationMatches } from "@/shared/sessionObservation";
import type {
  AssigneeValue,
  CommandRequest,
  NativeWorkerContextView,
} from "@/features/assignments/types";
import {
  isNativeHandoffProposal,
  isNativeHandoffTransition,
  type NativeHandoffTransition,
  type NativeHandoffProposal,
  type WorkerWorkspace,
} from "@/features/worker-items/types";

export interface HandoffScope {
  workspace_id: string;
  principal_id: string;
  worker_item_id: string;
}

export type HandoffActionName = "prepare" | "confirm" | "cancel";
export type HandoffActionStatus = "idle" | "pending" | "unknown" | "applied" | "rejected";

export interface HandoffActionState {
  original: CommandRequest | null;
  status: HandoffActionStatus;
  rejectionCode: string | null;
}

export interface HandoffDraft {
  schema_version: 1;
  scope: HandoffScope;
  source_review: HandoffSourceReview | null;
  target: AssigneeValue | null;
  reason: string;
  instruction: string;
  prepared: HandoffPreparedAnchor | null;
  transition_id: string | null;
  actions: Record<HandoffActionName, HandoffActionState>;
}

export interface HandoffSourceReview {
  workspace_id: string;
  principal_id: string;
  worker_item_id: string;
  root_task_id: string;
  subtask_id: string;
  worker_item_version: number;
  execution_mode: "native_profile";
  execution_id: string;
  session_id: string;
  session_version: number;
  session_last_sequence?: number;
  worker_id: string;
  binding_version: number;
  run_id: string;
  plan_generation: number;
  context_version: number;
  source_profile_ref: ProfileRef;
  plan_snapshot_id: string;
}

export type HandoffSourceReviewResult =
  | { ok: true; review: HandoffSourceReview }
  | { ok: false; reason: string };

export interface HandoffPreparedAnchor {
  proposal: NativeHandoffProposal;
  source_review: HandoffSourceReview;
  target: AssigneeValue;
}

export type HandoffActionMappingResult =
  | { ok: true; proposal: NativeHandoffProposal; transition: NativeHandoffTransition | null }
  | { ok: false; reason: "mapping_unavailable" };

export type HandoffPreparedReviewResult =
  | { ok: true; anchor: HandoffPreparedAnchor }
  | { ok: false; reason: string };

const ACTIVE_STATES = new Set(["running", "idle"]);
const TERMINAL_LINK_STATES = new Set(["succeeded", "failed", "stopped", "cancelled"]);
const HANDOFF_DRAFT_PREFIX = "sidera.native-assignment.handoff.v1";
const HANDOFF_STORAGE_UNAVAILABLE = "本地交接记录暂不可用";
const HANDOFF_ACTIONS = ["prepare", "confirm", "cancel"] as const;
const SAFE_HANDOFF_REJECTIONS = new Set([
  "assignment_forbidden",
  "handoff_required",
  "not_found",
  "permission_denied",
  "validation_error",
  "version_conflict",
  "unknown",
]);

export function handoffDraftKey(scope: HandoffScope): string {
  return `${HANDOFF_DRAFT_PREFIX}/${encodeURIComponent(scope.workspace_id)}/${encodeURIComponent(scope.principal_id)}/${encodeURIComponent(scope.worker_item_id)}`;
}

export function loadHandoffDraft(
  scope: HandoffScope,
): { ok: true; draft: HandoffDraft | null } | { ok: false; reason: string } {
  if (!validHandoffScope(scope)) {
    return { ok: false, reason: HANDOFF_STORAGE_UNAVAILABLE };
  }
  try {
    const storage = globalThis.localStorage;
    if (!storage) return { ok: false, reason: HANDOFF_STORAGE_UNAVAILABLE };
    const stored = storage.getItem(handoffDraftKey(scope));
    if (stored === null) return { ok: true, draft: null };
    let value: unknown;
    try {
      value = JSON.parse(stored);
    } catch {
      return { ok: false, reason: HANDOFF_STORAGE_UNAVAILABLE };
    }
    if (!isHandoffDraft(value, scope)) return { ok: false, reason: HANDOFF_STORAGE_UNAVAILABLE };
    return { ok: true, draft: value };
  } catch {
    return { ok: false, reason: HANDOFF_STORAGE_UNAVAILABLE };
  }
}

export function saveHandoffDraft(
  scope: HandoffScope,
  draft: HandoffDraft,
): { ok: true } | { ok: false; reason: string } {
  if (!validHandoffScope(scope) || !isHandoffDraft(draft, scope)) {
    return { ok: false, reason: HANDOFF_STORAGE_UNAVAILABLE };
  }
  try {
    const storage = globalThis.localStorage;
    if (!storage) return { ok: false, reason: HANDOFF_STORAGE_UNAVAILABLE };
    storage.setItem(handoffDraftKey(scope), JSON.stringify(draft));
    return { ok: true };
  } catch {
    return { ok: false, reason: HANDOFF_STORAGE_UNAVAILABLE };
  }
}

export function mapHandoffActionResult(input: {
  action: HandoffActionName;
  original: CommandRequest;
  response: unknown;
  scope: HandoffScope;
  source_review: HandoffSourceReview;
  target: AssigneeValue;
  prepared: HandoffPreparedAnchor | null;
}): HandoffActionMappingResult {
  const { action, original, response, scope, source_review, target, prepared } = input;
  const reason = action === "prepare" && typeof original.payload.handoff_reason === "string"
    ? original.payload.handoff_reason
    : "";
  const instruction = action === "prepare" && typeof original.payload.handoff_instruction === "string"
    ? original.payload.handoff_instruction
    : "";
  if (!validHandoffScope(scope)
    || !isStoredSourceReview(source_review, scope)
    || !isStoredTarget(target, scope)
    || !isStoredCommand(original, action, scope, source_review, target, prepared, reason, instruction)
    || !isRecord(response)
    || response.command_id !== original.command_id
    || !nonblank(response.aggregate_id)
    || !positiveInteger(response.aggregate_version)
    || !Array.isArray(response.event_ids)
    || !isRecord(response.data)) {
    return mappingUnavailable();
  }

  if (action === "prepare") {
    const proposal = response.data.proposal;
    if (!isNativeHandoffProposal(proposal)
      || proposal.id === scope.worker_item_id
      || response.aggregate_id !== proposal.id
      || response.aggregate_version !== proposal.version
      || !proposalMatchesPrepare(proposal, original, scope, source_review, target)) {
      return mappingUnavailable();
    }
    return { ok: true, proposal: cloneProposal(proposal), transition: null };
  }

  if (prepared === null || prepared.proposal.status !== "prepared") return mappingUnavailable();
  if (action === "cancel") {
    const proposal = response.data.proposal;
    if (!isNativeHandoffProposal(proposal)
      || response.aggregate_id !== prepared.proposal.id
      || proposal.id !== prepared.proposal.id
      || proposal.version !== prepared.proposal.version + 1
      || response.aggregate_version !== proposal.version
      || proposal.status !== "cancelled"
      || proposal.cancelled_at === null
      || proposal.confirmed_at !== prepared.proposal.confirmed_at
      || !sameImmutableProposal(proposal, prepared.proposal)) {
      return mappingUnavailable();
    }
    return { ok: true, proposal: cloneProposal(proposal), transition: null };
  }

  const proposal = response.data.proposal;
  const transition = response.data.transition;
  if (!isNativeHandoffProposal(proposal)
    || !isNativeHandoffTransition(transition)
    || proposal.id !== prepared.proposal.id
    || proposal.version !== prepared.proposal.version + 1
    || proposal.status !== "confirmed"
    || proposal.confirmed_at === null
    || proposal.cancelled_at !== null
    || !sameImmutableProposal(proposal, prepared.proposal)
    || transition.id === prepared.proposal.id
    || transition.proposal_id !== prepared.proposal.id
    || transition.workspace_id !== scope.workspace_id
    || transition.worker_item_id !== scope.worker_item_id
    || response.aggregate_id !== transition.id
    || response.aggregate_version !== transition.version
    || transition.confirmed_at !== proposal.confirmed_at
    || !transitionMatchesProposal(transition, proposal)) {
    return mappingUnavailable();
  }
  return {
    ok: true,
    proposal: cloneProposal(proposal),
    transition: cloneTransition(transition),
  };
}

export function reviewHandoffSource(input: {
  workspace: WorkerWorkspace;
  nativeContext: NativeWorkerContextView;
  principalId: string;
}): HandoffSourceReviewResult {
  const { workspace, nativeContext, principalId } = input;
  const workerItem = workspace.worker_item;
  const placement = workspace.current_placement;
  const runtime = workspace.current_runtime;
  const execution = nativeContext.execution;
  const address = nativeContext.context;
  const run = nativeContext.run;
  const binding = nativeContext.binding;

  if (!nonblank(principalId)
    || !nonblank(workspace.workspace_id)
    || nativeContext.workspace_id !== workspace.workspace_id
    || nativeContext.worker_item_id !== workerItem.id
    || workerItem.status !== "active"
    || !positiveInteger(workerItem.version)
    || workerItem.acknowledged_context_version !== workerItem.context_version
    || !positiveInteger(workerItem.context_version)
    || !nonblank(workerItem.root_task_id)
    || !nonblank(placement.plan_snapshot_id)
    || !positiveInteger(placement.plan_generation)
    || placement.subtask_task_id === null
    || !nonblank(placement.subtask_task_id)
    || nativeContext.worker_item_version !== workerItem.version
    || nativeContext.execution_mode !== "native_profile"
    || binding === null
    || !validProfileRef(binding.profile_ref, workspace.workspace_id)
    || binding.assignee.kind !== "profile"
    || binding.assignee.id !== binding.profile_ref.profile_id
    || !positiveInteger(binding.version)
    || runtime === null
    || runtime.adapter_type !== "agent-run"
    || !positiveInteger(runtime.session_version)
    || execution === null
    || address === null
    || run === null
    || execution.workspace_id !== workspace.workspace_id
    || execution.worker_item_id !== workerItem.id
    || execution.root_task_id !== workerItem.root_task_id
    || execution.subtask_id !== placement.subtask_task_id
    || execution.status !== runtime.execution_status
    || !ACTIVE_STATES.has(execution.status)
    || execution.run_id === null
    || !nonblank(execution.run_id)
    || execution.profile_ref === null
    || !sameProfileRef(execution.profile_ref, binding.profile_ref)
    || !positiveInteger(execution.binding_version)
    || TERMINAL_LINK_STATES.has(execution.link_status ?? "")
    || nativeContext.execution_version === null
    || nativeContext.execution_version === undefined
    || !positiveInteger(nativeContext.execution_version)
    || address.execution_mode !== "native_profile"
    || address.worker_item_id !== workerItem.id
    || address.execution_id !== execution.execution_id
    || address.session_id !== runtime.session_id
    || !sessionObservationMatches(address.session_version, address.session_last_sequence,
      address.session_version, address.session_last_sequence)
    || (address.session_last_sequence == null
      ? address.session_version !== runtime.session_version
      : runtime.session_version > address.session_version)
    || address.worker_id !== runtime.worker_id
    || address.binding_version !== binding.version
    || address.binding_version !== execution.binding_version
    || address.run_id !== execution.run_id
    || address.plan_generation !== placement.plan_generation
    || address.context_version !== workerItem.context_version
    || !positiveInteger(address.session_version)
    || !positiveInteger(address.binding_version)
    || !positiveInteger(address.context_version)
    || !positiveInteger(address.plan_generation)
    || !nonblank(address.execution_id)
    || !nonblank(address.session_id)
    || !nonblank(address.worker_id)
    || !nonblank(address.run_id)
    || runtime.session_status !== "active"
    || runtime.execution_id !== execution.execution_id
    || runtime.session_id === null
    || runtime.worker_id === null
    || runtime.task_id !== placement.subtask_task_id
    || !ACTIVE_STATES.has(run.state)
    || !sameProfileRef(run.profile_ref, binding.profile_ref)
    || run.runtime_ref.workspace_id !== workspace.workspace_id) {
    return { ok: false, reason: "当前 WorkerItem 的 native 来源事实不可安全确认" };
  }

  const summary = workspace.execution_history.find((item) => item.execution_id === execution.execution_id);
  if (summary === undefined
    || summary.version !== nativeContext.execution_version
    || summary.status !== execution.status
    || summary.task_id !== execution.subtask_id
    || summary.session_id !== address.session_id
    || summary.worker_id !== address.worker_id) {
    return { ok: false, reason: "当前 native Execution 与 WorkerItem 摘要尚未对齐" };
  }

  return {
    ok: true,
    review: {
      workspace_id: workspace.workspace_id,
      principal_id: principalId,
      worker_item_id: workerItem.id,
      root_task_id: workerItem.root_task_id,
      subtask_id: placement.subtask_task_id,
      worker_item_version: workerItem.version,
      execution_mode: "native_profile",
      execution_id: execution.execution_id,
      session_id: address.session_id,
      session_version: address.session_version,
      ...(address.session_last_sequence == null ? {} : { session_last_sequence: address.session_last_sequence }),
      worker_id: address.worker_id,
      binding_version: address.binding_version,
      run_id: address.run_id,
      plan_generation: address.plan_generation,
      context_version: address.context_version,
      source_profile_ref: { ...binding.profile_ref },
      plan_snapshot_id: placement.plan_snapshot_id,
    },
  };
}

export function reviewPreparedHandoff(input: {
  review: HandoffSourceReview;
  proposal: NativeHandoffProposal;
  workspace: WorkerWorkspace;
  nativeContext: NativeWorkerContextView;
  target: AssigneeValue;
  now: number;
}): HandoffPreparedReviewResult {
  const { review, proposal, workspace, nativeContext, target, now } = input;
  const source = reviewHandoffSource({
    workspace,
    nativeContext,
    principalId: review.principal_id,
  });
  const expiresAt = isNativeHandoffProposal(proposal) ? Date.parse(proposal.expires_at) : Number.NaN;
  if (!source.ok
    || !sameHandoffSourceReview(review, source.review)
    || !isNativeHandoffProposal(proposal)
    || !Number.isFinite(now)
    || !Number.isFinite(expiresAt)
    || expiresAt <= now
    || proposal.status !== "prepared"
    || proposal.stale_reason !== null
    || proposal.workspace_id !== review.workspace_id
    || proposal.worker_item_id !== review.worker_item_id
    || proposal.root_task_id !== review.root_task_id
    || proposal.subtask_id !== review.subtask_id
    || proposal.source_worker_item_version !== review.worker_item_version
    || proposal.source_execution_id !== review.execution_id
    || proposal.source_session_id !== review.session_id
    || proposal.source_run_id !== review.run_id
    || proposal.source_binding_version !== review.binding_version
    || !sameProfileRef(proposal.source_profile_ref, review.source_profile_ref)
    || proposal.context.target_context_version !== review.context_version
    || proposal.context.target_package_id !== null
    || proposal.context.target_package_hash !== null
    || proposal.plan_base.snapshot_id !== review.plan_snapshot_id
    || proposal.plan_base.generation !== review.plan_generation
    || !sameTarget(proposal, target, review)) {
    return { ok: false, reason: "Profile 交接方案已变化或暂不可用" };
  }

  return {
    ok: true,
    anchor: {
      proposal: cloneProposal(proposal),
      source_review: cloneReview(review),
      target: cloneTarget(target),
    },
  };
}

function proposalMatchesPrepare(
  proposal: NativeHandoffProposal,
  original: CommandRequest,
  scope: HandoffScope,
  review: HandoffSourceReview,
  target: AssigneeValue,
): boolean {
  return proposal.workspace_id === scope.workspace_id
    && proposal.worker_item_id === scope.worker_item_id
    && proposal.root_task_id === review.root_task_id
    && proposal.subtask_id === review.subtask_id
    && proposal.source_worker_item_version === original.expected_version
    && proposal.source_worker_item_version === review.worker_item_version
    && proposal.source_execution_id === review.execution_id
    && proposal.source_session_id === review.session_id
    && proposal.source_run_id === review.run_id
    && proposal.source_binding_version === review.binding_version
    && proposal.handoff_reason === original.payload.handoff_reason
    && proposal.handoff_instruction === original.payload.handoff_instruction
    && sameProfileRef(proposal.source_profile_ref, review.source_profile_ref)
    && proposal.context.target_context_version === review.context_version
    && proposal.context.target_package_id === null
    && proposal.context.target_package_hash === null
    && proposal.plan_base.snapshot_id === review.plan_snapshot_id
    && proposal.plan_base.generation === review.plan_generation
    && matchesAddress(original.payload.context, review)
    && matchesTargetPayload(original.payload.target_assignee, original.payload.target_profile_ref, target)
    && matchesProposalTarget(proposal, target);
}

function sameImmutableProposal(
  left: NativeHandoffProposal,
  right: NativeHandoffProposal,
): boolean {
  return left.id === right.id
    && left.proposal_schema_version === right.proposal_schema_version
    && left.workspace_id === right.workspace_id
    && left.root_task_id === right.root_task_id
    && left.subtask_id === right.subtask_id
    && left.worker_item_id === right.worker_item_id
    && left.source_worker_item_version === right.source_worker_item_version
    && left.proposal_hash === right.proposal_hash
    && left.source_execution_id === right.source_execution_id
    && left.source_session_id === right.source_session_id
    && left.source_run_id === right.source_run_id
    && left.source_binding_version === right.source_binding_version
    && sameProfileRef(left.source_profile_ref, right.source_profile_ref)
    && left.target_assignee.kind === right.target_assignee.kind
    && left.target_assignee.id === right.target_assignee.id
    && (left.target_profile_ref === null || right.target_profile_ref !== null)
    && (left.target_profile_ref === null
      ? right.target_profile_ref === null
      : right.target_profile_ref !== null && sameProfileRef(left.target_profile_ref, right.target_profile_ref))
    && left.target_worker_id === right.target_worker_id
    && left.handoff_reason === right.handoff_reason
    && left.handoff_instruction === right.handoff_instruction
    && sameHandoffContext(left.context, right.context)
    && left.plan_base.snapshot_id === right.plan_base.snapshot_id
    && left.plan_base.generation === right.plan_base.generation
    && left.plan_base.head_version === right.plan_base.head_version
    && left.created_at === right.created_at
    && left.expires_at === right.expires_at;
}

export function samePreparedHandoff(
  frozen: NativeHandoffProposal,
  current: NativeHandoffProposal,
): boolean {
  return sameImmutableProposal(frozen, current)
    && frozen.version === current.version
    && frozen.status === current.status
    && frozen.stale_reason === current.stale_reason
    && frozen.confirmed_at === current.confirmed_at
    && frozen.cancelled_at === current.cancelled_at;
}

export function sameHandoffSourceReview(
  saved: HandoffSourceReview,
  current: HandoffSourceReview,
): boolean {
  const normalized = { ...current, session_version: saved.session_version };
  if (saved.session_last_sequence === undefined) delete normalized.session_last_sequence;
  else normalized.session_last_sequence = saved.session_last_sequence;
  return sessionObservationMatches(saved.session_version, saved.session_last_sequence,
    current.session_version, current.session_last_sequence)
    && sameSourceReview(saved, normalized);
}

function sameHandoffContext(
  left: NativeHandoffProposal["context"],
  right: NativeHandoffProposal["context"],
): boolean {
  return left.source_package_id === right.source_package_id
    && left.source_package_hash === right.source_package_hash
    && left.source_context_version === right.source_context_version
    && left.target_package_id === right.target_package_id
    && left.target_package_hash === right.target_package_hash
    && left.target_context_version === right.target_context_version;
}

function transitionMatchesProposal(
  transition: NativeHandoffTransition,
  proposal: NativeHandoffProposal,
): boolean {
  return transition.source_execution_id === proposal.source_execution_id
    && transition.source_session_id === proposal.source_session_id
    && transition.source_run_id === proposal.source_run_id
    && transition.source_binding_version === proposal.source_binding_version
    && sameProfileRef(transition.source_profile_ref, proposal.source_profile_ref)
    && transition.target_assignee.kind === proposal.target_assignee.kind
    && transition.target_assignee.id === proposal.target_assignee.id
    && (transition.target_profile_ref === null || proposal.target_profile_ref !== null)
    && (transition.target_profile_ref === null
      ? proposal.target_profile_ref === null
      : proposal.target_profile_ref !== null
        && sameProfileRef(transition.target_profile_ref, proposal.target_profile_ref))
    && transition.target_worker_id === proposal.target_worker_id
    && transition.target_context_version === proposal.context.target_context_version
    && sameHandoffContext(transition.context, proposal.context)
    && transition.plan_base.snapshot_id === proposal.plan_base.snapshot_id
    && transition.plan_base.generation === proposal.plan_base.generation
    && transition.plan_base.head_version === proposal.plan_base.head_version;
}

function cloneTransition(transition: NativeHandoffTransition): NativeHandoffTransition {
  return {
    ...transition,
    source_profile_ref: { ...transition.source_profile_ref },
    target_assignee: { ...transition.target_assignee },
    target_profile_ref: transition.target_profile_ref === null
      ? null
      : { ...transition.target_profile_ref },
    context: { ...transition.context },
    plan_base: { ...transition.plan_base },
    available_recovery_strategies: [...transition.available_recovery_strategies],
  };
}

function mappingUnavailable(): HandoffActionMappingResult {
  return { ok: false, reason: "mapping_unavailable" };
}

function isHandoffDraft(value: unknown, scope: HandoffScope): value is HandoffDraft {
  if (!isRecord(value) || !hasExactKeys(value, [
    "schema_version", "scope", "source_review", "target", "reason", "instruction",
    "prepared", "transition_id", "actions",
  ]) || value.schema_version !== 1 || !sameHandoffScope(value.scope, scope)
    || !(value.source_review === null || isStoredSourceReview(value.source_review, scope))
    || !(value.target === null || isStoredTarget(value.target, scope))
    || typeof value.reason !== "string" || typeof value.instruction !== "string"
    || !(value.transition_id === null || isUuid(value.transition_id))
    || !isRecord(value.actions) || !hasExactKeys(value.actions, HANDOFF_ACTIONS)) {
    return false;
  }

  const sourceReview = value.source_review;
  const target = value.target;
  const prepared = value.prepared;
  if (prepared !== null) {
    if (sourceReview === null || !isPreparedAnchor(prepared, scope, sourceReview, target)
      || !["prepared", "expired", "stale"].includes(prepared.proposal.status)
      || prepared.proposal.handoff_reason !== value.reason
      || prepared.proposal.handoff_instruction !== value.instruction) return false;
  }

  const actions = value.actions;
  if (!isStoredAction(actions.prepare, "prepare", scope, sourceReview, target, prepared,
    value.reason, value.instruction)
    || !isStoredAction(actions.confirm, "confirm", scope, sourceReview, target, prepared,
      value.reason, value.instruction)
    || !isStoredAction(actions.cancel, "cancel", scope, sourceReview, target, prepared,
      value.reason, value.instruction)) return false;
  const { prepare, confirm, cancel } = actions;

  if (prepared !== null && (prepare.original === null || prepare.status !== "applied")) return false;
  if (confirm.original !== null && prepared === null) return false;
  if (value.transition_id !== null && (confirm.original === null || confirm.status !== "applied")) return false;
  if (cancel.original !== null && (prepared === null || value.transition_id !== null)) return false;
  return true;
}

function isStoredAction(
  value: unknown,
  action: HandoffActionName,
  scope: HandoffScope,
  sourceReview: unknown,
  target: unknown,
  prepared: unknown,
  reason: string,
  instruction: string,
): value is HandoffActionState {
  if (!isRecord(value) || !hasExactKeys(value, ["original", "status", "rejectionCode"])
    || !["idle", "pending", "unknown", "applied", "rejected"].includes(String(value.status))) {
    return false;
  }
  if (value.status === "idle") return value.original === null && value.rejectionCode === null;
  if (value.status === "rejected"
    ? typeof value.rejectionCode !== "string" || !SAFE_HANDOFF_REJECTIONS.has(value.rejectionCode)
    : value.rejectionCode !== null) return false;
  if (!isStoredCommand(value.original, action, scope, sourceReview, target, prepared, reason, instruction)) {
    return false;
  }
  return true;
}

function isStoredCommand(
  value: unknown,
  action: HandoffActionName,
  scope: HandoffScope,
  sourceReview: unknown,
  target: unknown,
  prepared: unknown,
  reason: string,
  instruction: string,
): value is CommandRequest {
  if (!isRecord(value) || !hasExactKeys(value, [
    "command_id", "workspace_id", "command_type", "actor", "target_type", "target_id",
    "expected_version", "idempotency_key", "correlation_id", "payload",
  ]) || value.workspace_id !== scope.workspace_id
    || !isUuid(value.command_id) || !isUuid(value.idempotency_key) || !isUuid(value.correlation_id)
    || !isRecord(value.actor) || !hasExactKeys(value.actor, ["type", "id"])
    || value.actor.type !== "user" || value.actor.id !== scope.principal_id
    || !isRecord(value.payload)) return false;

  if (action === "prepare") {
    if (sourceReview === null || target === null || !isStoredSourceReview(sourceReview, scope)
      || !isStoredTarget(target, scope)
      || value.command_type !== "PrepareProfileHandoff"
      || value.target_type !== "worker_item" || value.target_id !== scope.worker_item_id
      || value.expected_version !== sourceReview.worker_item_version
      || !hasExactKeys(value.payload, [
        "proposal_schema_version", "context", "target_assignee", "target_profile_ref",
        "handoff_reason", "handoff_instruction", "input_plan", "proposal_ttl_seconds",
      ]) || value.payload.proposal_schema_version !== 2
      || value.payload.handoff_reason !== reason || !nonblank(value.payload.handoff_reason)
      || value.payload.handoff_instruction !== instruction || !nonblank(value.payload.handoff_instruction)
      || value.payload.proposal_ttl_seconds !== 900
      || !isEmptyRecord(value.payload.input_plan)
      || !matchesAddress(value.payload.context, sourceReview)
      || !matchesTargetPayload(value.payload.target_assignee, value.payload.target_profile_ref, target)) {
      return false;
    }
    return true;
  }

  if (prepared === null || !isPreparedAnchor(prepared, scope, sourceReview, target)
    || prepared.proposal.status !== "prepared") return false;
  const anchor = prepared;
  if (value.command_type !== (action === "confirm" ? "ConfirmProfileHandoff" : "CancelWorkerHandoff")
    || value.target_type !== "handoff_proposal"
    || value.target_id !== anchor.proposal.id
    || value.expected_version !== anchor.proposal.version) return false;

  if (action === "confirm") {
    return hasExactKeys(value.payload, [
      "proposal_id", "expected_proposal_version", "expected_proposal_hash", "expected_worker_item_version",
    ]) && value.payload.proposal_id === anchor.proposal.id
      && value.payload.expected_proposal_version === anchor.proposal.version
      && value.payload.expected_proposal_hash === anchor.proposal.proposal_hash
      && value.payload.expected_worker_item_version === anchor.proposal.source_worker_item_version;
  }
  return hasExactKeys(value.payload, ["reason"]) && nonblank(value.payload.reason);
}

function isPreparedAnchor(
  value: unknown,
  scope: HandoffScope,
  sourceReview: unknown,
  target: unknown,
): value is HandoffPreparedAnchor {
  if (!isRecord(value) || !hasExactKeys(value, ["proposal", "source_review", "target"])
    || !isStoredSourceReview(sourceReview, scope)
    || !isStoredSourceReview(value.source_review, scope)
    || !sameSourceReview(value.source_review, sourceReview)
    || !isStoredTarget(target, scope)
    || !isStoredTarget(value.target, scope)
    || !sameTargetValue(value.target, target)
    || !isStoredNativeProposal(value.proposal)) return false;
  const proposal = value.proposal;
  return proposal.workspace_id === scope.workspace_id
    && proposal.worker_item_id === scope.worker_item_id
    && proposal.root_task_id === sourceReview.root_task_id
    && proposal.subtask_id === sourceReview.subtask_id
    && proposal.source_worker_item_version === sourceReview.worker_item_version
    && proposal.source_execution_id === sourceReview.execution_id
    && proposal.source_session_id === sourceReview.session_id
    && proposal.source_run_id === sourceReview.run_id
    && proposal.source_binding_version === sourceReview.binding_version
    && sameProfileRef(proposal.source_profile_ref, sourceReview.source_profile_ref)
    && proposal.context.target_context_version === sourceReview.context_version
    && proposal.plan_base.snapshot_id === sourceReview.plan_snapshot_id
    && proposal.plan_base.generation === sourceReview.plan_generation
    && matchesProposalTarget(proposal, target);
}

function isStoredNativeProposal(value: unknown): value is NativeHandoffProposal {
  return isNativeHandoffProposal(value)
    && hasExactKeys(value, [
      "id", "proposal_schema_version", "workspace_id", "root_task_id", "subtask_id",
      "worker_item_id", "source_worker_item_version", "version", "proposal_hash", "status",
      "stale_reason", "created_at", "expires_at", "confirmed_at", "cancelled_at",
      "source_execution_id", "source_session_id", "source_run_id", "source_binding_version",
      "source_profile_ref", "target_assignee", "target_profile_ref", "target_worker_id",
      "handoff_reason", "handoff_instruction", "context", "plan_base",
    ])
    && isStoredProfileRef(value.source_profile_ref, value.workspace_id)
    && isRecord(value.target_assignee)
    && hasExactKeys(value.target_assignee, ["kind", "id"])
    && (value.target_profile_ref === null
      || isStoredProfileRef(value.target_profile_ref, value.workspace_id))
    && isRecord(value.context)
    && hasExactKeys(value.context, [
      "source_package_id", "source_package_hash", "source_context_version",
      "target_package_id", "target_package_hash", "target_context_version",
    ])
    && isRecord(value.plan_base)
    && hasExactKeys(value.plan_base, ["snapshot_id", "generation", "head_version"]);
}

function isStoredSourceReview(value: unknown, scope: HandoffScope): value is HandoffSourceReview {
  return isRecord(value) && hasExactKeys(value, [
    "workspace_id", "principal_id", "worker_item_id", "root_task_id", "subtask_id",
    "worker_item_version", "execution_mode", "execution_id", "session_id", "session_version",
    "worker_id", "binding_version", "run_id", "plan_generation", "context_version",
    "source_profile_ref", "plan_snapshot_id",
    ...(Object.prototype.hasOwnProperty.call(value, "session_last_sequence") ? ["session_last_sequence"] : []),
  ]) && value.workspace_id === scope.workspace_id
    && value.principal_id === scope.principal_id
    && value.worker_item_id === scope.worker_item_id
    && nonblank(value.root_task_id) && nonblank(value.subtask_id)
    && positiveInteger(value.worker_item_version)
    && value.execution_mode === "native_profile"
    && nonblank(value.execution_id) && nonblank(value.session_id)
    && sessionObservationMatches(value.session_version, value.session_last_sequence,
      value.session_version, value.session_last_sequence)
    && (!Object.prototype.hasOwnProperty.call(value, "session_last_sequence")
      || typeof value.session_last_sequence === "number")
    && nonblank(value.worker_id)
    && positiveInteger(value.binding_version) && nonblank(value.run_id)
    && positiveInteger(value.plan_generation) && positiveInteger(value.context_version)
    && isStoredProfileRef(value.source_profile_ref, scope.workspace_id)
    && nonblank(value.plan_snapshot_id);
}

function isStoredTarget(value: unknown, scope: HandoffScope): value is AssigneeValue {
  if (!isRecord(value) || !nonblank(value.id)) return false;
  if (value.kind === "human") {
    return hasExactKeys(value, ["kind", "id", "profileRef"])
      && value.id === scope.principal_id && value.profileRef === null;
  }
  return value.kind === "profile" && hasExactKeys(value, ["kind", "id", "profileRef"])
    && isStoredProfileRef(value.profileRef, scope.workspace_id)
    && value.profileRef.profile_id === value.id;
}

function matchesAddress(value: unknown, review: HandoffSourceReview): boolean {
  return isRecord(value) && hasExactKeys(value, [
    "worker_item_id", "execution_mode", "execution_id", "session_id", "session_version",
    "worker_id", "binding_version", "run_id", "plan_generation", "context_version",
    ...(review.session_last_sequence === undefined ? [] : ["session_last_sequence"]),
  ]) && value.worker_item_id === review.worker_item_id
    && value.execution_mode === review.execution_mode
    && value.execution_id === review.execution_id
    && value.session_id === review.session_id
    && value.session_version === review.session_version
    && value.session_last_sequence === review.session_last_sequence
    && value.worker_id === review.worker_id
    && value.binding_version === review.binding_version
    && value.run_id === review.run_id
    && value.plan_generation === review.plan_generation
    && value.context_version === review.context_version;
}

function matchesTargetPayload(assignee: unknown, profileRef: unknown, target: AssigneeValue): boolean {
  if (!isRecord(assignee) || !hasExactKeys(assignee, ["kind", "id"])
    || assignee.kind !== target.kind || assignee.id !== target.id) return false;
  if (target.kind === "human") return profileRef === null;
  return isStoredProfileRef(profileRef, target.profileRef.workspace_id)
    && sameProfileRef(profileRef, target.profileRef);
}

function matchesProposalTarget(proposal: NativeHandoffProposal, target: AssigneeValue): boolean {
  if (proposal.target_assignee.kind !== target.kind || proposal.target_assignee.id !== target.id) return false;
  if (target.kind === "human") return target.id.length > 0 && proposal.target_profile_ref === null;
  return proposal.target_profile_ref !== null && sameProfileRef(proposal.target_profile_ref, target.profileRef);
}

function sameTargetValue(left: AssigneeValue, right: AssigneeValue): boolean {
  return left.kind === right.kind && left.id === right.id
    && (left.kind === "human" && right.kind === "human"
      || left.kind === "profile" && right.kind === "profile"
        && sameProfileRef(left.profileRef, right.profileRef));
}

function isEmptyRecord(value: unknown): boolean {
  return isRecord(value) && Object.keys(value).length === 0;
}

function isStoredProfileRef(value: unknown, workspaceId: string): value is ProfileRef {
  return isRecord(value) && hasExactKeys(value, ["workspace_id", "profile_id", "revision"])
    && value.workspace_id === workspaceId && nonblank(value.profile_id)
    && positiveInteger(value.revision);
}

function validHandoffScope(value: unknown): value is HandoffScope {
  return isRecord(value) && hasExactKeys(value, ["workspace_id", "principal_id", "worker_item_id"])
    && nonblank(value.workspace_id) && nonblank(value.principal_id) && nonblank(value.worker_item_id);
}

function sameHandoffScope(value: unknown, expected: HandoffScope): boolean {
  return validHandoffScope(value)
    && value.workspace_id === expected.workspace_id
    && value.principal_id === expected.principal_id
    && value.worker_item_id === expected.worker_item_id;
}

function hasExactKeys(value: Record<string, unknown>, expected: readonly string[]): boolean {
  const keys = Object.keys(value);
  return keys.length === expected.length && expected.every((key) => Object.prototype.hasOwnProperty.call(value, key));
}

function isUuid(value: unknown): value is string {
  return typeof value === "string"
    && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function sameTarget(
  proposal: NativeHandoffProposal,
  target: AssigneeValue,
  review: HandoffSourceReview,
): boolean {
  if (target.kind === "human") {
    return target.id === review.principal_id
      && target.profileRef === null
      && proposal.target_assignee.kind === "human"
      && proposal.target_assignee.id === target.id
      && proposal.target_profile_ref === null;
  }
  return target.profileRef.workspace_id === review.workspace_id
    && target.profileRef.profile_id === target.id
    && positiveInteger(target.profileRef.revision)
    && !(sameProfileRef(target.profileRef, review.source_profile_ref))
    && proposal.target_assignee.kind === "profile"
    && proposal.target_assignee.id === target.id
    && proposal.target_profile_ref !== null
    && sameProfileRef(proposal.target_profile_ref, target.profileRef);
}

function sameSourceReview(left: HandoffSourceReview, right: HandoffSourceReview): boolean {
  return left.workspace_id === right.workspace_id
    && left.principal_id === right.principal_id
    && left.worker_item_id === right.worker_item_id
    && left.root_task_id === right.root_task_id
    && left.subtask_id === right.subtask_id
    && left.worker_item_version === right.worker_item_version
    && left.execution_mode === right.execution_mode
    && left.execution_id === right.execution_id
    && left.session_id === right.session_id
    && left.session_version === right.session_version
    && left.session_last_sequence === right.session_last_sequence
    && left.worker_id === right.worker_id
    && left.binding_version === right.binding_version
    && left.run_id === right.run_id
    && left.plan_generation === right.plan_generation
    && left.context_version === right.context_version
    && left.plan_snapshot_id === right.plan_snapshot_id
    && sameProfileRef(left.source_profile_ref, right.source_profile_ref);
}

function cloneProposal(proposal: NativeHandoffProposal): NativeHandoffProposal {
  return {
    ...proposal,
    source_profile_ref: { ...proposal.source_profile_ref },
    target_assignee: { ...proposal.target_assignee },
    target_profile_ref: proposal.target_profile_ref === null
      ? null
      : { ...proposal.target_profile_ref },
    context: { ...proposal.context },
    plan_base: { ...proposal.plan_base },
  };
}

function cloneReview(review: HandoffSourceReview): HandoffSourceReview {
  return { ...review, source_profile_ref: { ...review.source_profile_ref } };
}

function cloneTarget(target: AssigneeValue): AssigneeValue {
  return target.kind === "human"
    ? { ...target }
    : { ...target, profileRef: { ...target.profileRef } };
}

function validProfileRef(value: ProfileRef | null, workspaceId: string): value is ProfileRef {
  return value !== null && value.workspace_id === workspaceId
    && nonblank(value.profile_id) && positiveInteger(value.revision);
}

function sameProfileRef(left: ProfileRef | null, right: ProfileRef | null): boolean {
  return left === null || right === null
    ? left === right
    : left.workspace_id === right.workspace_id
      && left.profile_id === right.profile_id
      && left.revision === right.revision;
}

function nonblank(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function positiveInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0;
}
