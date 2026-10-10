import type { TaskView } from "@/features/tasks/types";
import type { WorkerWorkspace } from "@/features/worker-items/types";
import { sessionObservationMatches } from "@/shared/sessionObservation";
import type {
  AgentBindingView,
  AgentExecutionView,
  AssigneeValue,
  CommandRequest,
  NativeWorkerContextView,
  ProfileRef,
} from "./types";

export interface AppointmentScope {
  workspace_id: string;
  principal_id: string;
  target_kind: "subtask" | "worker_item";
  target_id: string;
}

export interface AppointmentReview {
  aggregateVersion: number;
  bindingFingerprint: string;
  executionFingerprint: string;
}

export interface AppointmentDraft {
  schema_version: 1;
  scope: AppointmentScope;
  review: AppointmentReview | null;
  choice: AssigneeValue | null;
  original: CommandRequest | null;
  status: "idle" | "pending" | "unknown" | "applied" | "rejected";
  rejectionCode: string | null;
}

export interface AppointmentFacts {
  scope: AppointmentScope;
  binding: AgentBindingView | null;
  task?: TaskView;
  workerWorkspace?: WorkerWorkspace;
  nativeContext?: NativeWorkerContextView;
}

export type AppointmentReviewResult =
  | { ok: true; review: AppointmentReview; action: "appointment" | "handoff_required" }
  | { ok: false; action: "legacy" | "unavailable"; reason: string };

const STORAGE_PREFIX = "sidera.native-assignment.appointment.v1";
const STORAGE_UNAVAILABLE = "本地归属记录暂不可用";
const SAFE_REJECTION_CODES = new Set([
  "version_conflict",
  "handoff_required",
  "assignment_forbidden",
  "validation_error",
  "unknown",
]);
const MUTABLE_TASK_STATUSES = new Set([
  "pending",
  "blocked",
  "ready",
  "running",
  "waiting",
  "paused",
]);
const MUTABLE_WORKER_ITEM_STATUSES = new Set(["active", "paused"]);
const TERMINAL_LINK_STATES = new Set(["succeeded", "failed", "stopped", "cancelled"]);

export function appointmentDraftKey(scope: AppointmentScope): string {
  return `${STORAGE_PREFIX}/${encodeURIComponent(scope.workspace_id)}/${encodeURIComponent(scope.principal_id)}/${encodeURIComponent(scope.target_kind)}/${encodeURIComponent(scope.target_id)}`;
}

export function loadAppointmentDraft(
  scope: AppointmentScope,
): { ok: true; draft: AppointmentDraft | null } | { ok: false; reason: string } {
  if (!validScope(scope) || typeof localStorage === "undefined") {
    return { ok: false, reason: STORAGE_UNAVAILABLE };
  }
  try {
    const raw = localStorage.getItem(appointmentDraftKey(scope));
    if (raw === null) return { ok: true, draft: null };
    let value: unknown;
    try {
      value = JSON.parse(raw);
    } catch {
      return { ok: false, reason: STORAGE_UNAVAILABLE };
    }
    if (!isAppointmentDraft(value, scope)) return { ok: false, reason: STORAGE_UNAVAILABLE };
    return { ok: true, draft: value };
  } catch {
    return { ok: false, reason: STORAGE_UNAVAILABLE };
  }
}

export function saveAppointmentDraft(
  scope: AppointmentScope,
  draft: AppointmentDraft,
): { ok: true } | { ok: false; reason: string } {
  if (!validScope(scope) || !isAppointmentDraft(draft, scope) || typeof localStorage === "undefined") {
    return { ok: false, reason: STORAGE_UNAVAILABLE };
  }
  try {
    localStorage.setItem(appointmentDraftKey(scope), JSON.stringify(draft));
    return { ok: true };
  } catch {
    return { ok: false, reason: STORAGE_UNAVAILABLE };
  }
}

