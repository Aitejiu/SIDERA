import type { CommandRequest, CommandResponse } from "../assignments/types";
import type { GovernanceAction } from "../collaboration/types";
import type { WorkerWorkspace } from "../worker-items/types";
import { resultInputActionPayload, type ResultInputAction } from "./api";

export type WorkerResultOutcome = "paused" | "approved" | "stale" | "rejected" | "automatic" | "queued";
export type WorkerResultActionScope = {
  workspace_id: string;
  root_task_id: string;
  worker_item_id: string;
  principal_id: string;
};
export type WorkerResultActionStatus = "idle" | "pending" | "unknown" | "applied" | "rejected";
export type WorkerResultActionReview = { action: ResultInputAction; reason: string | null };
export type NativeWorkerResultActionDraft = {
  schema_version: 1;
  scope: WorkerResultActionScope;
  review: WorkerResultActionReview | null;
  original: CommandRequest | null;
  status: WorkerResultActionStatus;
  aggregate_id: string | null;
  aggregate_version: number | null;
  outcome: WorkerResultOutcome | null;
  rejection_code: "command_rejected" | null;
};
export type WorkerResultStorageError = "invalid_draft" | "storage_unavailable";

const actionPairs = {
  PauseCollaborationRelease: ["pause", "worker_item"],
  ApproveCollaborationReleaseProposal: ["approve", "collaboration_release_proposal"],
  RejectCollaborationReleaseProposal: ["reject", "collaboration_release_proposal"],
  ResumeAutomaticCollaboration: ["resume_automatic", "collaboration_governance_hold"],
  RetryContextAssembly: ["retry_context_assembly", "context_assembly"],
} as const;
const actionKeys = [
  "action", "command_type", "target_type", "target_id", "expected_version",
  "expected_fingerprint", "expected_plan_generation", "expected_input_selection_id", "expected_input_selection_hash",
];
const scopeKeys = ["workspace_id", "root_task_id", "worker_item_id", "principal_id"];

export function reviewWorkerGovernanceAction(
  workspace: WorkerWorkspace,
  action: GovernanceAction,
): ResultInputAction | null {
  const collaboration = workspace.collaboration;
  if (!collaboration?.enabled || !collaboration.available_actions.includes(action)
    || !isUuid(workspace.workspace_id) || !isUuid(workspace.worker_item.id)
    || !isUuid(workspace.worker_item.root_task_id)) return null;
  const generation = workspace.current_placement?.plan_generation ?? null;
  const base = {
    action, expected_plan_generation: generation,
    expected_fingerprint: null, expected_input_selection_id: null, expected_input_selection_hash: null,
  };
  let descriptor: ResultInputAction;
  if (action === "pause") {
    descriptor = {
      ...base, command_type: "PauseCollaborationRelease", target_type: "worker_item",
      target_id: workspace.worker_item.id, expected_version: workspace.worker_item.version,
    };
  } else if (action === "resume_automatic") {
    if (!collaboration.active_hold_id) return null;
    descriptor = {
      ...base, command_type: "ResumeAutomaticCollaboration", target_type: "collaboration_governance_hold",
      target_id: collaboration.active_hold_id, expected_version: collaboration.hold_resource_version ?? null,
    };
  } else {
    const proposal = collaboration.active_proposal;
    if (!proposal || proposal.status !== "pending"
      || (proposal.input_selection_id === undefined) !== (proposal.input_selection_hash === undefined)) return null;
    for (const [key, expected] of [
      ["workspace_id", workspace.workspace_id], ["root_task_id", workspace.worker_item.root_task_id],
      ["worker_item_id", workspace.worker_item.id],
    ]) {
      const supplied = (proposal as unknown as Record<string, unknown>)[key ?? ""];
      if (supplied !== undefined && supplied !== expected) return null;
    }
    descriptor = {
      ...base, command_type: action === "approve" ? "ApproveCollaborationReleaseProposal" : "RejectCollaborationReleaseProposal",
      target_type: "collaboration_release_proposal", target_id: proposal.proposal_id,
      expected_version: proposal.resource_version, expected_fingerprint: proposal.input_selection_hash ?? null,
      expected_input_selection_id: proposal.input_selection_id ?? null,
      expected_input_selection_hash: proposal.input_selection_hash ?? null,
    };
  }
  return validAction(descriptor) ? descriptor : null;
}

export function freezeWorkerResultActionReview(
  action: ResultInputAction,
  reason: string | null = null,
): WorkerResultActionReview | null {
  if (!validAction(action) || !(reason === null || typeof reason === "string")
    || (reason !== null && reason.length > 2000)) return null;
  return Object.freeze({ action: Object.freeze({ ...action }), reason: reason?.trim() || null });
}

export function workerResultActionFingerprint(review: WorkerResultActionReview): string {
  const action = review.action;
  return JSON.stringify([
    action.action, action.command_type, action.target_type, action.target_id, action.expected_version,
    action.expected_fingerprint, action.expected_plan_generation, action.expected_input_selection_id,
    action.expected_input_selection_hash, review.reason?.trim() || null,
  ]);
}

