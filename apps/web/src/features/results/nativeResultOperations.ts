import type { CommandRequest } from "@/features/assignments/types";

import type { ResultAction } from "./api";

export interface NativeResultOperationScope {
  workspace_id: string;
  root_task_id: string;
  worker_item_id: string;
  principal_id: string;
}

export type NativeResultOperationReview = Pick<ResultAction,
  "command_type" | "target_type" | "target_id" | "expected_version"
  | "expected_result_version" | "candidate_id" | "candidate_hash"
  | "plan_content_hash" | "expected_fingerprint" | "requires_reason"
  | "allowed_worker_item_ids" | "replacement_result_ids">;

export interface NativeResultOperationDraft {
  schema_version: 1;
  scope: NativeResultOperationScope;
  review: NativeResultOperationReview;
  original: CommandRequest;
  status: "pending" | "unknown" | "applied" | "rejected";
}

const STORAGE_UNAVAILABLE = "本地结果操作记录暂不可用";
const COMMAND_TYPES = ["RequestTaskRework", "AuthorizeAdditionalRework", "RetryResultVerification", "ReintegrateTaskResult"];
type OperationOutcome = "rework_requested" | "attention_required" | "budget_authorized"
  | "verification_scheduled" | "verification_inspection_required" | "integration_scheduled";

export function nativeResultOperationDraftKey(scope: NativeResultOperationScope): string {
  return `sidera.native-result-operation.v1:${encodeURIComponent(scope.workspace_id)}:${encodeURIComponent(scope.principal_id)}:${encodeURIComponent(scope.root_task_id)}:${encodeURIComponent(scope.worker_item_id)}`;
}

export function loadNativeResultOperationDraft(scope: NativeResultOperationScope):
  { ok: true; draft: NativeResultOperationDraft | null } | { ok: false; reason: string } {
  try {
    if (!validScope(scope)) return { ok: false, reason: STORAGE_UNAVAILABLE };
    const storage = globalThis.localStorage;
    if (!storage) return { ok: false, reason: STORAGE_UNAVAILABLE };
    const stored = storage.getItem(nativeResultOperationDraftKey(scope));
    if (stored === null) return { ok: true, draft: null };
    const value: unknown = JSON.parse(stored);
    if (validDraft(value, scope)) return { ok: true, draft: freeze(value) };
  } catch {
    return { ok: false, reason: STORAGE_UNAVAILABLE };
  }
  return { ok: false, reason: STORAGE_UNAVAILABLE };
}

export function saveNativeResultOperationDraft(scope: NativeResultOperationScope, draft: NativeResultOperationDraft):
  { ok: true } | { ok: false; reason: string } {
  try {
    if (!validDraft(draft, scope)) return { ok: false, reason: STORAGE_UNAVAILABLE };
    const storage = globalThis.localStorage;
    if (!storage) return { ok: false, reason: STORAGE_UNAVAILABLE };
    storage.setItem(nativeResultOperationDraftKey(scope), JSON.stringify(draft));
    return { ok: true };
  } catch {
    return { ok: false, reason: STORAGE_UNAVAILABLE };
  }
}

export function reviewNativeResultOperationResult(draft: NativeResultOperationDraft, response: unknown):
  { status: "unknown" | "applied"; mappingAvailable: boolean; outcome: OperationOutcome | null } {
  if (!validDraft(draft, draft.scope) || !record(response)
    || response.command_id !== draft.original.command_id
    || !uuid(response.aggregate_id) || !positive(response.aggregate_version)
    || !resourceList(response.event_ids) || !resultEnvelopeMatches(draft, response)) {
    return { status: "unknown", mappingAvailable: false, outcome: null };
  }
  const outcome = record(response.data) ? mappedOutcome(draft, response, response.data) : null;
  return { status: "applied", mappingAvailable: outcome !== null, outcome };
}

export function nativeResultOperationStatusMatches(draft: NativeResultOperationDraft, response: unknown): boolean {
  return validDraft(draft, draft.scope) && record(response)
    && response.command_id === draft.original.command_id
    && response.command_type === draft.original.command_type
    && response.target_type === draft.original.target_type && response.target_id === draft.original.target_id
    && response.expected_version === draft.original.expected_version
    && typeof response.status === "string" && ["pending", "applied", "rejected"].includes(response.status)
    && (response.payload === undefined || sameJson(response.payload, draft.original.payload))
    && (response.result === undefined || response.result === null
      || (record(response.result) && response.result.command_id === draft.original.command_id));
}