export function reviewAppointmentFacts(facts: AppointmentFacts): AppointmentReviewResult {
  const { scope, binding } = facts;
  if (!validScope(scope) || !Object.prototype.hasOwnProperty.call(facts, "binding")
    || binding === undefined || !validBinding(binding, scope)) {
    return unavailable("当前归属事实暂不可用");
  }

  if (scope.target_kind === "subtask") {
    const task = facts.task;
    if (task === undefined || task.id !== scope.target_id || task.workspace_id !== scope.workspace_id
      || !positiveInteger(task.version) || !MUTABLE_TASK_STATUSES.has(task.status)) {
      return unavailable("子任务当前不可更改归属");
    }
    return {
      ok: true,
      action: "appointment",
      review: {
        aggregateVersion: task.version,
        bindingFingerprint: bindingFingerprint(binding),
        executionFingerprint: "",
      },
    };
  }

  const workerWorkspace = facts.workerWorkspace;
  const nativeContext = facts.nativeContext;
  if (workerWorkspace === undefined || nativeContext === undefined
    || workerWorkspace.workspace_id !== scope.workspace_id
    || workerWorkspace.worker_item.id !== scope.target_id
    || !positiveInteger(workerWorkspace.worker_item.version)
    || nativeContext.workspace_id !== scope.workspace_id
    || nativeContext.worker_item_id !== scope.target_id
    || !positiveInteger(nativeContext.worker_item_version)
    || nativeContext.worker_item_version !== workerWorkspace.worker_item.version
    || !validBinding(nativeContext.binding, scope)
    || bindingFingerprint(nativeContext.binding) !== bindingFingerprint(binding)) {
    return unavailable("WorkerItem 归属事实尚未对齐");
  }

  const runtime = workerWorkspace.current_runtime;
  const adapterType = runtime?.adapter_type;
  const hasLegacyAdapter = typeof adapterType === "string"
    && adapterType.length > 0 && adapterType !== "agent-run";
  const hasNativeEvidence = adapterType === "agent-run"
    || nativeContext.execution_mode === "native_profile"
    || nativeContext.context !== null
    || nativeContext.run !== null
    || nativeContext.execution?.profile_ref !== null && nativeContext.execution !== null;
  const explicitLegacy = nativeContext.execution_mode === "legacy";
  if (hasLegacyAdapter || explicitLegacy) {
    if (hasNativeEvidence) return unavailable("执行归属事实互相冲突");
    return { ok: false, action: "legacy", reason: "此 WorkerItem 使用现有执行路由" };
  }

  if (!MUTABLE_WORKER_ITEM_STATUSES.has(workerWorkspace.worker_item.status)) {
    return unavailable("WorkerItem 当前状态不可更改归属");
  }

  if (nativeContext.execution_mode !== "native_profile" && nativeContext.execution_mode !== "unassigned") {
    return unavailable("执行模式尚未确认");
  }
  if (adapterType !== null && adapterType !== undefined && adapterType !== "agent-run") {
    return unavailable("当前执行与原始归属事实不一致");
  }

  const execution = nativeContext.execution;
  if (execution === null) {
    if (runtime !== null || nativeContext.context !== null || nativeContext.run !== null
      || nativeContext.execution_version !== null && nativeContext.execution_version !== undefined) {
      return unavailable("原始执行记录不完整");
    }
    const hasProfileBinding = binding !== null && binding.profile_ref !== null;
    if ((nativeContext.execution_mode === "native_profile") !== hasProfileBinding) {
      return unavailable("当前归属与执行模式不一致");
    }
    return {
      ok: true,
      action: "appointment",
      review: workerReview(workerWorkspace, nativeContext, binding, workerWorkspace.worker_item.version),
    };
  }

  if (!executionMatchesScope(execution, scope, workerWorkspace)
    || !positiveInteger(nativeContext.execution_version)
    || !workspaceExecutionMatches(workerWorkspace, execution, nativeContext.execution_version)) {
    return unavailable("WorkerItem 执行版本尚未对齐");
  }

  if (nativeContext.read_only_reason === "run_not_admitted"
    && execution.run_id === null && execution.event_cursor === 0
    && nativeContext.context === null && nativeContext.run === null
    && nativeContext.execution_mode === "native_profile"
    && binding !== null && binding.profile_ref !== null
    && sameProfileRef(execution.profile_ref, binding.profile_ref)
    && execution.binding_version === binding.version) {
    return {
      ok: true,
      action: "appointment",
      review: workerReview(workerWorkspace, nativeContext, binding, workerWorkspace.worker_item.version),
    };
  }

  if (nativeContext.execution_mode !== "native_profile"
    || execution.run_id === null || nativeContext.context === null || nativeContext.run === null
    || !nativePinsMatch(workerWorkspace, nativeContext, execution, binding)) {
    return unavailable("原始 native 执行事实尚未确认");
  }

  if (isCoherentlyTerminal(execution, nativeContext)) {
    return {
      ok: true,
      action: "appointment",
      review: workerReview(workerWorkspace, nativeContext, binding, workerWorkspace.worker_item.version),
    };
  }

  if ((nativeContext.run.state === "running" || nativeContext.run.state === "idle")
    && (execution.status === "running" || execution.status === "idle")
    && !TERMINAL_LINK_STATES.has(execution.link_status ?? "")) {
    if (binding === null || binding.profile_ref === null
      || binding.version !== execution.binding_version
      || !sameProfileRef(binding.profile_ref, execution.profile_ref)
      || !activeWorkerFactsMatch(workerWorkspace, nativeContext)) {
      return unavailable("当前 native 执行上下文尚未对齐");
    }
    return {
      ok: true,
      action: "handoff_required",
      review: workerReview(workerWorkspace, nativeContext, binding, workerWorkspace.worker_item.version, true),
    };
  }

  return unavailable("native 执行状态尚未确认");
}

