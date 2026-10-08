import { beforeEach, describe, expect, it, vi } from "vitest";

import type { CommandRequest, CommandResponse } from "../assignments/types";
import {
  emptyNativeTransitionRecoveryDraft, freezeNativeTransitionRecoveryIntent,
  loadNativeTransitionRecoveryDraft, nativeTransitionRecoveryDraftKey,
  reviewNativeTransitionRecoveryResult, saveNativeTransitionRecoveryDraft,
  type NativeTransitionRecoveryDraft, type NativeTransitionRecoveryIntent,
} from "./nativeTransitionRecovery";

const scope = {
  workspaceId: "20000000-0000-0000-0000-000000000001",
  principalId: "operator:current", workerItemId: "40000000-0000-0000-0000-000000000001",
};
const intent: NativeTransitionRecoveryIntent = {
  workerTransitionId: "a0000000-0000-0000-0000-000000000001",
  planTransitionId: "b0000000-0000-0000-0000-000000000001",
  rootTaskId: "30000000-0000-0000-0000-000000000001", version: 17,
  action: "continue", fallbackAction: null,
  sourceExecutionId: "60000000-0000-0000-0000-000000000001",
  sourceSessionId: "70000000-0000-0000-0000-000000000001",
  currentContextVersion: 4, targetContextVersion: 5, acknowledgedContextVersion: 4,
  patchId: null, patchHash: null, patchAcknowledged: false,
  strategy: "reconcile_adapter", reason: "reviewed", evidenceRefs: ["event:stop"],
};
const original: CommandRequest = {
  command_id: "d0000000-0000-0000-0000-000000000001", workspace_id: scope.workspaceId,
  command_type: "RecoverWorkerTransition", actor: { type: "user", id: scope.principalId },
  target_type: "worker_transition", target_id: intent.workerTransitionId, expected_version: 17,
  idempotency_key: "d0000000-0000-0000-0000-000000000002",
  correlation_id: "d0000000-0000-0000-0000-000000000003",
  payload: { strategy: "reconcile_adapter", reason: "reviewed", evidence_refs: ["event:stop"] },
};
function draft(overrides: Partial<NativeTransitionRecoveryIntent> = {}): NativeTransitionRecoveryDraft {
  const selected = { ...intent, ...overrides };
  return { ...emptyNativeTransitionRecoveryDraft(scope), intent: selected, status: "pending",
    original: { ...original, payload: { strategy: selected.strategy, reason: selected.reason, evidence_refs: selected.evidenceRefs } } };
}
function result(overrides: Partial<CommandResponse> = {}): CommandResponse {
  return { command_id: original.command_id, aggregate_id: intent.workerTransitionId,
    aggregate_version: 17, event_ids: ["e0000000-0000-0000-0000-000000000001"],
    data: { worker_transition_id: intent.workerTransitionId, plan_transition_id: intent.planTransitionId,
      version: 17, action: "continue", status: "blocked_unknown", writer_state: "unknown",
      recovery_strategy: "reconcile_adapter", adapter_call: "not_started" }, ...overrides };
}

