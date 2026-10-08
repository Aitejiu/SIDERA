import { beforeEach, describe, expect, it, vi } from "vitest";

import type { CommandRequest } from "../assignments/types";
import {
  buildNativeInboxOriginal, emptyNativeInboxDraft, freezeNativeInboxIntent, loadNativeInboxDraft,
  nativeInboxDraftKey, reviewNativeInboxJournal, reviewNativeInboxResult, saveNativeInboxDraft,
  unresolvedNativeInboxDraft, type NativeInboxDraft, type NativeInboxIntent,
} from "./nativeInboxCommand";

const WS = "20000000-0000-0000-0000-000000000001";
const THREAD = "60000000-0000-0000-0000-000000000001";
const FACT = "70000000-0000-0000-0000-000000000001";
const OLD = "90000000-0000-0000-0000-000000000001";
const NEXT = "90000000-0000-0000-0000-000000000002";
const ROUTE = "a0000000-0000-0000-0000-000000000001";
const ID = "80000000-0000-0000-0000-000000000001";
const scope = { workspaceId: WS, principalId: "operator:current" };
const base = { threadId: THREAD, threadVersion: 4, latestFactId: FACT };
const read: NativeInboxIntent = { ...base, kind: "mark_read" };
const archive: NativeInboxIntent = { ...base, kind: "archive" };
const requeue: NativeInboxIntent = { ...base, kind: "requeue", deliveryId: OLD, routeId: ROUTE, routeVersion: 3, reason: "Channel restored" };
function draft(intent: NativeInboxIntent = read): NativeInboxDraft {
  const original: CommandRequest = {
    command_id: ID, workspace_id: WS, actor: { type: "user", id: scope.principalId },
    correlation_id: "80000000-0000-0000-0000-000000000002", idempotency_key: "80000000-0000-0000-0000-000000000003",
    command_type: intent.kind === "mark_read" ? "MarkNotificationThreadRead" : intent.kind === "archive" ? "ArchiveNotificationThread" : "RequeueNotificationDelivery",
    target_type: intent.kind === "requeue" ? "notification_delivery" : "notification_thread",
    target_id: intent.kind === "requeue" ? OLD : THREAD, expected_version: intent.kind === "requeue" ? null : 4,
    payload: intent.kind === "mark_read" ? { expected_latest_fact_id: FACT } : intent.kind === "archive" ? {} : { expected_route_version: 3, expected_status: "exhausted", reason: "Channel restored" },
  };
  return { ...emptyNativeInboxDraft(scope), intent, original, status: "pending" };
}
function result(intent: NativeInboxIntent = read, version = 4) {
  return {
    command_id: ID, aggregate_id: intent.kind === "requeue" ? NEXT : THREAD,
    aggregate_version: intent.kind === "requeue" ? 1 : version, event_ids: [],
    data: intent.kind === "requeue" ? { notification_delivery_id: NEXT, previous_delivery_id: OLD, route_version: 3, delivery_round: 2, fencing_token: 1, delivery_status: "pending", operation_fact_id: FACT }
      : { notification_thread_id: THREAD, thread_version: version, latest_fact_id: FACT, organization_status: "archived" },
  };
}

