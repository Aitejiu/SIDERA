import type {
  AgentBindingView,
  AgentExecutionView,
  CommandRequest,
  CommandResponse,
  NativeWorkerContextView,
  ProfileRef,
} from "@/features/assignments/types";
import type { WorkerWorkspace } from "@/features/worker-items/types";
import { sessionObservationMatches } from "@/shared/sessionObservation";
import type { Execution, ExecutionSession } from "./types";

export type NativeInterventionContext = NonNullable<NativeWorkerContextView["context"]>;
export type NativeInputStatus =
  | "accepted"
  | "queued"
  | "dispatching"
  | "delivered"
  | "blocked"
  | "failed"
  | "cancelled"
  | "superseded";
export type NativeActionStatus = "idle" | "pending" | "unknown" | "applied" | "rejected";

export interface NativeInterventionScope {
  workspace_id: string;
  principal_id: string;
  worker_item_id: string;
}

export interface NativeInterventionReview {
  context: NativeInterventionContext;
  execution_version: number;
  profile_ref: ProfileRef;
  binding_version: number;
}

export interface NativeInterventionAction {
  original: CommandRequest | null;
  review: NativeInterventionReview | null;
  status: NativeActionStatus;
  aggregate_id: string | null;
  aggregate_version: number | null;
  rejection_code: string | null;
}

export interface NativeInterventionDraft {
  schema_version: 1;
  scope: NativeInterventionScope;
  text: string;
  client_idempotency_key: string | null;
  review: NativeInterventionReview | null;
  input: NativeInterventionAction;
  stop: NativeInterventionAction;
}

export interface NativeInterventionResultReview {
  status: "unknown" | "applied";
  aggregate_id: string | null;
  aggregate_version: number | null;
}

export interface NativeInterventionFacts {
  scope: NativeInterventionScope;
  workspace: WorkerWorkspace;
  nativeContext: NativeWorkerContextView | undefined;
  execution: Execution | undefined;
  session: ExecutionSession | undefined;
}

export type NativeInterventionFactsReview =
  | {
    ok: true;
    review: NativeInterventionReview;
    canInput: boolean;
    canStop: boolean;
    inputReason: string | null;
    stopReason: string | null;
  }
  | { ok: false; reason: string };

const STORAGE_FAILURE = "native_intervention_storage_unavailable";
const ACTION_STATUSES: readonly NativeActionStatus[] = ["idle", "pending", "unknown", "applied", "rejected"];
const SAFE_REJECTION_CODES = new Set([
  "assignment_forbidden",
  "execution_context_changed",
  "input_closed",
  "permission_denied",
  "session_not_active",
  "stop_not_supported",
  "validation_error",
  "version_conflict",
  "unknown",
]);
const CONTEXT_KEYS = [
  "worker_item_id",
  "plan_generation",
  "context_version",
  "execution_id",
  "session_id",
  "session_version",
  "worker_id",
  "execution_mode",
  "binding_version",
  "run_id",
] as const;
const SCOPE_KEYS = ["workspace_id", "principal_id", "worker_item_id"] as const;
const REVIEW_KEYS = ["context", "execution_version", "profile_ref", "binding_version"] as const;
const PROFILE_REF_KEYS = ["workspace_id", "profile_id", "revision"] as const;
const DRAFT_KEYS = [
  "schema_version",
  "scope",
  "text",
  "client_idempotency_key",
  "review",
  "input",
  "stop",
] as const;
const ACTION_KEYS = [
  "original",
  "review",
  "status",
  "aggregate_id",
  "aggregate_version",
  "rejection_code",
] as const;
const COMMAND_KEYS = [
  "actor",
  "command_id",
  "workspace_id",
  "command_type",
  "target_type",
  "target_id",
  "expected_version",
  "idempotency_key",
  "correlation_id",
  "payload",
] as const;

