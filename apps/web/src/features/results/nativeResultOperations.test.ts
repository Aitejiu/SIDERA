import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { NativeResultOperationInput } from "./api";
import {
  loadNativeResultOperationDraft, nativeResultOperationDraftKey, nativeResultOperationStatusMatches,
  reviewNativeResultOperationResult, saveNativeResultOperationDraft,
  type NativeResultOperationDraft, type NativeResultOperationScope,
} from "./nativeResultOperations";
import { RESULT_CANDIDATE_ID, RESULT_OPERATION_ID, RESULT_ROOT_TASK_ID, RESULT_WORKSPACE_ID } from "./testFixtures";

const workerId = "71000000-0000-0000-0000-000000000021";
const mappingId = "71000000-0000-0000-0000-000000000040";
const nextId = "71000000-0000-0000-0000-000000000041";
const commandId = "71000000-0000-0000-0000-000000000042";
const scope: NativeResultOperationScope = {
  workspace_id: RESULT_WORKSPACE_ID, root_task_id: RESULT_ROOT_TASK_ID,
  worker_item_id: workerId, principal_id: "human:current/non-uuid",
};
const kinds = ["RequestTaskRework", "AuthorizeAdditionalRework", "RetryResultVerification", "ReintegrateTaskResult"] as const;

function draft(kind: typeof kinds[number] = "RequestTaskRework"): NativeResultOperationDraft {
  const payloads: Record<typeof kinds[number], NativeResultOperationInput["payload"]> = {
    RequestTaskRework: { candidate_id: RESULT_CANDIDATE_ID, candidate_hash: "c".repeat(64), mode: "user",
      issue_keys: ["expired-credentials"], target_worker_item_ids: [workerId], impact_worker_item_ids: [workerId], reason: "reviewed", suggestion: null },
    AuthorizeAdditionalRework: { additional_rounds: 1, additional_seconds: 300, reason: "reviewed" },
    RetryResultVerification: { verification_operation_id: RESULT_OPERATION_ID, expected_operation_version: 3,
      expected_result_version: 7, candidate_id: RESULT_CANDIDATE_ID, candidate_hash: "c".repeat(64), reason: "reviewed" },
    ReintegrateTaskResult: { source_candidate_id: RESULT_CANDIDATE_ID, source_candidate_hash: "c".repeat(64),
      replacement_result_ids: [mappingId], expected_result_version: 7, target_observation_id: null, reason: "reviewed" },
  };
  const retry = kind === "RetryResultVerification";
  const review = {
    command_type: kind, target_type: retry ? "result_operation" : "task",
    target_id: retry ? RESULT_OPERATION_ID : RESULT_ROOT_TASK_ID, expected_version: retry ? 3 : 7,
    expected_result_version: 7, candidate_id: RESULT_CANDIDATE_ID, candidate_hash: "c".repeat(64),
    plan_content_hash: "d".repeat(64), expected_fingerprint: "c".repeat(64), requires_reason: true,
    allowed_worker_item_ids: kind === "RequestTaskRework" ? [workerId] : [],
    replacement_result_ids: kind === "ReintegrateTaskResult" ? [mappingId] : [],
  };
  return {
    schema_version: 1, scope: { ...scope }, review,
    original: {
      command_id: commandId, workspace_id: RESULT_WORKSPACE_ID, command_type: kind,
      actor: { type: "user", id: scope.principal_id }, target_type: retry ? "result_operation" : "task",
      target_id: review.target_id, expected_version: review.expected_version, idempotency_key: "original-key",
      correlation_id: nextId, payload: payloads[kind],
    }, status: "pending",
  };
}

