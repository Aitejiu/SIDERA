import type { CommandRequest } from "../assignments/types";
import type { RecoverWorkerTransitionPayload, WorkerTransition, WorkerWorkspace } from "./types";

export type NativeTransitionRecoveryScope = { workspaceId: string; principalId: string; workerItemId: string };
export type NativeTransitionRecoveryReview = {
  workerTransitionId: string; planTransitionId: string; rootTaskId: string; version: number;
  action: WorkerTransition["action"]; fallbackAction: WorkerTransition["fallback_action"];
  sourceExecutionId: string | null; sourceSessionId: string | null;
  currentContextVersion: number; targetContextVersion: number; acknowledgedContextVersion: number;
  patchId: string | null; patchHash: string | null; patchAcknowledged: boolean;
};
export type NativeTransitionRecoveryIntent = NativeTransitionRecoveryReview & {
  strategy: RecoverWorkerTransitionPayload["strategy"]; reason: string; evidenceRefs: string[];
};
export type NativeTransitionRecoveryStatus = "idle" | "pending" | "unknown" | "applied" | "rejected";
export type NativeTransitionRecoveryOutcome = "recorded" | "queued" | "branch_closed";
export type NativeTransitionRecoveryDraft = {
  schema_version: 1; scope: NativeTransitionRecoveryScope; intent: NativeTransitionRecoveryIntent | null;
  original: CommandRequest | null; status: NativeTransitionRecoveryStatus;
  aggregate_id: string | null; aggregate_version: number | null;
  outcome: NativeTransitionRecoveryOutcome | null; rejection_code: "command_rejected" | null;
};
export type NativeTransitionRecoveryStorageError = "invalid_draft" | "storage_unavailable";

export function emptyNativeTransitionRecoveryDraft(scope: NativeTransitionRecoveryScope): NativeTransitionRecoveryDraft {
  return { schema_version: 1, scope: { ...scope }, intent: null, original: null, status: "idle",
    aggregate_id: null, aggregate_version: null, outcome: null, rejection_code: null };
}

const scopeKeys = ["workspaceId", "principalId", "workerItemId"] as const;
const reviewKeys = ["workerTransitionId", "planTransitionId", "rootTaskId", "version", "action", "fallbackAction",
  "sourceExecutionId", "sourceSessionId", "currentContextVersion", "targetContextVersion", "acknowledgedContextVersion",
  "patchId", "patchHash", "patchAcknowledged"] as const;
const intentKeys = [...reviewKeys, "strategy", "reason", "evidenceRefs"];
const strategies: readonly RecoverWorkerTransitionPayload["strategy"][] = ["reconcile_adapter", "continue_after_confirmed_stop",
  "continue_after_confirmed_patch_absence", "fail_closed_branch"];
const actions = ["preserve", "move", "patch", "continue", "create", "retire"];

export function freezeNativeTransitionRecoveryIntent(intent: NativeTransitionRecoveryIntent): NativeTransitionRecoveryIntent | null {
  if (!isRecord(intent) || !exactKeys(intent, intentKeys) || typeof intent.reason !== "string"
    || !Array.isArray(intent.evidenceRefs) || intent.evidenceRefs.some(ref => typeof ref !== "string")) return null;
  const normalized = { ...intent, reason: intent.reason.trim(), evidenceRefs: intent.evidenceRefs.map(ref => ref.trim()).filter(Boolean) };
  return validIntent(normalized) ? deepFreeze(normalized) : null;
}

export function nativeTransitionRecoveryDraftKey(scope: NativeTransitionRecoveryScope): string {
  return `sidera.native-transition-recovery.v1:${[scope.workspaceId, scope.principalId, scope.workerItemId].map(encodeURIComponent).join(":")}`;
}