beforeEach(() => { localStorage.clear(); vi.restoreAllMocks(); });
describe("native Inbox immutable contracts", () => {
  it.each([read, archive, requeue])("builds only the exact current user command for $kind", intent => {
    const original = buildNativeInboxOriginal(scope, intent, { session_id: "human-session", workspace_id: WS, principal_id: scope.principalId, expires_at: "2099-01-01T00:00:00Z" });
    expect(original.actor).toEqual({ type: "user", id: "operator:current" });
    expect(original.payload).toEqual(draft(intent).original!.payload);
    expect(original.target_id).toBe(intent.kind === "requeue" ? OLD : THREAD);
    expect(original.expected_version).toBe(intent.kind === "requeue" ? null : 4);
    expect(Object.isFrozen(original)).toBe(true);
    expect(Object.isFrozen(original.payload)).toBe(true);
  });
  it("normalizes a bounded reason without admitting unknown input fields", () => {
    const frozen = freezeNativeInboxIntent({ ...requeue, reason: "  Channel restored  " });
    expect(frozen?.kind === "requeue" && frozen.reason).toBe("Channel restored");
    expect(Object.isFrozen(frozen)).toBe(true);
    expect(freezeNativeInboxIntent({ ...read, secret: "private" })).toBeNull();
  });
  it.each(["", "x".repeat(257), "bearer private", "bad\u0001reason"])("rejects unsafe reason %s before identity", reason => {
    const identity = vi.spyOn(crypto, "randomUUID");
    expect(freezeNativeInboxIntent({ ...requeue, reason })).toBeNull();
    expect(identity).not.toHaveBeenCalled();
  });
  it.each([0, -1, Infinity, NaN, 1.5])("rejects invalid thread versions %s", threadVersion => {
    expect(freezeNativeInboxIntent({ ...read, threadVersion })).toBeNull();
  });
  it("denies a foreign principal before identity allocation", () => {
    const identity = vi.spyOn(crypto, "randomUUID");
    expect(() => buildNativeInboxOriginal(scope, read, { session_id: "s", workspace_id: WS, principal_id: "foreign", expires_at: "2099-01-01T00:00:00Z" })).toThrow();
    expect(identity).not.toHaveBeenCalled();
  });
  it.each([read, archive])("maps unchanged and incremented $kind versions with no events", intent => {
    for (const version of [4, 5]) {
      const next = reviewNativeInboxResult(draft(intent), result(intent, version));
      expect(next.status).toBe("applied");
      expect(next.outcome).toMatchObject({ kind: intent.kind, threadId: THREAD, threadVersion: version });
    }
  });
  it("maps the NEW pending delivery and its fencing token, preserving route 3", () => {
    const next = reviewNativeInboxResult(draft(requeue), result(requeue));
    expect(next.outcome).toEqual({ kind: "requeue", deliveryId: NEXT, previousDeliveryId: OLD, routeVersion: 3, deliveryRound: 2, fencingToken: 1, deliveryStatus: "pending" });
    expect(next.original?.target_id).toBe(OLD);
  });
  it.each([
    { command_id: NEXT }, { aggregate_id: NEXT }, { aggregate_version: 6 },
    { data: { notification_thread_id: THREAD, thread_version: 4, latest_fact_id: NEXT } },
    { data: { notification_thread_id: NEXT, thread_version: 4, latest_fact_id: FACT } },
    { data: { notification_thread_id: THREAD, thread_version: 5, latest_fact_id: FACT } },
  ])("does not map an uncorrelated or illegal read result %j", patch => {
    const next = reviewNativeInboxResult(draft(), { ...result(), ...patch });
    expect(next.outcome).toBeNull();
    expect(unresolvedNativeInboxDraft(next)).toBe(true);
  });
  it.each([
    { previous_delivery_id: NEXT }, { notification_delivery_id: OLD }, { route_version: 1 },
    { delivery_status: "queued" }, { delivery_status: "delivered" }, { fencing_token: 0 }, { delivery_round: 0 },
  ])("does not map illegal requeue facts %j", patch => {
    const response = result(requeue);
    expect(reviewNativeInboxResult(draft(requeue), { ...response, data: { ...response.data, ...patch } }).outcome).toBeNull();
  });
  it("does not map a fabricated archive status", () => {
    const response = result(archive);
    expect(reviewNativeInboxResult(draft(archive), { ...response, data: { ...response.data, organization_status: "active" } }).outcome).toBeNull();
  });
  it("keeps correlated applied-null recoverable and bounds rejection codes", () => {
    const next = reviewNativeInboxJournal(draft(), { command_id: ID, status: "applied", result: null });
    expect(next?.status).toBe("applied");
    expect(next?.outcome).toBeNull();
    expect(unresolvedNativeInboxDraft(next!)).toBe(true);
    expect(reviewNativeInboxJournal(draft(), { command_id: ID, status: "rejected", result: null, error_code: "private server response" })?.rejection_code).toBe("command_rejected");
  });
  it.each([{ command_id: NEXT }, { target_id: NEXT }, { expected_version: 99 }, { status: ["applied"] }])("rejects journal mismatch %j", patch => {
    expect(reviewNativeInboxJournal(draft(), { command_id: ID, status: "applied", result: result(), ...patch })).toBeNull();
  });
});