export function nativeWorkerResultActionDraftKey(scope: WorkerResultActionScope): string {
  return `sidera.native-worker-result-action.v1:${[
    scope.workspace_id, scope.principal_id, scope.root_task_id, scope.worker_item_id,
  ].map(encodeURIComponent).join(":")}`;
}

export function emptyNativeWorkerResultActionDraft(scope: WorkerResultActionScope): NativeWorkerResultActionDraft {
  return {
    schema_version: 1, scope: { ...scope }, review: null, original: null, status: "idle",
    aggregate_id: null, aggregate_version: null, outcome: null, rejection_code: null,
  };
}

export function loadNativeWorkerResultActionDraft(
  scope: WorkerResultActionScope,
  storage?: Pick<Storage, "getItem">,
): { ok: true; draft: NativeWorkerResultActionDraft } | { ok: false; error: WorkerResultStorageError } {
  try {
    if (!validScope(scope)) return { ok: false, error: "invalid_draft" };
    const raw = (storage ?? window.localStorage).getItem(nativeWorkerResultActionDraftKey(scope));
    if (raw === null) return { ok: true, draft: emptyNativeWorkerResultActionDraft(scope) };
    if (raw.length > 16384) return { ok: false, error: "invalid_draft" };
    let parsed: unknown;
    try { parsed = JSON.parse(raw); }
    catch { return { ok: false, error: "invalid_draft" }; }
    if (!validDraft(scope, parsed)) return { ok: false, error: "invalid_draft" };
    return { ok: true, draft: deepFreeze(parsed) };
  } catch {
    return { ok: false, error: "storage_unavailable" };
  }
}

export function saveNativeWorkerResultActionDraft(
  scope: WorkerResultActionScope,
  draft: NativeWorkerResultActionDraft,
  storage?: Pick<Storage, "setItem">,
): { ok: true } | { ok: false; error: WorkerResultStorageError } {
  try {
    if (!validDraft(scope, draft)) return { ok: false, error: "invalid_draft" };
    const serialized = JSON.stringify(draft);
    if (serialized.length > 16384) return { ok: false, error: "invalid_draft" };
    (storage ?? window.localStorage).setItem(nativeWorkerResultActionDraftKey(scope), serialized);
    return { ok: true };
  } catch {
    return { ok: false, error: "storage_unavailable" };
  }
}

export function reviewWorkerResultActionResult(
  original: CommandRequest,
  response: CommandResponse,
): {
  status: "unknown" | "applied";
  aggregate_id: string | null;
  aggregate_version: number | null;
  outcome: WorkerResultOutcome | null;
} {
  const empty = { aggregate_id: null, aggregate_version: null, outcome: null };
  if (!isRecord(response) || response.command_id !== original.command_id) {
    return { status: "unknown", ...empty };
  }
  const handled = { status: "applied" as const, ...empty };
  if (!isUuid(response.aggregate_id) || !positiveInteger(response.aggregate_version)
    || !isRecord(response.data)) return handled;
  const data = response.data;
  let outcome: WorkerResultOutcome | null = null;
  if (original.command_type === "PauseCollaborationRelease") {
    if (response.aggregate_id !== original.target_id && response.aggregate_version === 1
      && data.hold_id === response.aggregate_id && data.hold_version === 1
      && data.governance_mode === "paused") outcome = "paused";
  } else if (positiveInteger(original.expected_version)
    && response.aggregate_id === original.target_id
    && response.aggregate_version === original.expected_version + 1) {
    switch (original.command_type) {
      case "ApproveCollaborationReleaseProposal":
        if (data.proposal_id === original.target_id) {
          if (data.proposal_status === "approved" && data.result === "applied") outcome = "approved";
          if (data.proposal_status === "stale" && data.result === "stale") outcome = "stale";
        }
        break;
      case "RejectCollaborationReleaseProposal":
        if (data.proposal_id === original.target_id && data.proposal_status === "rejected") outcome = "rejected";
        break;
      case "ResumeAutomaticCollaboration":
        if (data.hold_id === original.target_id && data.hold_version === response.aggregate_version
          && data.governance_mode === "automatic") outcome = "automatic";
        break;
      case "RetryContextAssembly":
        if (data.context_assembly_request_id === original.target_id && data.status === "queued") outcome = "queued";
        break;
    }
  }
  return outcome === null ? handled : {
    status: "applied", aggregate_id: response.aggregate_id,
    aggregate_version: response.aggregate_version, outcome,
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isUuid(value: unknown): value is string {
  return typeof value === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);
}

function positiveInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0;
}