function workerReview(
  workspace: WorkerWorkspace,
  context: NativeWorkerContextView,
  binding: AgentBindingView | null,
  aggregateVersion: number,
  activeNativeHandoffEntry = false,
): AppointmentReview {
  return {
    aggregateVersion,
    bindingFingerprint: bindingFingerprint(binding),
    executionFingerprint: executionFingerprint(workspace, context, activeNativeHandoffEntry),
  };
}

function bindingFingerprint(binding: AgentBindingView | null): string {
  if (binding === null) return "null";
  return JSON.stringify([
    binding.workspace_id,
    binding.target_type,
    binding.target_id,
    binding.version,
    binding.assignee.kind,
    binding.assignee.id,
    profileRefTuple(binding.profile_ref),
  ]);
}

function executionFingerprint(
  workspace: WorkerWorkspace,
  context: NativeWorkerContextView,
  activeNativeHandoffEntry: boolean,
): string {
  const runtime = workspace.current_runtime;
  const saved = context.execution;
  const address = context.context;
  const run = context.run;
  // Entry navigation tracks the control baseline; commands review ordered proof.
  const traceEntry = activeNativeHandoffEntry && address?.session_last_sequence != null;
  const summary = saved === null
    ? null
    : workspace.execution_history.find((item) => item.execution_id === saved.execution_id) ?? null;
  return JSON.stringify([
    workspace.worker_item.version,
    workspace.worker_item.context_version,
    workspace.worker_item.acknowledged_context_version,
    workspace.current_placement.plan_generation,
    runtime === null ? null : [
      runtime.adapter_type,
      runtime.execution_id,
      runtime.execution_status,
      runtime.session_id,
      runtime.session_status,
      traceEntry ? null : runtime.session_version,
      runtime.task_id,
      runtime.worker_id,
    ],
    summary === null ? null : [
      summary.execution_id,
      summary.version,
      summary.status,
      summary.task_id,
      summary.session_id,
      summary.worker_id,
    ],
    saved === null ? null : [
      saved.execution_id,
      saved.status,
      saved.link_status,
      saved.run_id,
      traceEntry ? null : saved.event_cursor,
      saved.binding_version,
      profileRefTuple(saved.profile_ref),
      context.execution_version ?? null,
    ],
    address === null ? null : [
      address.worker_item_id,
      address.plan_generation,
      address.context_version,
      address.execution_id,
      address.session_id,
      traceEntry ? address.session_version - address.session_last_sequence! : address.session_version,
      address.worker_id,
      address.binding_version,
      address.run_id,
    ],
    run === null ? null : [
      run.state,
      run.input_open,
      profileRefTuple(run.profile_ref),
      run.runtime_ref.workspace_id,
      run.runtime_ref.runtime_id,
    ],
  ]);
}

function profileRefTuple(ref: ProfileRef | null): readonly unknown[] | null {
  return ref === null ? null : [ref.workspace_id, ref.profile_id, ref.revision];
}

function isCoherentlyTerminal(
  execution: AgentExecutionView,
  context: NativeWorkerContextView,
): boolean {
  if (context.run === null || context.context === null) return false;
  if (execution.status === "succeeded") {
    return execution.link_status === "succeeded" && context.run.state === "completed";
  }
  if (execution.status === "failed") {
    return execution.link_status === "failed" && context.run.state === "failed";
  }
  if (execution.status === "canceled") {
    return (execution.link_status === "stopped" || execution.link_status === "cancelled")
      && context.run.state === "stopped";
  }
  return false;
}