function resultEnvelopeMatches(draft: NativeResultOperationDraft, response: Record<string, unknown>): boolean {
  const version = response.aggregate_version as number;
  switch (draft.review.command_type) {
    case "RequestTaskRework":
      return response.aggregate_id === draft.scope.root_task_id && version === draft.review.expected_result_version;
    case "AuthorizeAdditionalRework":
      return response.aggregate_id === draft.scope.root_task_id && version === draft.review.expected_result_version + 1;
    case "ReintegrateTaskResult":
      return response.aggregate_id === draft.scope.root_task_id && version > draft.review.expected_result_version;
    case "RetryResultVerification":
      return response.aggregate_id !== draft.original.target_id || version === draft.review.expected_version;
    default: return false;
  }
}

function mappedOutcome(draft: NativeResultOperationDraft, response: Record<string, unknown>, data: Record<string, unknown>): OperationOutcome | null {
  switch (draft.review.command_type) {
    case "RequestTaskRework":
      if (!uuid(data.rework_record_id) || !resourceList(data.request_ids)) return null;
      if (data.status === "blocked" && data.attention_required === true && nonblank(data.reason)
        && data.request_ids.length === 0) return "attention_required";
      return data.status === "requested" && data.round_number === null && data.request_ids.length === 1
        && singleWorker(data.target_worker_item_ids, draft.scope.worker_item_id)
        && singleWorker(data.impact_worker_item_ids, draft.scope.worker_item_id) ? "rework_requested" : null;
    case "AuthorizeAdditionalRework":
      return nonnegative(data.auto_rounds_used) && nonnegative(data.auto_rounds_limit)
        && nonnegative(data.auto_seconds_limit)
        && data.auto_rounds_limit >= (draft.original.payload.additional_rounds as number)
        && data.auto_seconds_limit >= (draft.original.payload.additional_seconds as number)
        && (data.auto_deadline === null || (nonblank(data.auto_deadline) && Number.isFinite(Date.parse(data.auto_deadline))))
        ? "budget_authorized" : null;
    case "RetryResultVerification":
      if (data.operation_id !== response.aggregate_id || !positive(data.attempt)) return null;
      if (data.inspection_required === true) {
        return data.status === "unknown" && response.aggregate_id === draft.original.target_id
          && response.aggregate_version === draft.original.expected_version
          && data.previous_operation_id === undefined && data.execution_request_id === undefined
          ? "verification_inspection_required" : null;
      }
      return response.aggregate_id !== draft.original.target_id && data.previous_operation_id === draft.original.target_id
        && uuid(data.execution_request_id) && typeof data.status === "string"
        && ["pending", "running", "blocked", "unknown", "failed", "succeeded"].includes(data.status)
        ? "verification_scheduled" : null;
    case "ReintegrateTaskResult":
      return data.status === "scheduled" && data.source_candidate_id === draft.original.payload.source_candidate_id
        && uuid(data.execution_request_id) && uuid(data.input_selection_id) && uuid(data.release_id)
        && uuid(data.integration_operation_id) && uuid(data.rework_record_id)
        && data.target_observation_id === draft.original.payload.target_observation_id ? "integration_scheduled" : null;
    default: return null;
  }
}

function validDraft(value: unknown, scope: NativeResultOperationScope): value is NativeResultOperationDraft {
  if (!validScope(scope) || !record(value) || !exact(value, ["schema_version", "scope", "review", "original", "status"])
    || value.schema_version !== 1 || !validScope(value.scope) || !sameJson(value.scope, scope)
    || !validReview(value.review, scope)
    || typeof value.status !== "string" || !["pending", "unknown", "applied", "rejected"].includes(value.status)) return false;
  const command = value.original;
  const review = value.review;
  return record(command) && exact(command, ["command_id", "workspace_id", "command_type", "actor", "target_type", "target_id", "expected_version", "idempotency_key", "correlation_id", "payload"])
    && uuid(command.command_id) && command.workspace_id === scope.workspace_id
    && command.command_type === review.command_type && command.target_type === review.target_type
    && command.target_id === review.target_id && command.expected_version === review.expected_version
    && nonblank(command.idempotency_key) && uuid(command.correlation_id)
    && record(command.actor) && exact(command.actor, ["type", "id"])
    && command.actor.type === "user" && command.actor.id === scope.principal_id
    && validPayload(command.payload, review, scope);
}

function validScope(value: unknown): value is NativeResultOperationScope {
  return record(value) && exact(value, ["workspace_id", "root_task_id", "worker_item_id", "principal_id"])
    && uuid(value.workspace_id) && uuid(value.root_task_id) && uuid(value.worker_item_id) && nonblank(value.principal_id);
}

