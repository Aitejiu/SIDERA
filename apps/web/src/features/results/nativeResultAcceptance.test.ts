import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { buildNativeTaskAcceptanceCommand } from "./api";
import {
  loadNativeAcceptanceDraft,
  nativeAcceptanceDraftKey,
  nativeAcceptanceStatusMatches,
  reviewNativeAcceptanceResult,
  saveNativeAcceptanceDraft,
  type NativeAcceptanceDraft,
  type NativeAcceptanceScope,
} from "./nativeResultAcceptance";
import { completeTaskInput, RESULT_CANDIDATE_ID, RESULT_ROOT_TASK_ID, RESULT_WORKSPACE_ID, resultAction } from "./testFixtures";

const scope: NativeAcceptanceScope = {
  workspace_id: RESULT_WORKSPACE_ID,
  root_task_id: RESULT_ROOT_TASK_ID,
  principal_id: "human:current/opaque",
};
const otherId = "71000000-0000-0000-0000-000000000099";

function draft(): NativeAcceptanceDraft {
  return {
    schema_version: 1,
    scope: { ...scope },
    review: {
      root_task_id: RESULT_ROOT_TASK_ID,
      task_version: 11,
      task_status: "waiting",
      result_version: 7,
      candidate_id: RESULT_CANDIDATE_ID,
      candidate_hash: "c".repeat(64),
      plan_content_hash: "d".repeat(64),
    },
    original: buildNativeTaskAcceptanceCommand(RESULT_WORKSPACE_ID, resultAction(), completeTaskInput().payload, {
      session_id: "opaque-session",
      workspace_id: RESULT_WORKSPACE_ID,
      principal_id: scope.principal_id,
      expires_at: "2099-01-01T00:00:00Z",
    }),
    status: "pending",
  };
}

function receipt(value: NativeAcceptanceDraft) {
  return {
    command_id: value.original.command_id,
    aggregate_id: RESULT_ROOT_TASK_ID,
    aggregate_version: 11,
    event_ids: [],
    data: {
      status: "accepted",
      acceptance_id: "71000000-0000-0000-0000-000000000040",
      candidate_id: RESULT_CANDIDATE_ID,
      result_version: 7,
      task_status: "waiting",
      apply_operation_id: null,
      apply_status: null,
      finalization_operation_id: "71000000-0000-0000-0000-000000000041",
      result_stage: "finalizing",
    },
  };
}

beforeEach(() => localStorage.clear());
afterEach(() => vi.restoreAllMocks());