function nativePinsMatch(
  workspace: WorkerWorkspace,
  context: NativeWorkerContextView,
  execution: AgentExecutionView,
  binding: AgentBindingView | null,
): boolean {
  const address = context.context;
  const run = context.run;
  if (address === null || run === null || execution.profile_ref === null
    || !validProfileRef(execution.profile_ref, workspace.workspace_id)
    || !validProfileRef(run.profile_ref, workspace.workspace_id)
    || !sameProfileRef(execution.profile_ref, run.profile_ref)
    || !positiveInteger(execution.binding_version)
    || address.worker_item_id !== workspace.worker_item.id
    || address.execution_id !== execution.execution_id
    || address.run_id !== execution.run_id
    || address.binding_version !== execution.binding_version
    || address.plan_generation !== workspace.current_placement.plan_generation
    || address.context_version !== workspace.worker_item.context_version
    || execution.run_id === null) {
    return false;
  }
  if (binding?.profile_ref === null || binding === null
    || binding.version < execution.binding_version) {
    return false;
  }
  return true;
}

function activeWorkerFactsMatch(
  workspace: WorkerWorkspace,
  context: NativeWorkerContextView,
): boolean {
  const runtime = workspace.current_runtime;
  const address = context.context;
  const summary = context.execution === null
    ? undefined
    : workspace.execution_history.find((item) => item.execution_id === context.execution?.execution_id);
  if (runtime === null || address === null || context.execution === null || summary === undefined
    || runtime.adapter_type !== "agent-run"
    || runtime.execution_id !== address.execution_id
    || runtime.session_id !== address.session_id
    || !sessionObservationMatches(address.session_version, address.session_last_sequence,
      address.session_version, address.session_last_sequence)
    || !positiveInteger(runtime.session_version)
    || (address.session_last_sequence == null
      ? runtime.session_version !== address.session_version
      : runtime.session_version > address.session_version)
    || runtime.worker_id !== address.worker_id
    || runtime.task_id !== workspace.current_placement.subtask_task_id
    || summary.version !== context.execution_version
    || summary.session_id !== address.session_id
    || summary.worker_id !== address.worker_id
    || summary.task_id !== context.execution.subtask_id) {
    return false;
  }
  return true;
}

function executionMatchesScope(
  execution: AgentExecutionView,
  scope: AppointmentScope,
  workspace: WorkerWorkspace,
): boolean {
  return execution.workspace_id === scope.workspace_id
    && execution.worker_item_id === scope.target_id
    && execution.subtask_id === workspace.current_placement.subtask_task_id
    && execution.root_task_id === workspace.worker_item.root_task_id
    && execution.execution_id.length > 0;
}

function workspaceExecutionMatches(
  workspace: WorkerWorkspace,
  execution: AgentExecutionView,
  version: number,
): boolean {
  const summary = workspace.execution_history.find((item) => item.execution_id === execution.execution_id);
  if (summary === undefined || summary.version !== version || summary.task_id !== execution.subtask_id
    || summary.status !== execution.status) {
    return false;
  }
  const runtime = workspace.current_runtime;
  return runtime === null || runtime.execution_id === execution.execution_id;
}

function validBinding(binding: AgentBindingView | null | undefined, scope: AppointmentScope): boolean {
  if (binding === null) return true;
  if (binding === undefined || binding.workspace_id !== scope.workspace_id
    || binding.target_type !== scope.target_kind || binding.target_id !== scope.target_id
    || !positiveInteger(binding.version) || !isRecord(binding.assignee)
    || typeof binding.assignee.id !== "string" || binding.assignee.id.trim().length === 0) {
    return false;
  }
  if (binding.assignee.kind === "human") return binding.profile_ref === null;
  return binding.assignee.kind === "profile" && binding.profile_ref !== null
    && validProfileRef(binding.profile_ref, scope.workspace_id)
    && binding.profile_ref.profile_id === binding.assignee.id;
}

function validProfileRef(ref: ProfileRef | null, workspaceId: string): ref is ProfileRef {
  return isRecord(ref) && ref.workspace_id === workspaceId
    && typeof ref.profile_id === "string" && ref.profile_id.trim().length > 0
    && positiveInteger(ref.revision);
}

function sameProfileRef(left: ProfileRef | null, right: ProfileRef | null): boolean {
  return left === null || right === null
    ? left === right
    : left.workspace_id === right.workspace_id
      && left.profile_id === right.profile_id
      && left.revision === right.revision;
}

function validScope(scope: AppointmentScope): boolean {
  return isRecord(scope)
    && typeof scope.workspace_id === "string" && scope.workspace_id.trim().length > 0
    && typeof scope.principal_id === "string" && scope.principal_id.trim().length > 0
    && (scope.target_kind === "subtask" || scope.target_kind === "worker_item")
    && typeof scope.target_id === "string" && scope.target_id.trim().length > 0;
}