export function nativeContextFingerprint(
  context: NativeInterventionContext | null | undefined,
): string {
  if (context == null) return "";
  return JSON.stringify([
    context.worker_item_id,
    context.plan_generation,
    context.context_version,
    context.execution_id,
    context.session_id,
    context.session_version,
    context.worker_id,
    context.execution_mode,
    context.binding_version,
    context.run_id,
    ...(Object.prototype.hasOwnProperty.call(context, "session_last_sequence") ? [context.session_last_sequence] : []),
  ]);
}

export function reviewNativeInterventionFacts(
  facts: NativeInterventionFacts,
): NativeInterventionFactsReview {
  const { scope, workspace, nativeContext, execution, session } = facts;
  if (!isScope(scope) || workspace.workspace_id !== scope.workspace_id
    || workspace.worker_item.id !== scope.worker_item_id
    || nativeContext === undefined
    || nativeContext.workspace_id !== scope.workspace_id
    || nativeContext.worker_item_id !== scope.worker_item_id
    || nativeContext.execution_mode !== "native_profile"
    || nativeContext.context === null
    || nativeContext.execution === null
    || nativeContext.run === null
    || execution === undefined
    || session === undefined) {
    return { ok: false, reason: "native_source_unavailable" };
  }

  const address = nativeContext.context;
  const saved = nativeContext.execution;
  const run = nativeContext.run;
  const binding = nativeContext.binding;
  const runtime = workspace.current_runtime;
  const executionVersion = nativeContext.execution_version;
  const profileRef = saved.profile_ref;
  const completeObservationProof = address.session_last_sequence != null && session.last_sequence != null;

  if (executionVersion === null || executionVersion === undefined
    || !positiveInteger(executionVersion)
    || !positiveInteger(nativeContext.worker_item_version)
    || nativeContext.worker_item_version !== workspace.worker_item.version
    || !positiveInteger(workspace.worker_item.version)
    || !positiveInteger(workspace.worker_item.context_version)
    || !positiveInteger(workspace.worker_item.acknowledged_context_version)
    || !positiveInteger(workspace.current_placement.plan_generation)
    || !positiveInteger(execution.version)
    || execution.version !== executionVersion
    || execution.id !== address.execution_id
    || execution.workspace_id !== scope.workspace_id
    || execution.worker_item_id !== scope.worker_item_id
    || saved.execution_id !== address.execution_id
    || saved.workspace_id !== scope.workspace_id
    || saved.worker_item_id !== scope.worker_item_id
    || saved.root_task_id !== workspace.worker_item.root_task_id
    || saved.subtask_id !== workspace.current_placement.subtask_task_id
    || saved.run_id !== address.run_id
    || execution.task_id !== saved.subtask_id
    || address.worker_item_id !== scope.worker_item_id
    || address.plan_generation !== workspace.current_placement.plan_generation
    || address.context_version !== workspace.worker_item.context_version
    || workspace.worker_item.acknowledged_context_version !== address.context_version
    || address.session_id !== session.id
    || !sessionObservationMatches(address.session_version, address.session_last_sequence, session.version, session.last_sequence)
    || address.execution_id !== session.execution_id
    || address.worker_id !== session.worker_id
    || session.workspace_id !== scope.workspace_id
    || !positiveInteger(session.version)
    || runtime === null
    || runtime.adapter_type !== "agent-run"
    || runtime.execution_id !== address.execution_id
    || runtime.execution_status !== execution.status
    || runtime.session_id !== address.session_id
    || !positiveInteger(runtime.session_version)
    || (completeObservationProof ? runtime.session_version > session.version : runtime.session_version !== session.version)
    || runtime.session_status !== session.status
    || runtime.worker_id !== address.worker_id
    || runtime.task_id !== execution.task_id
    || !sameSavedNativePins(scope, address, saved, run, binding, profileRef)) {
    return { ok: false, reason: "native_source_mismatch" };
  }

  const currentContext = { ...address, session_version: session.version };
  if (!completeObservationProof) delete currentContext.session_last_sequence;
  else currentContext.session_last_sequence = session.last_sequence;
  const review: NativeInterventionReview = {
    context: currentContext,
    execution_version: executionVersion,
    profile_ref: profileRef,
    binding_version: address.binding_version,
  };
  const handoffActive = workspace.active_handoff != null
    && workspace.active_handoff.status !== "converged";
  const workerActive = workspace.worker_item.status === "active"
    && workspace.worker_item.context_version === address.context_version
    && workspace.worker_item.acknowledged_context_version === address.context_version
    && workspace.current_placement.plan_generation === address.plan_generation;
  const runtimeActive = session.status === "active" && runtime.session_status === "active";
  const runCanInput = run.input_open
    && (run.state === "running" || run.state === "idle")
    && (execution.status === "running" || execution.status === "idle")
    && !execution.stop_requested;
  const canInput = nativeContext.input_enabled
    && runtimeActive
    && workerActive
    && !handoffActive
    && runCanInput;
  const stopLeaseMatches = execution.active_lease_id !== null
    && execution.active_lease_id.trim().length > 0
    && runtime.lease_id === execution.active_lease_id;
  const canStop = runtimeActive
    && workerActive
    && !handoffActive
    && (run.state === "running" || run.state === "idle" || run.state === "unknown")
    && (execution.status === "leased" || execution.status === "running")
    && !execution.stop_requested
    && stopLeaseMatches;

  return {
    ok: true,
    review,
    canInput,
    canStop,
    inputReason: canInput ? null : inputReason({
      nativeContext,
      session,
      workerActive,
      handoffActive,
      runCanInput,
    }),
    stopReason: canStop ? null : stopReason({
      session,
      workerActive,
      handoffActive,
      execution,
      stopLeaseMatches,
      run,
    }),
  };
}