export function loadNativeTransitionRecoveryDraft(scope: NativeTransitionRecoveryScope, storage?: Pick<Storage, "getItem">):
  { ok: true; draft: NativeTransitionRecoveryDraft } | { ok: false; error: NativeTransitionRecoveryStorageError } {
  try {
    if (!validScope(scope)) return { ok: false, error: "invalid_draft" };
    const raw = (storage ?? window.localStorage).getItem(nativeTransitionRecoveryDraftKey(scope));
    if (raw === null) return { ok: true, draft: emptyNativeTransitionRecoveryDraft(scope) };
    if (raw.length > 16384) return { ok: false, error: "invalid_draft" };
    let value: unknown;
    try { value = JSON.parse(raw); } catch { return { ok: false, error: "invalid_draft" }; }
    return validDraft(scope, value) ? { ok: true, draft: deepFreeze(value) } : { ok: false, error: "invalid_draft" };
  } catch { return { ok: false, error: "storage_unavailable" }; }
}

export function saveNativeTransitionRecoveryDraft(scope: NativeTransitionRecoveryScope, draft: NativeTransitionRecoveryDraft, storage?: Pick<Storage, "setItem">):
  { ok: true } | { ok: false; error: NativeTransitionRecoveryStorageError } {
  try {
    if (!validDraft(scope, draft)) return { ok: false, error: "invalid_draft" };
    const raw = JSON.stringify(draft);
    if (raw.length > 16384) return { ok: false, error: "invalid_draft" };
    (storage ?? window.localStorage).setItem(nativeTransitionRecoveryDraftKey(scope), raw);
    return { ok: true };
  } catch { return { ok: false, error: "storage_unavailable" }; }
}

export function reviewNativeTransitionRecoveryResult(draft: NativeTransitionRecoveryDraft, response: unknown): Pick<NativeTransitionRecoveryDraft,
  "status" | "aggregate_id" | "aggregate_version" | "outcome"> {
  const empty = { aggregate_id: null, aggregate_version: null, outcome: null };
  if (!draft.original || !draft.intent || !isRecord(response) || response.command_id !== draft.original.command_id) {
    return { status: "unknown", ...empty };
  }
  const handled = { status: "applied" as const, ...empty };
  const intent = draft.intent;
  const version = intent.version + (intent.strategy === "reconcile_adapter" ? 0 : 1);
  if (response.aggregate_id !== intent.workerTransitionId || response.aggregate_version !== version
    || !isRecord(response.data) || !Array.isArray(response.event_ids) || response.event_ids.length === 0
    || !response.event_ids.every(isUuid)) return handled;
  const data = response.data;
  if (data.worker_transition_id !== intent.workerTransitionId || data.plan_transition_id !== intent.planTransitionId
    || data.version !== version || data.recovery_strategy !== intent.strategy) return handled;
  let outcome: NativeTransitionRecoveryOutcome | null = null;
  if (intent.strategy === "reconcile_adapter" && data.status === "blocked_unknown" && data.writer_state === "unknown"
    && data.action === intent.action && data.adapter_call === "not_started") outcome = "recorded";
  if (intent.strategy === "continue_after_confirmed_stop" && intent.sourceExecutionId !== null
    && ["continue", "retire"].includes(intent.action) && data.status === "pending"
    && data.writer_state === "fenced" && data.action === intent.action) outcome = "queued";
  if (intent.strategy === "continue_after_confirmed_patch_absence" && intent.patchId !== null
    && intent.action === "patch" && intent.fallbackAction === "continue" && data.status === "pending"
    && data.writer_state === "fenced" && data.action === "continue") outcome = "queued";
  if (intent.strategy === "fail_closed_branch" && data.status === "failed" && data.writer_state === "closed"
    && data.action === intent.action) outcome = "branch_closed";
  return outcome === null ? handled : {
    status: "applied", aggregate_id: intent.workerTransitionId, aggregate_version: version, outcome,
  };
}