function receipt(value: NativeResultOperationDraft) {
  const data: Record<typeof kinds[number], Record<string, unknown>> = {
    RequestTaskRework: { rework_record_id: mappingId, request_ids: [nextId], target_worker_item_ids: [workerId],
      impact_worker_item_ids: [workerId], status: "requested", round_number: null },
    AuthorizeAdditionalRework: { auto_rounds_used: 2, auto_rounds_limit: 4, auto_seconds_limit: 1200, auto_deadline: null },
    RetryResultVerification: { operation_id: nextId, previous_operation_id: RESULT_OPERATION_ID, attempt: 2,
      status: "pending", execution_request_id: mappingId },
    ReintegrateTaskResult: { source_candidate_id: RESULT_CANDIDATE_ID, execution_request_id: mappingId,
      input_selection_id: nextId, release_id: mappingId, integration_operation_id: nextId,
      rework_record_id: mappingId, target_observation_id: null, status: "scheduled" },
  };
  const kind = value.original.command_type as typeof kinds[number];
  return { command_id: value.original.command_id,
    aggregate_id: kind === "RetryResultVerification" ? nextId : RESULT_ROOT_TASK_ID,
    aggregate_version: kind === "RequestTaskRework" ? 7 : kind === "RetryResultVerification" ? 1 : 8,
    event_ids: [], data: data[kind] };
}

function journal(value: NativeResultOperationDraft) {
  return { command_id: value.original.command_id, command_type: value.original.command_type,
    target_type: value.original.target_type, target_id: value.original.target_id,
    expected_version: value.original.expected_version, payload: value.original.payload,
    status: "applied", result: receipt(value) };
}

beforeEach(() => localStorage.clear());
afterEach(() => vi.restoreAllMocks());