export function reviewNativeInputResult(
  original: CommandRequest,
  result: CommandResponse | null | undefined,
): NativeInterventionResultReview {
  if (result == null || !isRecord(result) || result.command_id !== original.command_id) {
    return { status: "unknown", aggregate_id: null, aggregate_version: null };
  }
  if (original.command_type !== "SendExecutionInput"
    || original.target_type !== "execution_session"
    || typeof original.target_id !== "string"
    || !isUuid(result.aggregate_id)
    || result.aggregate_id === original.target_id
    || !positiveInteger(result.aggregate_version)) {
    return { status: "applied", aggregate_id: null, aggregate_version: null };
  }
  return {
    status: "applied",
    aggregate_id: result.aggregate_id,
    aggregate_version: result.aggregate_version,
  };
}

export function reviewNativeStopResult(
  original: CommandRequest,
  review: NativeInterventionReview,
  result: CommandResponse | null | undefined,
): NativeInterventionResultReview {
  if (result == null || !isRecord(result) || result.command_id !== original.command_id) {
    return { status: "unknown", aggregate_id: null, aggregate_version: null };
  }
  if (original.command_type !== "StopExecution"
    || original.target_type !== "execution"
    || original.target_id !== review.context.execution_id
    || original.expected_version !== review.execution_version
    || !positiveInteger(review.execution_version)
    || result.aggregate_id !== review.context.execution_id
    || result.aggregate_version !== review.execution_version + 1) {
    return { status: "applied", aggregate_id: null, aggregate_version: null };
  }
  return {
    status: "applied",
    aggregate_id: result.aggregate_id,
    aggregate_version: result.aggregate_version,
  };
}

export function nativeOriginalStatusMatches(original: CommandRequest, status: unknown): boolean {
  try {
    if (!isRecord(status)
      || status.command_id !== original.command_id
      || status.command_type !== original.command_type
      || status.target_type !== original.target_type
      || status.target_id !== original.target_id
      || status.expected_version !== original.expected_version
      || (status.status !== "pending" && status.status !== "applied" && status.status !== "rejected")) {
      return false;
    }
    if (status.result !== undefined && status.result !== null
      && (!isRecord(status.result) || status.result.command_id !== original.command_id)) {
      return false;
    }
    return true;
  } catch {
    return false;
  }
}

export function nativeInterventionDraftKey(scope: NativeInterventionScope): string {
  return `sidera.native-intervention.v1:${encodeURIComponent(scope.workspace_id)}:${encodeURIComponent(scope.principal_id)}:${encodeURIComponent(scope.worker_item_id)}`;
}