function validReview(value: unknown, scope: NativeResultOperationScope): value is NativeResultOperationReview {
  if (!record(value) || !exact(value, ["command_type", "target_type", "target_id", "expected_version", "expected_result_version", "candidate_id", "candidate_hash", "plan_content_hash", "expected_fingerprint", "requires_reason", "allowed_worker_item_ids", "replacement_result_ids"])
    || typeof value.command_type !== "string" || !COMMAND_TYPES.includes(value.command_type)
    || !uuid(value.target_id) || !positive(value.expected_version) || !positive(value.expected_result_version)
    || !(value.candidate_id === null || uuid(value.candidate_id))
    || !(value.candidate_hash === null || hash(value.candidate_hash))
    || !(value.plan_content_hash === null || hash(value.plan_content_hash))
    || !(value.expected_fingerprint === null || (hash(value.expected_fingerprint) && value.expected_fingerprint === value.candidate_hash))
    || typeof value.requires_reason !== "boolean" || !resourceList(value.allowed_worker_item_ids)
    || !resourceList(value.replacement_result_ids)) return false;
  const retry = value.command_type === "RetryResultVerification";
  return value.target_type === (retry ? "result_operation" : "task")
    && (retry || (value.target_id === scope.root_task_id && value.expected_version === value.expected_result_version))
    && (value.command_type === "AuthorizeAdditionalRework" || (uuid(value.candidate_id) && hash(value.candidate_hash)));
}

function validPayload(value: unknown, review: NativeResultOperationReview, scope: NativeResultOperationScope): boolean {
  if (!record(value) || !nonblank(value.reason)) return false;
  switch (review.command_type) {
    case "RequestTaskRework":
      return exact(value, ["candidate_id", "candidate_hash", "mode", "issue_keys", "target_worker_item_ids", "impact_worker_item_ids", "reason", "suggestion"])
        && value.candidate_id === review.candidate_id && value.candidate_hash === review.candidate_hash
        && value.mode === "user" && value.suggestion === null && list(value.issue_keys, nonblank)
        && singleWorker(value.target_worker_item_ids, scope.worker_item_id) && singleWorker(value.impact_worker_item_ids, scope.worker_item_id)
        && (review.allowed_worker_item_ids.length === 0 || review.allowed_worker_item_ids.includes(scope.worker_item_id));
    case "AuthorizeAdditionalRework":
      return exact(value, ["additional_rounds", "additional_seconds", "reason"])
        && nonnegative(value.additional_rounds) && nonnegative(value.additional_seconds)
        && (value.additional_rounds > 0 || value.additional_seconds > 0);
    case "RetryResultVerification":
      return exact(value, ["verification_operation_id", "expected_operation_version", "expected_result_version", "candidate_id", "candidate_hash", "reason"])
        && value.verification_operation_id === review.target_id && value.expected_operation_version === review.expected_version
        && value.expected_result_version === review.expected_result_version
        && value.candidate_id === review.candidate_id && value.candidate_hash === review.candidate_hash;
    case "ReintegrateTaskResult":
      return exact(value, ["source_candidate_id", "source_candidate_hash", "replacement_result_ids", "expected_result_version", "target_observation_id", "reason"])
        && value.source_candidate_id === review.candidate_id && value.source_candidate_hash === review.candidate_hash
        && value.expected_result_version === review.expected_result_version
        && resourceList(value.replacement_result_ids) && value.replacement_result_ids.length > 0
        && sameJson(value.replacement_result_ids, review.replacement_result_ids)
        && (value.target_observation_id === null || uuid(value.target_observation_id));
    default: return false;
  }
}

function sameJson(left: unknown, right: unknown): boolean {
  if (left === right) return true;
  if (Array.isArray(left) && Array.isArray(right)) {
    return Object.keys(left).length === left.length && left.length === right.length
      && left.every((item, index) => sameJson(item, right[index]));
  }
  return record(left) && record(right) && exact(left, Object.keys(right))
    && Object.keys(left).every(key => sameJson(left[key], right[key]));
}

function freeze<T>(value: T): T {
  if (typeof value !== "object" || value === null || Object.isFrozen(value)) return value;
  for (const child of Object.values(value)) freeze(child);
  return Object.freeze(value);
}

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function exact(value: Record<string, unknown>, keys: readonly string[]): boolean {
  return Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
}

function nonblank(value: unknown): value is string { return typeof value === "string" && value.trim().length > 0; }
function uuid(value: unknown): value is string { return typeof value === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value); }
function hash(value: unknown): value is string { return typeof value === "string" && /^[0-9a-f]{64}$/.test(value); }
function positive(value: unknown): value is number { return typeof value === "number" && Number.isSafeInteger(value) && value > 0; }
function nonnegative(value: unknown): value is number { return typeof value === "number" && Number.isSafeInteger(value) && value >= 0; }
function list(value: unknown, valid: (item: unknown) => boolean): value is unknown[] {
  return Array.isArray(value) && Object.keys(value).length === value.length && value.every(valid);
}
function resourceList(value: unknown): value is string[] { return list(value, uuid) && new Set(value).size === value.length; }
function singleWorker(value: unknown, workerId: string): boolean { return list(value, uuid) && value.length === 1 && value[0] === workerId; }
