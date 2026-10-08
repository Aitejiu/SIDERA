import type { CommandRequest } from "@/features/assignments/types";

import type { AcceptanceCriterionDecision, CompleteTaskPayload, TaskResultOverview } from "./api";

export interface NativeAcceptanceScope {
  workspace_id: string;
  root_task_id: string;
  principal_id: string;
}

export interface NativeAcceptanceReview {
  root_task_id: string;
  task_version: number;
  task_status: TaskResultOverview["task_status"];
  result_version: number;
  candidate_id: string;
  candidate_hash: string;
  plan_content_hash: string;
}

export interface NativeAcceptanceDraft {
  schema_version: 1;
  scope: NativeAcceptanceScope;
  review: NativeAcceptanceReview;
  original: CommandRequest;
  status: "pending" | "unknown" | "applied" | "rejected";
}

const STORAGE_UNAVAILABLE = "本地验收记录暂不可用";
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const HASH = /^[0-9a-f]{64}$/;

export function nativeAcceptanceDraftKey(scope: NativeAcceptanceScope): string {
  return `sidera.native-result-acceptance.v1:${encodeURIComponent(scope.workspace_id)}:${encodeURIComponent(scope.principal_id)}:${encodeURIComponent(scope.root_task_id)}`;
}

export function loadNativeAcceptanceDraft(
  scope: NativeAcceptanceScope,
): { ok: true; draft: NativeAcceptanceDraft | null } | { ok: false; reason: string } {
  try {
    if (!validScope(scope)) return { ok: false, reason: STORAGE_UNAVAILABLE };
    const storage = globalThis.localStorage;
    if (!storage) return { ok: false, reason: STORAGE_UNAVAILABLE };
    const stored = storage.getItem(nativeAcceptanceDraftKey(scope));
    if (stored === null) return { ok: true, draft: null };
    const value: unknown = JSON.parse(stored);
    if (validDraft(value, scope)) return { ok: true, draft: freezeDraft(value) };
  } catch {
    return { ok: false, reason: STORAGE_UNAVAILABLE };
  }
  return { ok: false, reason: "本地验收记录暂不可用" };
}

export function saveNativeAcceptanceDraft(
  scope: NativeAcceptanceScope,
  draft: NativeAcceptanceDraft,
): { ok: true } | { ok: false; reason: string } {
  try {
    if (!validDraft(draft, scope)) return { ok: false, reason: STORAGE_UNAVAILABLE };
    const storage = globalThis.localStorage;
    if (!storage) return { ok: false, reason: STORAGE_UNAVAILABLE };
    storage.setItem(nativeAcceptanceDraftKey(scope), JSON.stringify(draft));
    return { ok: true };
  } catch {
    return { ok: false, reason: STORAGE_UNAVAILABLE };
  }
}

export function reviewNativeAcceptanceResult(
  draft: NativeAcceptanceDraft,
  response: unknown,
): { status: "unknown" | "applied"; mappingAvailable: boolean } {
  if (!validDraft(draft, draft.scope) || !record(response)
    || response.command_id !== draft.original.command_id
    || response.aggregate_id !== draft.scope.root_task_id
    || !positiveInteger(response.aggregate_version)
    || response.aggregate_version !== draft.review.task_version + (draft.review.task_status === "waiting" ? 0 : 1)
    || !Array.isArray(response.event_ids) || !response.event_ids.every(uuid)) {
    return { status: "unknown", mappingAvailable: false };
  }
  const data = response.data;
  const mappingAvailable = record(data)
    && data.status === "accepted" && uuid(data.acceptance_id)
    && data.candidate_id === draft.review.candidate_id
    && data.result_version === draft.original.payload.expected_result_version
    && data.task_status === "waiting"
    && ((data.result_stage === "applying" && uuid(data.apply_operation_id)
      && data.apply_status === "pending" && data.finalization_operation_id === null)
      || (data.result_stage === "finalizing" && uuid(data.finalization_operation_id)
        && data.apply_operation_id === null && data.apply_status === null));
  return { status: "applied", mappingAvailable };
}

export function nativeAcceptanceStatusMatches(draft: NativeAcceptanceDraft, response: unknown): boolean {
  return validDraft(draft, draft.scope) && record(response)
    && response.command_id === draft.original.command_id
    && response.command_type === "CompleteTask" && response.target_type === "task"
    && response.target_id === draft.original.target_id
    && response.expected_version === draft.original.expected_version
    && typeof response.status === "string" && ["pending", "applied", "rejected"].includes(response.status)
    && (response.payload === undefined || samePayload(response.payload, draft.original.payload))
    && (response.result === null || (record(response.result)
      && response.result.command_id === draft.original.command_id));
}