describe("finite Worker result-operation storage", () => {
  it("encodes the full human and WorkerItem scope without scanning storage", () => {
    expect(nativeResultOperationDraftKey(scope)).toBe(`sidera.native-result-operation.v1:${RESULT_WORKSPACE_ID}:human%3Acurrent%2Fnon-uuid:${RESULT_ROOT_TASK_ID}:${workerId}`);
    expect(loadNativeResultOperationDraft(scope)).toEqual({ ok: true, draft: null });
  });

  it.each(kinds)("round trips only the public %s original and freezes nested values", (kind) => {
    const value = draft(kind);
    expect(saveNativeResultOperationDraft(scope, value)).toEqual({ ok: true });
    const loaded = loadNativeResultOperationDraft(scope);
    expect(loaded).toEqual({ ok: true, draft: value });
    if (!loaded.ok) throw new Error("Expected public draft");
    expect(Object.isFrozen(loaded.draft?.original.payload)).toBe(true);
    expect(Object.isFrozen(loaded.draft?.review.allowed_worker_item_ids)).toBe(true);
  });

  it("retains legal nullable review fields and a non-UUID human principal", () => {
    const value = draft("AuthorizeAdditionalRework");
    Object.assign(value.review, { candidate_id: null, candidate_hash: null, plan_content_hash: null, expected_fingerprint: null });
    expect(saveNativeResultOperationDraft(scope, value)).toEqual({ ok: true });
    expect(loadNativeResultOperationDraft(scope)).toEqual({ ok: true, draft: value });
  });

  it.each(kinds)("rejects private extra keys at every stored %s object boundary", (kind) => {
    for (const keys of [[], ["scope"], ["review"], ["original"], ["original", "actor"], ["original", "payload"]]) {
      const value = draft(kind);
      let object = value as unknown as Record<string, unknown>;
      for (const key of keys) object = object[key] as Record<string, unknown>;
      object.private_provider_config = "must not persist";
      expect(saveNativeResultOperationDraft(scope, value)).toMatchObject({ ok: false });
      localStorage.setItem(nativeResultOperationDraftKey(scope), JSON.stringify(value));
      expect(loadNativeResultOperationDraft(scope)).toMatchObject({ ok: false });
    }
  });

  it.each<[string, (value: NativeResultOperationDraft) => void]>([
    ["foreign principal", value => { value.original.actor.id = "other-human"; }],
    ["Worker actor", value => { value.original.actor.type = "worker"; }],
    ["foreign Wi", value => { value.scope.worker_item_id = nextId; }],
    ["foreign root", value => { value.original.target_id = nextId; }],
    ["non-resource workspace", value => { value.scope.workspace_id = "workspace"; }],
    ["unsupported command", value => { value.original.command_type = "CompleteTask"; }],
    ["unsafe version", value => { value.review.expected_version = Number.MAX_SAFE_INTEGER + 1; }],
    ["fractional result version", value => { value.review.expected_result_version = 7.5; }],
    ["different result fence", value => { value.review.expected_result_version = 8; }],
    ["different candidate", value => { value.review.candidate_id = nextId; }],
    ["wrong hash", value => { value.original.payload.candidate_hash = "b".repeat(64); }],
    ["automatic rework", value => { value.original.payload.mode = "automatic"; }],
    ["suggestion", value => { value.original.payload.suggestion = { private_token: "no" }; }],
    ["broader impact", value => { value.original.payload.impact_worker_item_ids = [workerId, nextId]; }],
    ["invalid issue", value => { value.original.payload.issue_keys = [{ private_token: "no" }]; }],
    ["invalid status", value => { value.status = "idle" as NativeResultOperationDraft["status"]; }],
  ])("rejects %s without identity, command submission, or storage overwrite", (_label, mutate) => {
    const value = draft();
    const identity = vi.spyOn(crypto, "randomUUID");
    const set = vi.spyOn(Storage.prototype, "setItem");
    mutate(value);
    expect(saveNativeResultOperationDraft(scope, value)).toMatchObject({ ok: false });
    expect(identity).not.toHaveBeenCalled();
    expect(set).not.toHaveBeenCalled();
  });

  it.each<[typeof kinds[number], Record<string, unknown>]>([
    ["AuthorizeAdditionalRework", { additional_rounds: -1 }],
    ["AuthorizeAdditionalRework", { additional_rounds: 0, additional_seconds: 0 }],
    ["AuthorizeAdditionalRework", { additional_seconds: 0.5 }],
    ["RetryResultVerification", { verification_operation_id: nextId }],
    ["RetryResultVerification", { expected_operation_version: 7 }],
    ["RetryResultVerification", { expected_result_version: 8 }],
    ["ReintegrateTaskResult", { source_candidate_id: nextId }],
    ["ReintegrateTaskResult", { replacement_result_ids: [nextId] }],
    ["ReintegrateTaskResult", { target_observation_id: "observation" }],
    ["ReintegrateTaskResult", { reason: " " }],
  ])("rejects malformed typed %s payloads", (kind, override) => {
    const value = draft(kind);
    Object.assign(value.original.payload, override);
    expect(saveNativeResultOperationDraft(scope, value)).toMatchObject({ ok: false });
  });

  it.each(["acquisition", "get", "stringify", "set"])("returns a fixed safe error for storage %s failure", (failure) => {
    const value = draft();
    const privateError = () => { throw new Error("private provider handle"); };
    if (failure === "acquisition") vi.spyOn(globalThis, "localStorage", "get").mockImplementation(privateError);
    if (failure === "get") vi.spyOn(Storage.prototype, "getItem").mockImplementation(privateError);
    if (failure === "stringify") vi.spyOn(JSON, "stringify").mockImplementation(privateError);
    if (failure === "set") vi.spyOn(Storage.prototype, "setItem").mockImplementation(privateError);
    const result = failure === "get" ? loadNativeResultOperationDraft(scope) : saveNativeResultOperationDraft(scope, value);
    expect(result).toEqual({ ok: false, reason: "本地结果操作记录暂不可用" });
  });
});