export function loadNativeInterventionDraft(
  scope: NativeInterventionScope,
): { ok: true; draft: NativeInterventionDraft | null } | { ok: false; reason: string } {
  try {
    if (!isScope(scope)) return { ok: false, reason: STORAGE_FAILURE };
    const raw = localStorage.getItem(nativeInterventionDraftKey(scope));
    if (raw === null) return { ok: true, draft: null };
    const value: unknown = JSON.parse(raw);
    if (!isNativeInterventionDraft(value, scope)) {
      return { ok: false, reason: STORAGE_FAILURE };
    }
    return { ok: true, draft: value };
  } catch {
    return { ok: false, reason: STORAGE_FAILURE };
  }
}

export function saveNativeInterventionDraft(
  scope: NativeInterventionScope,
  draft: NativeInterventionDraft,
): { ok: true } | { ok: false; reason: string } {
  try {
    if (!isScope(scope) || !isNativeInterventionDraft(draft, scope)) {
      return { ok: false, reason: STORAGE_FAILURE };
    }
    const serialized = JSON.stringify(draft);
    if (serialized === undefined) return { ok: false, reason: STORAGE_FAILURE };
    localStorage.setItem(nativeInterventionDraftKey(scope), serialized);
    return { ok: true };
  } catch {
    return { ok: false, reason: STORAGE_FAILURE };
  }
}

function sameSavedNativePins(
  scope: NativeInterventionScope,
  address: NativeInterventionContext,
  saved: AgentExecutionView,
  run: NonNullable<NativeWorkerContextView["run"]>,
  binding: AgentBindingView | null,
  profileRef: ProfileRef | null,
): profileRef is ProfileRef {
  return profileRef !== null
    && validProfileRef(profileRef, scope.workspace_id)
    && sameProfileRef(run.profile_ref, profileRef)
    && run.runtime_ref.workspace_id === scope.workspace_id
    && saved.binding_version === address.binding_version
    && positiveInteger(saved.binding_version)
    && binding !== null
    && binding.workspace_id === scope.workspace_id
    && binding.target_type === "worker_item"
    && binding.target_id === scope.worker_item_id
    && binding.version === address.binding_version
    && sameProfileRef(binding.profile_ref, profileRef)
    && binding.assignee.kind === "profile"
    && binding.assignee.id === profileRef.profile_id
    && address.execution_mode === "native_profile"
    && positiveInteger(address.plan_generation)
    && positiveInteger(address.context_version)
    && positiveInteger(address.session_version)
    && positiveInteger(address.binding_version)
    && isUuid(address.worker_item_id)
    && isUuid(address.execution_id)
    && isUuid(address.session_id)
    && isUuid(address.worker_id)
    && isUuid(address.run_id);
}

function inputReason(input: {
  nativeContext: NativeWorkerContextView;
  session: ExecutionSession;
  workerActive: boolean;
  handoffActive: boolean;
  runCanInput: boolean;
}): string {
  if (input.session.status !== "active") return "session_closed";
  if (input.handoffActive) return "handoff_in_progress";
  if (!input.workerActive) return "worker_item_unavailable";
  if (input.nativeContext.read_only_reason === "unsupported_input"
    || input.nativeContext.read_only_reason === "input_unsupported"
    || input.nativeContext.read_only_reason === "adapter_unsupported") {
    return "input_unsupported";
  }
  if (!input.nativeContext.input_enabled) return "input_unavailable";
  if (!input.runCanInput) {
    return input.nativeContext.run?.input_open === false ? "input_closed" : "run_not_active";
  }
  return "input_unavailable";
}

function stopReason(input: {
  session: ExecutionSession;
  workerActive: boolean;
  handoffActive: boolean;
  execution: Execution;
  stopLeaseMatches: boolean;
  run: NonNullable<NativeWorkerContextView["run"]>;
}): string {
  if (input.session.status !== "active") return "session_closed";
  if (input.handoffActive) return "handoff_in_progress";
  if (!input.workerActive) return "worker_item_unavailable";
  if (input.execution.stop_requested) return "stop_already_requested";
  if (input.execution.status !== "leased" && input.execution.status !== "running") {
    return "execution_not_stoppable";
  }
  if (input.run.state !== "running" && input.run.state !== "idle"
    && input.run.state !== "unknown") return "run_not_active";
  if (!input.stopLeaseMatches) return "lease_unavailable";
  return "stop_unavailable";
}