function validDraft(value: unknown, scope: NativeAcceptanceScope): value is NativeAcceptanceDraft {
  if (!validScope(scope) || !record(value) || !exact(value, ["schema_version", "scope", "review", "original", "status"])
    || value.schema_version !== 1 || !validScope(value.scope)
    || value.scope.workspace_id !== scope.workspace_id || value.scope.root_task_id !== scope.root_task_id
    || value.scope.principal_id !== scope.principal_id
    || !record(value.review) || !exact(value.review, ["root_task_id", "task_version", "task_status", "result_version", "candidate_id", "candidate_hash", "plan_content_hash"])
    || value.review.root_task_id !== scope.root_task_id
    || !positiveInteger(value.review.task_version) || !positiveInteger(value.review.result_version)
    || typeof value.review.task_status !== "string" || !["pending", "ready", "running", "waiting"].includes(value.review.task_status)
    || !uuid(value.review.candidate_id) || !hash(value.review.candidate_hash) || !hash(value.review.plan_content_hash)
    || typeof value.status !== "string" || !["pending", "unknown", "applied", "rejected"].includes(value.status)) return false;

  const command = value.original;
  return record(command) && exact(command, ["command_id", "workspace_id", "command_type", "actor", "target_type", "target_id", "expected_version", "idempotency_key", "correlation_id", "payload"])
    && uuid(command.command_id) && command.workspace_id === scope.workspace_id
    && command.command_type === "CompleteTask" && command.target_type === "task"
    && command.target_id === scope.root_task_id && command.expected_version === value.review.task_version
    && nonblank(command.idempotency_key) && uuid(command.correlation_id)
    && record(command.actor) && exact(command.actor, ["type", "id"])
    && command.actor.type === "user" && command.actor.id === scope.principal_id
    && validPayload(command.payload)
    && command.payload.candidate_id === value.review.candidate_id
    && command.payload.candidate_hash === value.review.candidate_hash
    && command.payload.expected_result_version === value.review.result_version;
}

function validScope(value: unknown): value is NativeAcceptanceScope {
  return record(value) && exact(value, ["workspace_id", "root_task_id", "principal_id"])
    && uuid(value.workspace_id) && uuid(value.root_task_id) && nonblank(value.principal_id);
}

function validPayload(value: unknown): value is CompleteTaskPayload {
  return record(value) && exact(value, ["candidate_id", "candidate_hash", "acceptance_basis_hash", "expected_result_version", "criteria"])
    && uuid(value.candidate_id) && hash(value.candidate_hash) && hash(value.acceptance_basis_hash)
    && positiveInteger(value.expected_result_version) && Array.isArray(value.criteria)
    && value.criteria.length > 0 && value.criteria.every(validCriterion)
    && new Set(value.criteria.map(criterion => criterion.criterion_key)).size === value.criteria.length;
}

function validCriterion(value: unknown): value is AcceptanceCriterionDecision {
  return record(value) && exact(value, ["criterion_key", "decision", "reason"])
    && nonblank(value.criterion_key)
    && typeof value.decision === "string" && ["accept_passed", "manual_passed", "exception_accepted"].includes(value.decision)
    && (value.reason === null || nonblank(value.reason))
    && (value.decision !== "exception_accepted" || nonblank(value.reason));
}

function samePayload(left: unknown, right: unknown): boolean {
  return validPayload(left) && validPayload(right)
    && left.candidate_id === right.candidate_id && left.candidate_hash === right.candidate_hash
    && left.acceptance_basis_hash === right.acceptance_basis_hash
    && left.expected_result_version === right.expected_result_version
    && left.criteria.length === right.criteria.length
    && left.criteria.every((criterion, index) => {
      const other = right.criteria[index];
      return other !== undefined && criterion.criterion_key === other.criterion_key
        && criterion.decision === other.decision && criterion.reason === other.reason;
    });
}

function freezeDraft(draft: NativeAcceptanceDraft): NativeAcceptanceDraft {
  for (const value of Object.values(draft)) freeze(value);
  return Object.freeze(draft);
}

function freeze(value: unknown): void {
  if (typeof value !== "object" || value === null || Object.isFrozen(value)) return;
  for (const child of Object.values(value)) freeze(child);
  Object.freeze(value);
}

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function exact(value: Record<string, unknown>, keys: readonly string[]): boolean {
  return Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
}

function nonblank(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function uuid(value: unknown): value is string {
  return typeof value === "string" && UUID.test(value);
}

function hash(value: unknown): value is string {
  return typeof value === "string" && HASH.test(value);
}

function positiveInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0;
}