function isAppointmentDraft(value: unknown, scope: AppointmentScope): value is AppointmentDraft {
  if (!isRecord(value) || !hasExactKeys(value, [
    "schema_version", "scope", "review", "choice", "original", "status", "rejectionCode",
  ]) || value.schema_version !== 1 || !sameScope(value.scope, scope)
    || !["idle", "pending", "unknown", "applied", "rejected"].includes(String(value.status))) {
    return false;
  }
  const review = value.review;
  if (review !== null && (!isRecord(review) || !hasExactKeys(review, [
    "aggregateVersion", "bindingFingerprint", "executionFingerprint",
  ]) || !positiveInteger(review.aggregateVersion)
    || typeof review.bindingFingerprint !== "string" || review.bindingFingerprint.length === 0
    || typeof review.executionFingerprint !== "string")) {
    return false;
  }
  if (value.choice !== null && !validStoredChoice(value.choice, scope)) return false;

  const original = value.original;
  const status = value.status;
  const rejectionCode = value.rejectionCode;
  if (status === "idle") {
    return original === null && rejectionCode === null;
  }
  if (!isCommandRequest(original, scope, review)) return false;
  if (status === "rejected" ? !isSafeRejection(rejectionCode) : rejectionCode !== null) {
    return false;
  }
  return true;
}

function validStoredChoice(value: unknown, scope: AppointmentScope): value is AssigneeValue {
  if (!isRecord(value) || typeof value.id !== "string" || value.id.trim().length === 0) return false;
  if (value.kind === "human") {
    return hasExactKeys(value, ["kind", "id", "profileRef"]) && value.profileRef === null;
  }
  if (value.kind !== "profile" || !hasExactKeys(value, ["kind", "id", "profileRef"])) return false;
  return isRecord(value.profileRef)
    && hasExactKeys(value.profileRef, ["workspace_id", "profile_id", "revision"])
    && value.profileRef.workspace_id === scope.workspace_id
    && value.profileRef.profile_id === value.id
    && positiveInteger(value.profileRef.revision);
}

function isCommandRequest(
  value: unknown,
  scope: AppointmentScope,
  review: unknown,
): value is CommandRequest {
  if (!isRecord(value) || !hasExactKeys(value, [
    "command_id", "workspace_id", "command_type", "actor", "target_type", "target_id",
    "expected_version", "idempotency_key", "correlation_id", "payload",
  ]) || review === null || !isRecord(review)) {
    return false;
  }
  const commandType = scope.target_kind === "subtask" ? "SetSubtaskAssignee" : "SetWorkerItemAssignee";
  const targetType = scope.target_kind === "subtask" ? "task" : "worker_item";
  if (value.workspace_id !== scope.workspace_id || value.command_type !== commandType
    || value.target_type !== targetType || value.target_id !== scope.target_id
    || value.expected_version !== review.aggregateVersion
    || !isUuid(value.command_id) || !isUuid(value.idempotency_key) || !isUuid(value.correlation_id)
    || !isRecord(value.actor) || !hasExactKeys(value.actor, ["type", "id"])
    || value.actor.type !== "user" || value.actor.id !== scope.principal_id
    || !isRecord(value.payload) || !hasExactKeys(value.payload, [
      "assignee_kind", "assignee_id", "profile_revision",
    ]) || typeof value.payload.assignee_id !== "string" || value.payload.assignee_id.trim().length === 0) {
    return false;
  }
  if (value.payload.assignee_kind === "human") {
    return value.payload.assignee_id === scope.principal_id && value.payload.profile_revision === null;
  }
  return value.payload.assignee_kind === "profile"
    && positiveInteger(value.payload.profile_revision);
}

function isSafeRejection(value: unknown): boolean {
  return typeof value === "string" && SAFE_REJECTION_CODES.has(value);
}

function sameScope(value: unknown, expected: AppointmentScope): boolean {
  return isRecord(value) && hasExactKeys(value, [
    "workspace_id", "principal_id", "target_kind", "target_id",
  ]) && value.workspace_id === expected.workspace_id
    && value.principal_id === expected.principal_id
    && value.target_kind === expected.target_kind
    && value.target_id === expected.target_id;
}

function hasExactKeys(value: Record<string, unknown>, expected: readonly string[]): boolean {
  const keys = Object.keys(value);
  return keys.length === expected.length && expected.every((key) => Object.prototype.hasOwnProperty.call(value, key));
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

function unavailable(reason: string): AppointmentReviewResult {
  return { ok: false, action: "unavailable", reason };
}