function validAction(value: unknown): value is ResultInputAction {
  if (!isRecord(value) || !exactKeys(value, actionKeys) || !isUuid(value.target_id)
    || !positiveInteger(value.expected_version)
    || !(value.expected_plan_generation === null || positiveInteger(value.expected_plan_generation))
    || typeof value.command_type !== "string" || !Object.hasOwn(actionPairs, value.command_type)) return false;
  const pair = actionPairs[value.command_type as keyof typeof actionPairs];
  if (value.action !== pair[0] || value.target_type !== pair[1]) return false;
  if (value.command_type === "ApproveCollaborationReleaseProposal"
    || value.command_type === "RejectCollaborationReleaseProposal") {
    return ((value.expected_input_selection_id === null && value.expected_input_selection_hash === null)
      || (isUuid(value.expected_input_selection_id) && isHash(value.expected_input_selection_hash)))
      && value.expected_fingerprint === value.expected_input_selection_hash;
  }
  if (value.expected_input_selection_id !== null || value.expected_input_selection_hash !== null) return false;
  if (value.command_type === "RetryContextAssembly") return isHash(value.expected_fingerprint);
  return value.expected_fingerprint === null
    && (value.command_type !== "PauseCollaborationRelease" || positiveInteger(value.expected_plan_generation));
}

function validScope(value: unknown): value is WorkerResultActionScope {
  return isRecord(value) && exactKeys(value, scopeKeys) && isUuid(value.workspace_id)
    && isUuid(value.root_task_id) && isUuid(value.worker_item_id) && boundedString(value.principal_id, 256);
}

function validDraft(scope: WorkerResultActionScope, value: unknown): value is NativeWorkerResultActionDraft {
  if (!validScope(scope) || !isRecord(value) || !exactKeys(value, [
    "schema_version", "scope", "review", "original", "status", "aggregate_id", "aggregate_version", "outcome", "rejection_code",
  ]) || value.schema_version !== 1 || !validScope(value.scope)
    || scopeKeys.some(key => value.scope && (value.scope as Record<string, unknown>)[key] !== scope[key as keyof WorkerResultActionScope])) return false;
  if (value.original === null) {
    return value.status === "idle" && value.review === null && value.aggregate_id === null
      && value.aggregate_version === null && value.outcome === null && value.rejection_code === null;
  }
  if (!isRecord(value.review) || !exactKeys(value.review, ["action", "reason"])
    || !validAction(value.review.action)
    || !(value.review.reason === null || (boundedString(value.review.reason, 2000) && value.review.reason.trim() === value.review.reason))
    || !isRecord(value.original) || !exactKeys(value.original, [
      "command_id", "workspace_id", "command_type", "actor", "target_type", "target_id",
      "expected_version", "idempotency_key", "correlation_id", "payload",
    ])) return false;
  const original = value.original;
  const action = value.review.action;
  if (!isUuid(original.command_id) || !isUuid(original.correlation_id) || !boundedString(original.idempotency_key, 256)
    || original.workspace_id !== scope.workspace_id || !isRecord(original.actor)
    || !exactKeys(original.actor, ["type", "id"]) || original.actor.type !== "user" || original.actor.id !== scope.principal_id
    || original.command_type !== action.command_type || original.target_type !== action.target_type
    || original.target_id !== action.target_id || original.expected_version !== action.expected_version
    || (action.command_type === "PauseCollaborationRelease" && original.target_id !== scope.worker_item_id)
    || !samePayload(original.payload, resultInputActionPayload(action, value.review.reason))) return false;
  if (typeof value.status !== "string" || !["pending", "unknown", "applied", "rejected"].includes(value.status)) return false;
  if (value.rejection_code !== (value.status === "rejected" ? "command_rejected" : null)) return false;
  if (value.outcome === null) return value.aggregate_id === null && value.aggregate_version === null;
  if (value.status !== "applied" || !isUuid(value.aggregate_id) || !positiveInteger(value.aggregate_version)) return false;
  const outcomes: Record<string, readonly WorkerResultOutcome[]> = {
    PauseCollaborationRelease: ["paused"], ApproveCollaborationReleaseProposal: ["approved", "stale"],
    RejectCollaborationReleaseProposal: ["rejected"], ResumeAutomaticCollaboration: ["automatic"], RetryContextAssembly: ["queued"],
  };
  return outcomes[action.command_type]?.includes(value.outcome as WorkerResultOutcome) === true
    && (action.command_type === "PauseCollaborationRelease"
      ? value.aggregate_id !== action.target_id && value.aggregate_version === 1
      : value.aggregate_id === action.target_id && value.aggregate_version === (action.expected_version ?? 0) + 1);
}

export function sameWorkerResultActionPayload(value: unknown, expected: CommandRequest["payload"]): boolean {
  return samePayload(value, expected);
}

function samePayload(value: unknown, expected: CommandRequest["payload"]): boolean {
  return isRecord(value) && exactKeys(value, Object.keys(expected))
    && Object.keys(expected).every(key => value[key] === expected[key]);
}

function exactKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  return Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
}

function boundedString(value: unknown, maximum: number): value is string {
  return typeof value === "string" && value.trim().length > 0 && value.length <= maximum;
}

function isHash(value: unknown): value is string {
  return typeof value === "string" && /^[0-9a-f]{64}$/i.test(value);
}

function deepFreeze<T>(value: T): T {
  if (typeof value !== "object" || value === null || Object.isFrozen(value)) return value;
  for (const child of Object.values(value)) deepFreeze(child);
  return Object.freeze(value);
}