describe("native acceptance exact public original", () => {
  it("reads only the exact encoded workspace/principal/root key without allocating identity", () => {
    const identity = vi.spyOn(crypto, "randomUUID");
    const get = vi.spyOn(Storage.prototype, "getItem");
    const key = `sidera.native-result-acceptance.v1:${RESULT_WORKSPACE_ID}:human%3Acurrent%2Fopaque:${RESULT_ROOT_TASK_ID}`;
    expect(nativeAcceptanceDraftKey(scope)).toBe(key);
    expect(loadNativeAcceptanceDraft(scope)).toEqual({ ok: true, draft: null });
    expect(get).toHaveBeenCalledExactlyOnceWith(key);
    expect(identity).not.toHaveBeenCalled();
  });

  it.each(["pending", "unknown", "applied", "rejected"] as const)("round-trips the finite %s state and original decisions", (status) => {
    const value = { ...draft(), status };
    expect(saveNativeAcceptanceDraft(scope, value)).toEqual({ ok: true });
    expect(loadNativeAcceptanceDraft(scope)).toEqual({ ok: true, draft: value });
    expect(loadNativeAcceptanceDraft({ ...scope, principal_id: "other" })).toEqual({ ok: true, draft: null });
    expect(loadNativeAcceptanceDraft({ ...scope, root_task_id: otherId })).toEqual({ ok: true, draft: null });
    expect(loadNativeAcceptanceDraft({ ...scope, workspace_id: otherId })).toEqual({ ok: true, draft: null });
    expect(localStorage.getItem(nativeAcceptanceDraftKey(scope))).not.toContain("session");
  });

  it.each<[string, (value: NativeAcceptanceDraft) => void]>([
    ["schema", value => { Object.assign(value, { schema_version: 2 }); }],
    ["status", value => { Object.assign(value, { status: "succeeded" }); }],
    ["private state object", value => { Object.assign(value, { status: { toString: () => "pending", cookie: "private" } }); }],
    ["private Task state object", value => { Object.assign(value.review, { task_status: { toString: () => "waiting", cookie: "private" } }); }],
    ["private decision object", value => { Object.assign((value.original.payload.criteria as object[])[0]!, { decision: { toString: () => "accept_passed", cookie: "private" } }); }],
    ["scope root", value => { value.scope.root_task_id = otherId; }],
    ["scope workspace", value => { value.scope.workspace_id = otherId; }],
    ["scope principal", value => { value.scope.principal_id = "other"; }],
    ["review root", value => { value.review.root_task_id = otherId; }],
    ["review task version", value => { value.review.task_version = 12; }],
    ["review result version", value => { value.review.result_version = 8; }],
    ["terminal review status", value => { value.review.task_status = "succeeded"; }],
    ["unsafe version", value => { value.review.task_version = Number.MAX_SAFE_INTEGER + 1; }],
    ["candidate UUID", value => { value.review.candidate_id = "candidate"; }],
    ["candidate hash", value => { value.review.candidate_hash = "a".repeat(40); }],
    ["plan hash", value => { value.review.plan_content_hash = "sha256:" + "d".repeat(64); }],
    ["command UUID", value => { value.original.command_id = "original"; }],
    ["command type", value => { value.original.command_type = "RequestTaskRework"; }],
    ["actor", value => { value.original.actor.type = "local"; }],
    ["actor principal", value => { value.original.actor.id = "other"; }],
    ["target type", value => { value.original.target_type = "result_operation"; }],
    ["target root", value => { value.original.target_id = otherId; }],
    ["original workspace", value => { value.original.workspace_id = otherId; }],
    ["payload candidate", value => { value.original.payload.candidate_id = otherId; }],
    ["payload hash", value => { value.original.payload.candidate_hash = "b".repeat(64); }],
    ["basis hash", value => { value.original.payload.acceptance_basis_hash = "bad"; }],
    ["payload result version", value => { value.original.payload.expected_result_version = 8; }],
    ["no criteria", value => { value.original.payload.criteria = []; }],
    ["duplicate criteria", value => { const criteria = value.original.payload.criteria as object[]; criteria.push(criteria[0]!); }],
    ["criterion decision", value => { Object.assign((value.original.payload.criteria as object[])[0]!, { decision: "accept" }); }],
    ["exception reason", value => { Object.assign((value.original.payload.criteria as object[])[0]!, { decision: "exception_accepted", reason: null }); }],
    ["criterion private extra", value => { Object.assign((value.original.payload.criteria as object[])[0]!, { token: "private" }); }],
    ["payload private extra", value => { value.original.payload.csrf = "private"; }],
    ["actor private extra", value => { Object.assign(value.original.actor, { cookie: "private" }); }],
    ["command private extra", value => { Object.assign(value.original, { native_handle: "private" }); }],
    ["review private extra", value => { Object.assign(value.review, { provider: "private" }); }],
    ["scope private extra", value => { Object.assign(value.scope, { model: "private" }); }],
    ["outer private extra", value => { Object.assign(value, { grant: "private" }); }],
  ])("rejects malformed or private %s on both load and save", (_label, mutate) => {
    const value = structuredClone(draft());
    mutate(value);
    const set = vi.spyOn(Storage.prototype, "setItem");
    expect(saveNativeAcceptanceDraft(scope, value).ok).toBe(false);
    expect(set).not.toHaveBeenCalled();
    localStorage.setItem(nativeAcceptanceDraftKey(scope), JSON.stringify(value));
    expect(loadNativeAcceptanceDraft(scope).ok).toBe(false);
  });

  it("rejects corrupt JSON and invalid scope without scanning storage", () => {
    localStorage.setItem(nativeAcceptanceDraftKey(scope), "{");
    expect(loadNativeAcceptanceDraft(scope).ok).toBe(false);
    const get = vi.spyOn(Storage.prototype, "getItem");
    expect(loadNativeAcceptanceDraft({ ...scope, principal_id: " " }).ok).toBe(false);
    expect(get).not.toHaveBeenCalled();
  });

  it("returns explicit failure when storage acquisition fails", () => {
    const value = draft();
    vi.spyOn(globalThis, "localStorage", "get").mockImplementation(() => { throw new Error("private acquisition"); });
    expect(loadNativeAcceptanceDraft(scope).ok).toBe(false);
    expect(saveNativeAcceptanceDraft(scope, value).ok).toBe(false);
  });

  it("returns explicit failures for get/stringify/set without private text", () => {
    const value = draft();
    const get = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("private get"); });
    expect(loadNativeAcceptanceDraft(scope)).toMatchObject({ ok: false });
    get.mockRestore();
    const stringify = vi.spyOn(JSON, "stringify").mockImplementation(() => { throw new Error("private stringify"); });
    expect(saveNativeAcceptanceDraft(scope, value)).toMatchObject({ ok: false });
    stringify.mockRestore();
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("private set"); });
    expect(saveNativeAcceptanceDraft(scope, value)).toMatchObject({ ok: false });
  });
});