function isNativeInterventionDraft(value: unknown, scope: NativeInterventionScope): value is NativeInterventionDraft {
  if (!isRecord(value) || !hasExactKeys(value, DRAFT_KEYS)
    || value.schema_version !== 1
    || !isExactScope(value.scope, scope)
    || typeof value.text !== "string"
    || !(value.client_idempotency_key === null || isUuid(value.client_idempotency_key))
    || !(value.review === null || isReview(value.review, scope))
    || !isAction(value.input, "input", scope)
    || !isAction(value.stop, "stop", scope)) {
    return false;
  }
  if (value.input.original !== null
    && (value.input.status === "pending" || value.input.status === "unknown")) {
    const payload = value.input.original.payload;
    if (!isRecord(payload) || value.client_idempotency_key !== payload.client_idempotency_key) {
      return false;
    }
  }
  return true;
}

function isAction(
  value: unknown,
  kind: "input" | "stop",
  scope: NativeInterventionScope,
): value is NativeInterventionAction {
  if (!isRecord(value) || !hasExactKeys(value, ACTION_KEYS)
    || !isActionStatus(value.status)
    || !(value.aggregate_id === null || isUuid(value.aggregate_id))
    || !(value.aggregate_version === null || positiveInteger(value.aggregate_version))
    || !(value.rejection_code === null
      || (typeof value.rejection_code === "string"
        && SAFE_REJECTION_CODES.has(value.rejection_code)))) {
    return false;
  }
  if (value.status === "idle") {
    return value.original === null
      && value.review === null
      && value.aggregate_id === null
      && value.aggregate_version === null
      && value.rejection_code === null;
  }
  if (value.original === null || value.review === null || !isReview(value.review, scope)
    || !isCommandRequest(value.original, kind, scope, value.review)) {
    return false;
  }
  const hasAggregateId = value.aggregate_id !== null;
  const hasAggregateVersion = value.aggregate_version !== null;
  if (hasAggregateId !== hasAggregateVersion) return false;
  if (value.status === "pending" || value.status === "unknown") {
    return !hasAggregateId && value.rejection_code === null;
  }
  if (value.status === "rejected") {
    return !hasAggregateId && value.rejection_code !== null;
  }
  if (value.rejection_code !== null) return false;
  if (!hasAggregateId) return true;
  if (kind === "input") {
    return value.aggregate_id !== value.original.target_id;
  }
  return value.aggregate_id === value.review.context.execution_id
    && value.aggregate_version === value.review.execution_version + 1;
}

function isCommandRequest(
  value: unknown,
  kind: "input" | "stop",
  scope: NativeInterventionScope,
  review: NativeInterventionReview,
): value is CommandRequest {
  if (!isRecord(value) || !hasExactKeys(value, COMMAND_KEYS)
    || !isRecord(value.actor) || !hasExactKeys(value.actor, ["type", "id"])
    || value.actor.type !== "user"
    || value.actor.id !== scope.principal_id
    || !isUuid(value.command_id)
    || value.workspace_id !== scope.workspace_id
    || !isUuid(value.idempotency_key)
    || !isUuid(value.correlation_id)
    || value.expected_version === null
    || !positiveInteger(value.expected_version)
    || typeof value.target_id !== "string"
    || !isUuid(value.target_id)
    || !isRecord(value.payload)) {
    return false;
  }
  if (kind === "input") {
    return value.command_type === "SendExecutionInput"
      && value.target_type === "execution_session"
      && value.target_id === review.context.session_id
      && value.expected_version === review.context.session_version
      && hasExactKeys(value.payload, ["context", "client_idempotency_key", "text", "attachment_refs"])
      && isNativeContext(value.payload.context)
      && sameContext(value.payload.context, review.context)
      && isUuid(value.payload.client_idempotency_key)
      && typeof value.payload.text === "string"
      && value.payload.text.trim().length > 0
      && Array.isArray(value.payload.attachment_refs)
      && value.payload.attachment_refs.length === 0;
  }
  return value.command_type === "StopExecution"
    && value.target_type === "execution"
    && value.target_id === review.context.execution_id
    && value.expected_version === review.execution_version
    && hasExactKeys(value.payload, []);
}

