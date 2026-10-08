import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";

import type { CommandRequest } from "../assignments/types";
import {
  emptyNativeAttentionDraft, freezeNativeAttentionIntent, loadNativeAttentionDraft,
  nativeAttentionDraftKey, nativeAttentionPayload, reviewNativeAttentionJournal,
  reviewNativeAttentionResult, saveNativeAttentionDraft, unresolvedNativeAttentionDraft,
  type NativeAttentionDraft, type NativeAttentionScope,
} from "./nativeAttentionAcknowledgement";

const scope: NativeAttentionScope = {
  workspaceId: "20000000-0000-0000-0000-000000000001",
  rootTaskId: "30000000-0000-0000-0000-000000000001", principalId: "operator",
};
const intent = {
  rootTaskVersion: 11, attentionKey: "blocked:worker", sourceResourceVersion: 3,
  sourceCorrelationId: "50000000-0000-0000-0000-000000000001",
};
const original: CommandRequest = {
  command_id: "60000000-0000-0000-0000-000000000001", workspace_id: scope.workspaceId,
  command_type: "AcknowledgeAttentionItem", actor: { type: "user", id: "operator" },
  target_type: "task", target_id: scope.rootTaskId, expected_version: 11,
  idempotency_key: "same-original", correlation_id: "70000000-0000-0000-0000-000000000001",
  payload: { attention_key: "blocked:worker", source_correlation_id: intent.sourceCorrelationId, source_resource_version: 3 },
};
const result = { command_id: original.command_id, aggregate_id: scope.rootTaskId, aggregate_version: 11, event_ids: [], data: {} };
const pending = (): NativeAttentionDraft => ({ ...emptyNativeAttentionDraft(scope), intent, original, status: "pending" });
const journal = () => ({
  command_id: original.command_id, command_type: original.command_type, target_type: "task",
  target_id: scope.rootTaskId, expected_version: 11, payload: original.payload, status: "applied", result,
});