export function reviewNativeTransitionRecovery(workspace: WorkerWorkspace, scope: NativeTransitionRecoveryScope, transition: WorkerTransition): NativeTransitionRecoveryReview | null {
  if (!validScope(scope) || !isRecord(workspace) || workspace.workspace_id !== scope.workspaceId
    || !isRecord(workspace.worker_item) || workspace.worker_item.id !== scope.workerItemId
    || !isRecord(workspace.plan_sync) || !isRecord(transition) || !isRecord(transition.root_task)
    || !isRecord(transition.worker_item) || !isRecord(transition.context)) return null;
  const sync = workspace.plan_sync;
  const context = transition.context;
  if (sync.status !== "blocked_unknown" || transition.status !== "blocked_unknown"
    || sync.worker_transition_id !== transition.worker_transition_id || sync.plan_transition_id !== transition.plan_transition_id
    || transition.root_task.id !== workspace.worker_item.root_task_id || transition.worker_item.id !== scope.workerItemId
    || sync.action !== transition.action || sync.writer_state !== transition.writer_state
    || sync.source_execution_id !== transition.source_execution_id || sync.source_session_id !== transition.source_session_id
    || sync.current_context_version !== context.current_context_version || sync.target_context_version !== context.target_context_version
    || sync.acknowledged_context_version !== context.acknowledged_context_version
    || sync.patch_id !== context.patch_id || sync.patch_acknowledged !== context.patch_acknowledged) return null;
  const review: NativeTransitionRecoveryReview = {
    workerTransitionId: transition.worker_transition_id, planTransitionId: transition.plan_transition_id,
    rootTaskId: transition.root_task.id, version: transition.version, action: transition.action,
    fallbackAction: transition.fallback_action, sourceExecutionId: transition.source_execution_id,
    sourceSessionId: transition.source_session_id, currentContextVersion: context.current_context_version,
    targetContextVersion: context.target_context_version, acknowledgedContextVersion: context.acknowledged_context_version,
    patchId: context.patch_id, patchHash: context.patch_hash, patchAcknowledged: context.patch_acknowledged,
  };
  return validReview(review) ? deepFreeze(review) : null;
}

export function nativeTransitionRecoveryStrategies(review: NativeTransitionRecoveryReview): readonly RecoverWorkerTransitionPayload["strategy"][] {
  return strategies.filter(strategy => strategy !== "continue_after_confirmed_stop"
    && strategy !== "continue_after_confirmed_patch_absence"
    || strategy === "continue_after_confirmed_stop" && review.sourceExecutionId !== null && ["continue", "retire"].includes(review.action)
    || strategy === "continue_after_confirmed_patch_absence" && review.patchId !== null && review.action === "patch" && review.fallbackAction === "continue");
}

export function sameNativeTransitionRecoveryReview(a: NativeTransitionRecoveryReview, b: NativeTransitionRecoveryReview): boolean {
  return reviewKeys.every(key => a[key] === b[key]);
}

export function nativeTransitionRecoveryPayload(intent: NativeTransitionRecoveryIntent): CommandRequest["payload"] {
  return { strategy: intent.strategy, reason: intent.reason, evidence_refs: [...intent.evidenceRefs] };
}

export function sameNativeTransitionRecoveryPayload(value: unknown, expected: CommandRequest["payload"]): boolean {
  return isRecord(value) && exactKeys(value, ["strategy", "reason", "evidence_refs"])
    && value.strategy === expected.strategy && value.reason === expected.reason
    && Array.isArray(value.evidence_refs) && Array.isArray(expected.evidence_refs)
    && value.evidence_refs.length === expected.evidence_refs.length
    && value.evidence_refs.every((ref, index) => typeof ref === "string" && ref === (expected.evidence_refs as unknown[])[index]);
}