function isReview(value: unknown, scope: NativeInterventionScope): value is NativeInterventionReview {
  if (!isRecord(value) || !hasExactKeys(value, REVIEW_KEYS)
    || !isNativeContext(value.context)
    || value.context.worker_item_id !== scope.worker_item_id
    || !positiveInteger(value.execution_version)
    || !isProfileRef(value.profile_ref)
    || value.profile_ref.workspace_id !== scope.workspace_id
    || !positiveInteger(value.binding_version)
    || value.context.binding_version !== value.binding_version) {
    return false;
  }
  return true;
}

function isNativeContext(value: unknown): value is NativeInterventionContext {
  if (!isRecord(value)) return false;
  const hasProof = Object.prototype.hasOwnProperty.call(value, "session_last_sequence");
  if (!hasExactKeys(value, hasProof ? [...CONTEXT_KEYS, "session_last_sequence"] : CONTEXT_KEYS)) return false;
  return isUuid(value.worker_item_id)
    && positiveInteger(value.plan_generation)
    && positiveInteger(value.context_version)
    && isUuid(value.execution_id)
    && isUuid(value.session_id)
    && positiveInteger(value.session_version)
    && (!hasProof || (typeof value.session_last_sequence === "number"
      && Number.isSafeInteger(value.session_last_sequence)
      && value.session_last_sequence >= 0
      && value.session_last_sequence < value.session_version))
    && isUuid(value.worker_id)
    && value.execution_mode === "native_profile"
    && positiveInteger(value.binding_version)
    && isUuid(value.run_id);
}

function isProfileRef(value: unknown): value is ProfileRef {
  return isRecord(value)
    && hasExactKeys(value, PROFILE_REF_KEYS)
    && isUuid(value.workspace_id)
    && isUuid(value.profile_id)
    && positiveInteger(value.revision);
}

function isExactScope(value: unknown, expected: NativeInterventionScope): value is NativeInterventionScope {
  return isScope(value)
    && value.workspace_id === expected.workspace_id
    && value.principal_id === expected.principal_id
    && value.worker_item_id === expected.worker_item_id;
}

function isScope(value: unknown): value is NativeInterventionScope {
  return isRecord(value)
    && hasExactKeys(value, SCOPE_KEYS)
    && isUuid(value.workspace_id)
    && typeof value.principal_id === "string"
    && value.principal_id.trim().length > 0
    && isUuid(value.worker_item_id);
}

function isActionStatus(value: unknown): value is NativeActionStatus {
  return typeof value === "string" && ACTION_STATUSES.includes(value as NativeActionStatus);
}

function sameContext(left: NativeInterventionContext, right: NativeInterventionContext): boolean {
  return nativeContextFingerprint(left) === nativeContextFingerprint(right);
}

function sameProfileRef(left: ProfileRef | null, right: ProfileRef): boolean {
  return left !== null
    && left.workspace_id === right.workspace_id
    && left.profile_id === right.profile_id
    && left.revision === right.revision;
}

function validProfileRef(value: ProfileRef, workspaceId: string): boolean {
  return value.workspace_id === workspaceId
    && isUuid(value.profile_id)
    && positiveInteger(value.revision);
}

function hasExactKeys(value: Record<string, unknown>, expected: readonly string[]): boolean {
  const keys = Object.keys(value);
  return keys.length === expected.length
    && expected.every((key) => Object.prototype.hasOwnProperty.call(value, key));
}

function isUuid(value: unknown): value is string {
  return typeof value === "string"
    && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);
}

function positiveInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