describe("native attention bounded draft and exact result", () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => vi.restoreAllMocks());

  it("maps acknowledgement at the unchanged Task version with empty events and data", () => {
    expect(reviewNativeAttentionResult(original, result)).toEqual({ status: "applied", outcome: "acknowledged", rejection_code: null });
    expect(reviewNativeAttentionResult(original, { ...result, data: undefined, event_ids: [intent.sourceCorrelationId] }).outcome).toBe("acknowledged");
  });

  it.each([
    { command_id: intent.sourceCorrelationId }, { aggregate_id: intent.sourceCorrelationId },
    { aggregate_version: 12 }, { aggregate_version: 0 }, { aggregate_version: "11" },
    { event_ids: ["not-a-uuid"] }, { event_ids: {} }, { data: [] },
  ])("leaves an illegal or uncorrelated result unresolved: %j", (change) => {
    expect(reviewNativeAttentionResult(original, { ...result, ...change })).toEqual({ status: "unknown", outcome: null, rejection_code: null });
  });

  it("captures only the public intent and rejects proposals or malformed facts", () => {
    const item = { key: intent.attentionKey, correlation_id: intent.sourceCorrelationId, resource_version: 3, kind: "blocked", reason: "private detail" };
    expect(freezeNativeAttentionIntent(item, 11)).toEqual(intent);
    expect(nativeAttentionPayload(intent)).toEqual(original.payload);
    expect(freezeNativeAttentionIntent({ ...item, kind: "collaboration_release_proposal" }, 11)).toBeNull();
    expect(freezeNativeAttentionIntent(item, undefined)).toBeNull();
    expect(freezeNativeAttentionIntent({ ...item, correlation_id: "bad" }, 11)).toBeNull();
    expect(freezeNativeAttentionIntent({ ...item, resource_version: 0 }, 11)).toBeNull();
  });

  it("uses one encoded slot per workspace, human and root without scanning", () => {
    const encoded = { ...scope, principalId: "human:operator/1" };
    expect(nativeAttentionDraftKey(encoded)).toBe(`sidera.native-attention.v1:${scope.workspaceId}:human%3Aoperator%2F1:${scope.rootTaskId}`);
    const scan = vi.spyOn(Storage.prototype, "key");
    expect(loadNativeAttentionDraft(scope)).toEqual({ ok: true, draft: emptyNativeAttentionDraft(scope) });
    expect(scan).not.toHaveBeenCalled();
  });

  it.each(["idle", "pending", "unknown", "applied", "rejected"] as const)("round-trips literal %s and freezes its original", (status) => {
    const draft = status === "idle" ? emptyNativeAttentionDraft(scope) : {
      ...pending(), status, outcome: status === "applied" ? "acknowledged" as const : null,
      rejection_code: status === "rejected" ? "command_rejected" as const : null,
    };
    expect(saveNativeAttentionDraft(scope, draft)).toEqual({ ok: true });
    const loaded = loadNativeAttentionDraft(scope);
    expect(loaded).toEqual({ ok: true, draft });
    if (loaded.ok) {
      expect(Object.isFrozen(loaded.draft)).toBe(true);
      if (loaded.draft.original) expect(Object.isFrozen(loaded.draft.original.payload)).toBe(true);
    }
    expect(localStorage.length).toBe(1);
  });

  it.each(["pending", "unknown", "applied", "rejected"])("rejects array status [%s] on load and save with zero writes", (status) => {
    const draft = { ...pending(), status: [status] };
    localStorage.setItem(nativeAttentionDraftKey(scope), JSON.stringify(draft));
    expect(loadNativeAttentionDraft(scope).ok).toBe(false);
    const writes = vi.spyOn(Storage.prototype, "setItem");
    expect(saveNativeAttentionDraft(scope, draft as unknown as NativeAttentionDraft).ok).toBe(false);
    expect(writes).not.toHaveBeenCalled();
  });

  it.each([
    ["outcome", ["acknowledged"]], ["rejection_code", ["command_rejected"]],
    ["outcome", "completed"], ["rejection_code", "private-message"],
  ])("rejects nonliteral or unsupported %s tags", (key, value) => {
    const draft = { ...pending(), [key as string]: value };
    localStorage.setItem(nativeAttentionDraftKey(scope), JSON.stringify(draft));
    const writes = vi.spyOn(Storage.prototype, "setItem");
    expect(loadNativeAttentionDraft(scope).ok).toBe(false);
    expect(saveNativeAttentionDraft(scope, draft as NativeAttentionDraft).ok).toBe(false);
    expect(writes).not.toHaveBeenCalled();
  });

  it.each(["outer", "scope", "intent", "original", "actor", "payload"])("rejects private extras in %s", (part) => {
    const draft = structuredClone(pending());
    const target = part === "outer" ? draft : part === "actor" ? draft.original!.actor
      : part === "payload" ? draft.original!.payload : draft[part as "scope" | "intent" | "original"];
    Object.assign(target!, { secret: "must-not-persist" });
    localStorage.setItem(nativeAttentionDraftKey(scope), JSON.stringify(draft));
    expect(loadNativeAttentionDraft(scope).ok).toBe(false);
    const writes = vi.spyOn(Storage.prototype, "setItem");
    expect(saveNativeAttentionDraft(scope, draft).ok).toBe(false);
    expect(writes).not.toHaveBeenCalled();
  });

  it.each([
    { actor: { type: "local", id: "operator" } }, { actor: { type: "user", id: "foreign" } },
    { workspace_id: intent.sourceCorrelationId }, { target_id: intent.sourceCorrelationId },
    { target_type: "worker_item" }, { command_type: "CompleteTask" }, { expected_version: 12 },
    { command_id: "bad" }, { correlation_id: "bad" }, { idempotency_key: "" },
    { payload: { ...original.payload, source_resource_version: 4 } },
  ])("fails closed on a malformed or foreign original: %j", (change) => {
    const draft = { ...pending(), original: { ...original, ...change } } as NativeAttentionDraft;
    localStorage.setItem(nativeAttentionDraftKey(scope), JSON.stringify(draft));
    expect(loadNativeAttentionDraft(scope).ok).toBe(false);
    const writes = vi.spyOn(Storage.prototype, "setItem");
    expect(saveNativeAttentionDraft(scope, draft).ok).toBe(false);
    expect(writes).not.toHaveBeenCalled();
  });

  it.each([0, -1, 1.5, Infinity, NaN])("rejects nonpositive or nonfinite versions %s", (version) => {
    const draft = { ...pending(), intent: { ...intent, rootTaskVersion: version } };
    expect(saveNativeAttentionDraft(scope, draft).ok).toBe(false);
    const source = { ...pending(), intent: { ...intent, sourceResourceVersion: version } };
    expect(saveNativeAttentionDraft(scope, source).ok).toBe(false);
  });

  it("isolates a foreign scope and rejects inconsistent status relationships", () => {
    expect(saveNativeAttentionDraft({ ...scope, principalId: "other" }, pending()).ok).toBe(false);
    expect(saveNativeAttentionDraft(scope, { ...pending(), status: "idle" }).ok).toBe(false);
    expect(saveNativeAttentionDraft(scope, { ...pending(), outcome: "acknowledged" }).ok).toBe(false);
    expect(saveNativeAttentionDraft(scope, { ...pending(), status: "rejected" }).ok).toBe(false);
    expect(saveNativeAttentionDraft(scope, { ...emptyNativeAttentionDraft(scope), status: "unknown" }).ok).toBe(false);
  });

  it("blocks unresolved originals and allows a fresh explicit click only after mapping or rejection", () => {
    expect(unresolvedNativeAttentionDraft(pending())).toBe(true);
    expect(unresolvedNativeAttentionDraft({ ...pending(), status: "unknown" })).toBe(true);
    expect(unresolvedNativeAttentionDraft({ ...pending(), status: "applied" })).toBe(true);
    expect(unresolvedNativeAttentionDraft({ ...pending(), status: "applied", outcome: "acknowledged" })).toBe(false);
    expect(unresolvedNativeAttentionDraft({ ...pending(), status: "rejected", rejection_code: "command_rejected" })).toBe(false);
  });

  it("requires correlated explicit terminal journal metadata and keeps applied-null unmapped", () => {
    expect(reviewNativeAttentionJournal(original, journal())?.outcome).toBe("acknowledged");
    expect(reviewNativeAttentionJournal(original, { ...journal(), result: null })).toEqual({ status: "applied", outcome: null, rejection_code: null });
    expect(reviewNativeAttentionJournal(original, { ...journal(), status: "rejected", result: null, error_code: "private-error" })).toEqual({ status: "rejected", outcome: null, rejection_code: "command_rejected" });
    for (const change of [
      { status: "pending", result: null }, { status: ["applied"] }, { status: undefined },
      { command_id: intent.sourceCorrelationId }, { command_type: undefined }, { target_type: undefined },
      { target_id: undefined }, { expected_version: undefined }, { expected_version: 12 },
      { payload: { ...original.payload, attention_key: "other" } }, { result: { ...result, aggregate_version: 12 } },
    ]) expect(reviewNativeAttentionJournal(original, { ...journal(), ...change })).toBeNull();
  });

  it("returns finite safe storage errors when access or serialization throws", () => {
    const get = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("private read detail"); });
    expect(loadNativeAttentionDraft(scope)).toEqual({ ok: false, error: "storage_unavailable" });
    get.mockRestore();
    const set = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("private write detail"); });
    expect(saveNativeAttentionDraft(scope, pending())).toEqual({ ok: false, error: "storage_unavailable" });
    set.mockRestore();
    vi.spyOn(JSON, "stringify").mockImplementation(() => { throw new Error("private stringify detail"); });
    expect(saveNativeAttentionDraft(scope, pending())).toEqual({ ok: false, error: "storage_unavailable" });
  });
});