describe("native transition recovery public contract", () => {
  beforeEach(() => localStorage.clear());

  it("normalizes and freezes the reviewed finite intent without adding payload evidence", () => {
    const selected = freezeNativeTransitionRecoveryIntent({ ...intent, reason: "  reviewed  ", evidenceRefs: [" event:stop ", " "] });
    expect(selected).toEqual(intent);
    expect(Object.isFrozen(selected)).toBe(true);
    expect(Object.isFrozen(selected?.evidenceRefs)).toBe(true);
    expect(freezeNativeTransitionRecoveryIntent({ ...intent, evidenceRefs: [] })).toBeNull();
    expect(freezeNativeTransitionRecoveryIntent({ ...intent, reason: " " })).toBeNull();
  });

  it("maps reconcile only at the unchanged original version with no Adapter call", () => {
    expect(reviewNativeTransitionRecoveryResult(draft(), result())).toEqual({
      status: "applied", outcome: "recorded", aggregate_id: intent.workerTransitionId, aggregate_version: 17,
    });
    expect(reviewNativeTransitionRecoveryResult(draft(), result({ aggregate_version: 18 })).outcome).toBeNull();
    expect(reviewNativeTransitionRecoveryResult(draft(), result({ data: { ...result().data, adapter_call: "started" } })).outcome).toBeNull();
  });

  it.each(["continue", "retire"])("maps confirmed stop for %s at exactly version18", action => {
    const selected = draft({ strategy: "continue_after_confirmed_stop", action });
    expect(reviewNativeTransitionRecoveryResult(selected, result({ aggregate_version: 18,
      data: { ...result().data, version: 18, action, status: "pending", writer_state: "fenced", recovery_strategy: "continue_after_confirmed_stop" },
    })).outcome).toBe("queued");
  });

  it("maps patch absence only from patch/continue to queued continue", () => {
    const selected = draft({ strategy: "continue_after_confirmed_patch_absence", action: "patch", fallbackAction: "continue",
      patchId: "c0000000-0000-0000-0000-000000000001", patchHash: "a".repeat(64) });
    const response = result({ aggregate_version: 18, data: { ...result().data, version: 18,
      status: "pending", writer_state: "fenced", recovery_strategy: "continue_after_confirmed_patch_absence" } });
    expect(reviewNativeTransitionRecoveryResult(selected, response).outcome).toBe("queued");
    expect(reviewNativeTransitionRecoveryResult({ ...selected, intent: { ...selected.intent!, fallbackAction: "retire" } }, response).outcome).toBeNull();
    expect(reviewNativeTransitionRecoveryResult(selected, { ...response, data: { ...response.data, action: "patch" } }).outcome).toBeNull();
  });

  it("maps fail-closed only to a failed closed branch with unchanged action", () => {
    const selected = draft({ strategy: "fail_closed_branch" });
    const response = result({ aggregate_version: 18, data: { ...result().data, version: 18,
      status: "failed", writer_state: "closed", recovery_strategy: "fail_closed_branch" } });
    expect(reviewNativeTransitionRecoveryResult(selected, response).outcome).toBe("branch_closed");
    expect(reviewNativeTransitionRecoveryResult(selected, { ...response, data: { ...response.data, status: "succeeded" } }).outcome).toBeNull();
  });

  it.each([
    { aggregate_id: "a0000000-0000-0000-0000-000000000099" },
    { aggregate_version: 16 }, { data: null },
    { data: { ...result().data, worker_transition_id: "foreign" } },
    { data: { ...result().data, plan_transition_id: "foreign" } },
    { data: { ...result().data, version: 18 } },
    { data: { ...result().data, recovery_strategy: "fail_closed_branch" } },
    { data: { ...result().data, status: "converged" } },
  ])("keeps correlated but unmapped public results handled: %j", change => {
    const reviewed = reviewNativeTransitionRecoveryResult(draft(), { ...result(), ...change });
    expect(reviewed.status).toBe("applied"); expect(reviewed.outcome).toBeNull();
    expect(reviewed.aggregate_id).toBeNull(); expect(reviewed.aggregate_version).toBeNull();
  });
  it("keeps a foreign command unknown", () => {
    expect(reviewNativeTransitionRecoveryResult(draft(), result({ command_id: "foreign" })).status).toBe("unknown");
  });

  it("stores one scoped original and loads exact literal outcomes", () => {
    expect(nativeTransitionRecoveryDraftKey(scope)).toBe(`sidera.native-transition-recovery.v1:${scope.workspaceId}:operator%3Acurrent:${scope.workerItemId}`);
    for (const status of ["pending", "unknown", "applied", "rejected"] as const) {
      const selected = { ...draft(), status, rejection_code: status === "rejected" ? "command_rejected" as const : null };
      expect(saveNativeTransitionRecoveryDraft(scope, selected)).toEqual({ ok: true });
      expect(loadNativeTransitionRecoveryDraft(scope)).toEqual({ ok: true, draft: selected });
    }
    const selected = { ...draft(), ...reviewNativeTransitionRecoveryResult(draft(), result()) };
    expect(saveNativeTransitionRecoveryDraft(scope, selected)).toEqual({ ok: true });
    expect(loadNativeTransitionRecoveryDraft(scope)).toEqual({ ok: true, draft: selected });
  });

  it.each(["pending", "unknown", "applied", "rejected"])("rejects array status [%s] on load/save with writes0", status => {
    const malformed = { ...draft(), status: [status], rejection_code: status === "rejected" ? "command_rejected" : null };
    localStorage.setItem(nativeTransitionRecoveryDraftKey(scope), JSON.stringify(malformed));
    expect(loadNativeTransitionRecoveryDraft(scope)).toEqual({ ok: false, error: "invalid_draft" });
    const setItem = vi.fn();
    expect(saveNativeTransitionRecoveryDraft(scope, malformed as unknown as NativeTransitionRecoveryDraft, { setItem })).toEqual({ ok: false, error: "invalid_draft" });
    expect(setItem).not.toHaveBeenCalled();
  });

  it.each([
    { scope: { ...scope, principalId: "foreign" } },
    { original: { ...original, actor: { type: "local", id: scope.principalId } } },
    { original: { ...original, expected_version: 99 } },
    { original: { ...original, payload: { ...original.payload, operation_id: "private" } } },
    { original: { ...original, actor: { ...original.actor, grant: "private" } } },
    { intent: { ...intent, session: "private" } }, { private: "secret" },
    { outcome: ["recorded"], status: "applied", aggregate_id: intent.workerTransitionId, aggregate_version: 17 },
    { status: "rejected", rejection_code: ["command_rejected"] },
    { status: "applied", outcome: "queued", aggregate_id: intent.workerTransitionId, aggregate_version: 18 },
  ])("rejects foreign or nested private records: %j", change => {
    const malformed = { ...draft(), ...change };
    const setItem = vi.fn();
    expect(saveNativeTransitionRecoveryDraft(scope, malformed as NativeTransitionRecoveryDraft, { setItem }).ok).toBe(false);
    expect(setItem).not.toHaveBeenCalled();
    localStorage.setItem(nativeTransitionRecoveryDraftKey(scope), JSON.stringify(malformed));
    expect(loadNativeTransitionRecoveryDraft(scope).ok).toBe(false);
  });

  it("contains unavailable storage and stringify failures", () => {
    expect(loadNativeTransitionRecoveryDraft(scope, { getItem: () => { throw Error("private"); } })).toEqual({ ok: false, error: "storage_unavailable" });
    expect(saveNativeTransitionRecoveryDraft(scope, draft(), { setItem: () => { throw Error("private"); } })).toEqual({ ok: false, error: "storage_unavailable" });
    const stringify = vi.spyOn(JSON, "stringify").mockImplementationOnce(() => { throw Error("private"); });
    expect(saveNativeTransitionRecoveryDraft(scope, draft())).toEqual({ ok: false, error: "storage_unavailable" });
    stringify.mockRestore();
  });
});