describe("actual command-specific result mappings", () => {
  it.each([
    ["RequestTaskRework", "rework_requested"], ["AuthorizeAdditionalRework", "budget_authorized"],
    ["RetryResultVerification", "verification_scheduled"], ["ReintegrateTaskResult", "integration_scheduled"],
  ] as const)("maps %s to a finite outcome without retaining response data", (kind, outcome) => {
    const value = draft(kind);
    const response = receipt(value);
    response.data.private_provider_handle = "must never be stored";
    expect(reviewNativeResultOperationResult(value, response)).toEqual({ status: "applied", mappingAvailable: true, outcome });
  });

  it("accepts legal blocked attention with unchanged head and absent target/impact arrays", () => {
    const value = draft();
    const response = { ...receipt(value), data: { rework_record_id: mappingId, request_ids: [], status: "blocked", attention_required: true, reason: "rework_owner_required" } };
    expect(reviewNativeResultOperationResult(value, response)).toEqual({ status: "applied", mappingAvailable: true, outcome: "attention_required" });
  });

  it("recognizes inspection of the original unknown verification without a new attempt", () => {
    const value = draft("RetryResultVerification");
    const response = { ...receipt(value), aggregate_id: RESULT_OPERATION_ID, aggregate_version: 3,
      data: { operation_id: RESULT_OPERATION_ID, attempt: 1, status: "unknown", inspection_required: true } };
    expect(reviewNativeResultOperationResult(value, response)).toEqual({ status: "applied", mappingAvailable: true, outcome: "verification_inspection_required" });
    expect(reviewNativeResultOperationResult(value, { ...response, aggregate_version: 4 }).mappingAvailable).toBe(false);
  });

  it.each(kinds)("keeps missing %s mappings recoverable after correlated handling", (kind) => {
    const value = draft(kind);
    expect(reviewNativeResultOperationResult(value, { ...receipt(value), data: null })).toEqual({ status: "applied", mappingAvailable: false, outcome: null });
    expect(reviewNativeResultOperationResult(value, { ...receipt(value), command_id: nextId })).toEqual({ status: "unknown", mappingAvailable: false, outcome: null });
  });

  it.each<[typeof kinds[number], Record<string, unknown>]>([
    ["RequestTaskRework", { target_worker_item_ids: [nextId] }],
    ["RequestTaskRework", { round_number: 1 }],
    ["AuthorizeAdditionalRework", { auto_seconds_limit: 0.5 }],
    ["AuthorizeAdditionalRework", { auto_deadline: "not-a-date" }],
    ["RetryResultVerification", { previous_operation_id: nextId }],
    ["RetryResultVerification", { execution_request_id: null }],
    ["RetryResultVerification", { operation_id: RESULT_OPERATION_ID }],
    ["ReintegrateTaskResult", { source_candidate_id: nextId }],
    ["ReintegrateTaskResult", { status: "succeeded" }],
    ["ReintegrateTaskResult", { target_observation_id: nextId }],
  ])("rejects foreign or malformed %s mappings", (kind, override) => {
    const value = draft(kind);
    const response = receipt(value);
    expect(reviewNativeResultOperationResult(value, { ...response, data: { ...response.data, ...override } }).mappingAvailable).toBe(false);
  });

  it("uses unchanged rework, budget plus one, advanced integration, and independent retry versions", () => {
    for (const kind of ["RequestTaskRework", "AuthorizeAdditionalRework", "ReintegrateTaskResult"] as const) {
      const value = draft(kind);
      expect(reviewNativeResultOperationResult(value, { ...receipt(value), aggregate_id: nextId }).status).toBe("unknown");
      expect(reviewNativeResultOperationResult(value, { ...receipt(value), aggregate_version: 11 }).mappingAvailable).toBe(kind === "ReintegrateTaskResult");
    }
    const retry = draft("RetryResultVerification");
    expect(reviewNativeResultOperationResult(retry, receipt(retry)).mappingAvailable).toBe(true);
  });
});

describe("original command journal fences", () => {
  it.each(kinds)("requires explicit %s metadata for pending/applied/rejected", (kind) => {
    const value = draft(kind);
    for (const status of ["pending", "applied", "rejected"]) {
      expect(nativeResultOperationStatusMatches(value, { ...journal(value), status, result: null })).toBe(true);
    }
    for (const override of [
      { command_id: nextId }, { command_type: "CompleteTask" }, { target_type: "worker_item" },
      { target_id: nextId }, { expected_version: 11 }, { expected_version: undefined },
      { payload: { ...value.original.payload, reason: "changed" } }, { status: "unknown" },
      { result: { ...receipt(value), command_id: nextId } },
    ]) expect(nativeResultOperationStatusMatches(value, { ...journal(value), ...override })).toBe(false);
  });
});