describe("native acceptance intent receipt mapping", () => {
  it("maps the original result version 7 and unchanged waiting Task version 11", () => {
    const value = draft();
    expect(reviewNativeAcceptanceResult(value, receipt(value))).toEqual({ status: "applied", mappingAvailable: true });
    expect(reviewNativeAcceptanceResult(value, {
      ...receipt(value),
      data: { ...receipt(value).data, result_stage: "applying", apply_operation_id: otherId, apply_status: "pending", finalization_operation_id: null },
    })).toEqual({ status: "applied", mappingAvailable: true });
  });

  it.each(["pending", "ready", "running"])("requires the await_acceptance increment for an original %s Task", taskStatus => {
    const value = { ...draft(), review: { ...draft().review, task_status: taskStatus } };
    expect(reviewNativeAcceptanceResult(value, { ...receipt(value), aggregate_version: 12 })).toEqual({ status: "applied", mappingAvailable: true });
    expect(reviewNativeAcceptanceResult(value, receipt(value))).toEqual({ status: "unknown", mappingAvailable: false });
  });

  it("retains correlated application when the result mapping is unavailable", () => {
    const value = draft();
    const response = receipt(value);
    const withoutMapping = { command_id: response.command_id, aggregate_id: response.aggregate_id, aggregate_version: response.aggregate_version, event_ids: response.event_ids };
    expect(reviewNativeAcceptanceResult(value, withoutMapping)).toEqual({ status: "applied", mappingAvailable: false });
    expect(reviewNativeAcceptanceResult(value, { ...response, data: { ...response.data, result_version: 8 } })).toEqual({ status: "applied", mappingAvailable: false });
  });

  it.each(["command_id", "aggregate_id", "aggregate_version"])("does not accept another %s", field => {
    const value = draft();
    expect(reviewNativeAcceptanceResult(value, { ...receipt(value), [field]: field === "aggregate_version" ? 12 : otherId })).toEqual({ status: "unknown", mappingAvailable: false });
  });

  it.each([
    { candidate_id: otherId }, { acceptance_id: "acceptance" }, { task_status: "succeeded" },
    { result_stage: "completed" }, { apply_operation_id: otherId }, { finalization_operation_id: null },
    { apply_status: "succeeded" }, { result_version: 8 },
  ])("does not treat malformed, foreign, or finished mapping as a valid acceptance receipt", extra => {
    const value = draft();
    expect(reviewNativeAcceptanceResult(value, { ...receipt(value), data: { ...receipt(value).data, ...extra } }).mappingAvailable).toBe(false);
  });

  it.each(["pending", "applied", "rejected"])("requires explicit %s journal state and original metadata", status => {
    const value = draft();
    const journal = {
      command_id: value.original.command_id, command_type: "CompleteTask", target_type: "task",
      target_id: RESULT_ROOT_TASK_ID, expected_version: 11, payload: value.original.payload, status, result: null,
    };
    expect(nativeAcceptanceStatusMatches(value, journal)).toBe(true);
    for (const extra of [{ command_id: otherId }, { command_type: null }, { target_id: otherId }, { expected_version: 12 }, { status: "unknown" }, { status: "rejected-private" }, { status: { toString: () => "rejected", cookie: "private" } }, { payload: { ...value.original.payload, expected_result_version: 8 } }]) {
      expect(nativeAcceptanceStatusMatches(value, { ...journal, ...extra })).toBe(false);
    }
    expect(nativeAcceptanceStatusMatches(value, { ...journal, result: { ...receipt(value), command_id: otherId } })).toBe(false);
  });
});