function validScope(value: unknown): value is NativeTransitionRecoveryScope {
  return isRecord(value) && exactKeys(value, scopeKeys) && isUuid(value.workspaceId) && isUuid(value.workerItemId)
    && bounded(value.principalId, 256);
}
function validReview(value: Record<string, unknown>): boolean {
  return isUuid(value.workerTransitionId) && isUuid(value.planTransitionId) && isUuid(value.rootTaskId)
    && positive(value.version) && typeof value.action === "string" && actions.includes(value.action)
    && (value.fallbackAction === null || typeof value.fallbackAction === "string" && actions.includes(value.fallbackAction))
    && nullableUuid(value.sourceExecutionId) && nullableUuid(value.sourceSessionId)
    && (value.sourceExecutionId !== null || value.sourceSessionId === null)
    && nonnegative(value.currentContextVersion) && positive(value.targetContextVersion) && nonnegative(value.acknowledgedContextVersion)
    && nullableUuid(value.patchId) && (value.patchHash === null || typeof value.patchHash === "string" && /^[a-f0-9]{64}$/i.test(value.patchHash))
    && (value.patchId !== null || value.patchHash === null) && typeof value.patchAcknowledged === "boolean";
}
function validIntent(value: unknown): value is NativeTransitionRecoveryIntent {
  return isRecord(value) && exactKeys(value, intentKeys) && validReview(value)
    && typeof value.strategy === "string" && strategies.includes(value.strategy as RecoverWorkerTransitionPayload["strategy"])
    && nativeTransitionRecoveryStrategies(value as NativeTransitionRecoveryIntent).includes(value.strategy as RecoverWorkerTransitionPayload["strategy"])
    && bounded(value.reason, 2000) && value.reason === value.reason.trim()
    && Array.isArray(value.evidenceRefs) && value.evidenceRefs.length > 0 && value.evidenceRefs.length <= 32
    && value.evidenceRefs.every(ref => bounded(ref, 256) && ref.trim() === ref);
}
function validDraft(scope: NativeTransitionRecoveryScope, value: unknown): value is NativeTransitionRecoveryDraft {
  if (!validScope(scope) || !isRecord(value) || !exactKeys(value, ["schema_version", "scope", "intent", "original", "status",
    "aggregate_id", "aggregate_version", "outcome", "rejection_code"]) || value.schema_version !== 1
    || !validScope(value.scope) || scopeKeys.some(key => (value.scope as NativeTransitionRecoveryScope)[key] !== scope[key])) return false;
  if (value.original === null) return value.intent === null && value.status === "idle" && value.aggregate_id === null
    && value.aggregate_version === null && value.outcome === null && value.rejection_code === null;
  if (!validIntent(value.intent) || !isRecord(value.original) || !exactKeys(value.original, ["command_id", "workspace_id", "command_type",
    "actor", "target_type", "target_id", "expected_version", "idempotency_key", "correlation_id", "payload"])) return false;
  const command = value.original;
  const intent = value.intent;
  if (!isUuid(command.command_id) || !isUuid(command.correlation_id) || !bounded(command.idempotency_key, 256)
    || command.workspace_id !== scope.workspaceId || command.command_type !== "RecoverWorkerTransition"
    || command.target_type !== "worker_transition" || command.target_id !== intent.workerTransitionId
    || command.expected_version !== intent.version || !isRecord(command.actor) || !exactKeys(command.actor, ["type", "id"])
    || command.actor.type !== "user" || command.actor.id !== scope.principalId
    || !sameNativeTransitionRecoveryPayload(command.payload, nativeTransitionRecoveryPayload(intent))) return false;
  if (typeof value.status !== "string" || !["pending", "unknown", "applied", "rejected"].includes(value.status)
    || value.rejection_code !== (value.status === "rejected" ? "command_rejected" : null)) return false;
  if (value.outcome === null) return value.aggregate_id === null && value.aggregate_version === null;
  const outcome = intent.strategy === "reconcile_adapter" ? "recorded" : intent.strategy === "fail_closed_branch" ? "branch_closed" : "queued";
  return value.status === "applied" && typeof value.outcome === "string" && value.outcome === outcome
    && value.aggregate_id === intent.workerTransitionId
    && value.aggregate_version === intent.version + (intent.strategy === "reconcile_adapter" ? 0 : 1);
}
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function exactKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  return Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
}
function isUuid(value: unknown): value is string {
  return typeof value === "string" && /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(value);
}
function nullableUuid(value: unknown): boolean { return value === null || isUuid(value); }
function nonnegative(value: unknown): value is number { return typeof value === "number" && Number.isSafeInteger(value) && value >= 0; }
function positive(value: unknown): value is number { return nonnegative(value) && value > 0; }
function bounded(value: unknown, limit: number): value is string { return typeof value === "string" && value.trim().length > 0 && value.length <= limit; }
function deepFreeze<T>(value: T): T {
  if (typeof value !== "object" || value === null || Object.isFrozen(value)) return value;
  for (const child of Object.values(value)) deepFreeze(child);
  return Object.freeze(value);
}