describe("strict one-slot persistence", () => {
  it.each([read, archive, requeue])("roundtrips a frozen original for $kind in the exact encoded slot", intent => {
    const value = draft(intent);
    expect(nativeInboxDraftKey(scope)).toBe(`sidera.native-inbox.v1:${WS}:operator%3Acurrent`);
    expect(saveNativeInboxDraft(scope, value)).toEqual({ ok: true });
    expect(localStorage.length).toBe(1);
    const loaded = loadNativeInboxDraft(scope);
    expect(loaded).toEqual({ ok: true, draft: value });
    if (loaded.ok) expect(Object.isFrozen(loaded.draft.original?.payload)).toBe(true);
  });
  it.each(["pending", "unknown", "applied", "rejected"])("rejects array durable status [%s] in load AND save with writes0", status => {
    const invalid = { ...draft(), status: [status] };
    localStorage.setItem(nativeInboxDraftKey(scope), JSON.stringify(invalid));
    expect(loadNativeInboxDraft(scope).ok).toBe(false);
    const writes = vi.spyOn(Storage.prototype, "setItem");
    expect(saveNativeInboxDraft(scope, invalid as unknown as NativeInboxDraft).ok).toBe(false);
    expect(writes).not.toHaveBeenCalled();
  });
  it.each([
    (value: NativeInboxDraft) => ({ ...value, scope: { ...scope, principalId: "foreign" } }),
    (value: NativeInboxDraft) => ({ ...value, original: { ...value.original, actor: { type: "local", id: "aitejiu" } } }),
    (value: NativeInboxDraft) => ({ ...value, original: { ...value.original, payload: { expected_latest_fact_id: FACT, session: { cookie: "private" } } } }),
    (value: NativeInboxDraft) => ({ ...value, intent: { ...value.intent, route: { source_ref: "private" } } }),
    (value: NativeInboxDraft) => ({ ...value, status: "rejected", rejection_code: ["version_conflict"] }),
    (value: NativeInboxDraft) => ({ ...value, status: "applied", outcome: { kind: ["mark_read"], threadId: THREAD, threadVersion: 4, latestFactId: FACT } }),
    (value: NativeInboxDraft) => ({ ...value, status: "applied", outcome: { kind: "mark_read", threadId: NEXT, threadVersion: 4, latestFactId: FACT } }),
    (value: NativeInboxDraft) => ({ ...value, status: "pending", outcome: { kind: "archive" } }),
  ])("fails closed for foreign/private or inconsistent durable data %#", change => {
    const value = change(draft());
    localStorage.setItem(nativeInboxDraftKey(scope), JSON.stringify(value));
    expect(loadNativeInboxDraft(scope).ok).toBe(false);
    const writes = vi.spyOn(Storage.prototype, "setItem");
    expect(saveNativeInboxDraft(scope, value as unknown as NativeInboxDraft).ok).toBe(false);
    expect(writes).not.toHaveBeenCalled();
  });
  it.each([read, archive, requeue])("roundtrips only the correlated mapped $kind outcome", intent => {
    const value = reviewNativeInboxResult(draft(intent), result(intent));
    expect(saveNativeInboxDraft(scope, value).ok).toBe(true);
    expect(loadNativeInboxDraft(scope)).toEqual({ ok: true, draft: value });
  });
  it("catches storage getter/read/write failures and never scans other slots", () => {
    const keys = vi.spyOn(Storage.prototype, "key");
    const reads = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("private"); });
    expect(loadNativeInboxDraft(scope)).toEqual({ ok: false, error: "unavailable" });
    reads.mockRestore();
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("private"); });
    expect(saveNativeInboxDraft(scope, draft())).toEqual({ ok: false, error: "unavailable" });
    expect(keys).not.toHaveBeenCalled();
  });
});
