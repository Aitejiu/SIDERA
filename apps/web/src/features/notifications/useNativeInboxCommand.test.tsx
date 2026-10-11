import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { server } from "@/test/server";
import type { CommandRequest } from "../assignments/types";
import type { NotificationThreadDetail } from "./api";
import { nativeInboxDraftKey, type NativeInboxIntent } from "./nativeInboxCommand";
import { useNativeInboxCommand } from "./useNativeInboxCommand";

const WS = "20000000-0000-0000-0000-000000000001";
const OTHER = "20000000-0000-0000-0000-000000000002";
const THREAD = "60000000-0000-0000-0000-000000000001";
const FACT = "70000000-0000-0000-0000-000000000001";
const OLD = "90000000-0000-0000-0000-000000000001";
const NEXT = "90000000-0000-0000-0000-000000000002";
const ROUTE = "a0000000-0000-0000-0000-000000000001";
const SESSION = { session_id: "human-current", workspace_id: WS, principal_id: "operator:current", expires_at: "2099-01-01T00:00:00Z" };
const scope = { workspaceId: WS, principalId: SESSION.principal_id };
const read: NativeInboxIntent = { kind: "mark_read", threadId: THREAD, threadVersion: 4, latestFactId: FACT };
const archive: NativeInboxIntent = { ...read, kind: "archive" };
const requeue: NativeInboxIntent = { ...read, kind: "requeue", deliveryId: OLD, routeId: ROUTE, routeVersion: 3, reason: "Channel restored" };
function detail(): NotificationThreadDetail {
  return {
    thread: { id: THREAD, workspace_id: WS, recipient_type: "workspace_operator", recipient_id: "local", category: "action",
      business_status: "resolved", organization_status: "active", priority: "high", root_task_id: "30000000-0000-0000-0000-000000000001", root_task_title: "Root",
      subtask_id: null, subtask_title: null, worker_item_id: null, worker_item_title: null, title: "Inbox thread", summary: "Channel issue",
      action: "Review channel", topic: "notification.delivery", notification_ref: "notification-ref", latest_fact_id: FACT,
      latest_fact_occurred_at: "2026-10-07T08:30:00Z", last_read_fact_id: null, unread_count: 1, version: 4, deep_link: "",
      opened_at: "2026-10-07T08:00:00Z", resolved_at: "2026-10-07T08:30:00Z", archived_at: null, updated_at: "2026-10-07T08:30:00Z" },
    facts: { items: [], next_cursor: null }, deliveries: [{ delivery_id: OLD, route_id: ROUTE, route_version: 3, channel_type: "local",
      contract_version: 1, status: "exhausted", attempts: 10, next_attempt_at: "2026-10-07T09:00:00Z", last_error_code: "provider_unavailable",
      delivered_at: null, exhausted_at: "2026-10-07T09:00:00Z", created_at: "2026-10-07T08:30:00Z", updated_at: "2026-10-07T09:00:00Z" }],
    notification_ref: "notification-ref", available_commands: ["mark_read", "archive"], deep_link: "", last_change_cursor: 42,
  };
}
function legal(command: CommandRequest) {
  const isRequeue = command.command_type === "RequeueNotificationDelivery";
  return { command_id: command.command_id, aggregate_id: isRequeue ? NEXT : THREAD, aggregate_version: isRequeue ? 2 : 4, event_ids: [],
    data: isRequeue ? { notification_delivery_id: NEXT, previous_delivery_id: OLD, route_version: 3, delivery_round: 2, delivery_status: "pending", fencing_token: 2 }
      : { notification_thread_id: THREAD, thread_version: 4, latest_fact_id: FACT, organization_status: "archived" } };
}
function deferred() { let release!: () => void; const promise = new Promise<void>(resolve => { release = resolve; }); return { promise, release }; }
function handlers() {
  const world = { health: { status: "ok", service: "sidera-product" }, session: { ...SESSION }, detail: detail(),
    lost: false, detailFails: false, sessionFails: false, appliedNull: false,
    readGate: null as ReturnType<typeof deferred> | null, postGate: null as ReturnType<typeof deferred> | null };
  const counts = { health: 0, session: 0, detail: 0, post: 0, lookup: 0 };
  const posted: CommandRequest[] = [], lookups: string[] = [], unexpected: string[] = [];
  const pendingOnPost: unknown[] = [], csrf: (string | null)[] = [];
  server.use(
    http.get("/main-agent/v1/health", () => { counts.health++; return HttpResponse.json(world.health); }),
    http.get("/product/v1/session", () => { counts.session++; return world.sessionFails ? new HttpResponse(null, { status: 401 }) : HttpResponse.json(world.session); }),
    http.get("/api/v1/workspaces/:workspace/notification-threads/:thread", async () => {
      counts.detail++; const captured = structuredClone(world.detail); if (world.readGate) await world.readGate.promise;
      return world.detailFails ? new HttpResponse(null, { status: 503 }) : HttpResponse.json(captured);
    }),
    http.post("/api/v1/commands", async ({ request }) => {
      counts.post++; const command = await request.json() as CommandRequest; posted.push(command); csrf.push(request.headers.get("X-Sidera-CSRF"));
      pendingOnPost.push(JSON.parse(localStorage.getItem(nativeInboxDraftKey({ workspaceId: command.workspace_id, principalId: command.actor.id })) ?? "null"));
      if (world.postGate) await world.postGate.promise;
      return world.lost ? HttpResponse.error() : HttpResponse.json(world.appliedNull ? { ...legal(command), data: {} } : legal(command));
    }),
    http.get("/api/v1/workspaces/:workspace/commands/:command", ({ params }) => {
      counts.lookup++; lookups.push(String(params.command));
      const command = posted.find(item => item.command_id === params.command)!;
      return HttpResponse.json({ command_id: command.command_id, command_type: command.command_type, target_id: command.target_id,
        target_type: command.target_type, expected_version: command.expected_version, status: "applied", result: world.appliedNull ? null : legal(command) });
    }),
    http.all("*", ({ request }) => { unexpected.push(`${request.method} ${new URL(request.url).pathname}`); return new HttpResponse(null, { status: 404 }); }),
  );
  return { world, counts, posted, lookups, unexpected, pendingOnPost, csrf };
}
function mount(workspaceId = WS, onRefresh = vi.fn(async () => {})) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
  const hook = renderHook(props => useNativeInboxCommand(props), { initialProps: { workspaceId, onRefresh },
    wrapper: ({ children }: { children: ReactNode }) => <QueryClientProvider client={queryClient}>{children}</QueryClientProvider> });
  return { ...hook, queryClient, onRefresh };
}
type Hook = ReturnType<typeof mount>;
async function ready(hook: Hook) { await waitFor(() => expect(hook.result.current.authorized).toBe(true)); }
async function perform(hook: Hook, intent: NativeInboxIntent = read) { await act(async () => { await hook.result.current.perform(intent); }); }
beforeEach(() => { localStorage.clear(); document.cookie = "sidera_csrf=inbox-current-csrf; path=/"; });
afterEach(() => { vi.restoreAllMocks(); document.cookie = "sidera_csrf=; Max-Age=0; path=/"; });

describe("native Inbox single owner", () => {
  it.each([read, archive, requeue])("fresh checks and saves the exact $kind original before sole POST", async intent => {
    const h = handlers(), hook = mount(); await ready(hook); await perform(hook, intent);
    expect(h.counts).toEqual({ health: 2, session: 2, detail: 1, post: 1, lookup: 0 });
    expect(h.posted[0]?.actor).toEqual({ type: "user", id: "operator:current" });
    expect(h.posted[0]?.target_id).toBe(intent.kind === "requeue" ? OLD : THREAD);
    expect(h.posted[0]?.expected_version).toBe(intent.kind === "requeue" ? null : 4);
    expect(h.posted[0]?.payload).toEqual(intent.kind === "mark_read" ? { expected_latest_fact_id: FACT } : intent.kind === "archive" ? {} : { expected_route_version: 3, expected_status: "exhausted", reason: "Channel restored" });
    expect(h.csrf).toEqual(["inbox-current-csrf"]);
    expect(h.pendingOnPost[0]).toMatchObject({ status: "pending", original: h.posted[0] });
    expect(hook.result.current.state?.outcome?.kind).toBe(intent.kind);
    expect(h.unexpected).toEqual([]);
  });
  it("locks same-tick duplicate submissions and all unresolved threads", async () => {
    const h = handlers(), hook = mount(); await ready(hook); h.world.lost = true;
    await act(async () => { await Promise.all([hook.result.current.perform(read), hook.result.current.perform(read)]); });
    expect(h.counts.post).toBe(1); expect(hook.result.current.canPerform).toBe(false);
    await perform(hook, { ...archive, threadId: NEXT }); expect(h.counts.post).toBe(1);
  });
  it.each([
    ["workspace", (d: NotificationThreadDetail) => { d.thread.workspace_id = OTHER; }, read],
    ["thread", (d: NotificationThreadDetail) => { d.thread.id = NEXT; }, read],
    ["version", (d: NotificationThreadDetail) => { d.thread.version = 5; }, read],
    ["fact", (d: NotificationThreadDetail) => { d.thread.latest_fact_id = NEXT; }, read],
    ["permission", (d: NotificationThreadDetail) => { d.available_commands = []; }, read],
    ["unresolved archive", (d: NotificationThreadDetail) => { d.thread.business_status = "open"; }, archive],
    ["selected delivery removed", (d: NotificationThreadDetail) => { d.deliveries[0]!.delivery_id = NEXT; }, requeue],
    ["delivery not exhausted", (d: NotificationThreadDetail) => { d.deliveries[0]!.status = "pending"; }, requeue],
    ["route", (d: NotificationThreadDetail) => { d.deliveries[0]!.route_id = NEXT; }, requeue],
    ["route version", (d: NotificationThreadDetail) => { d.deliveries[0]!.route_version = 4; }, requeue],
    ["missing route version", (d: NotificationThreadDetail) => { d.deliveries[0]!.route_version = null; }, requeue],
  ] as const)("denies fresh %s drift before identity/save/POST", async (_label, change, intent) => {
    const h = handlers(), hook = mount(); await ready(hook); change(h.world.detail);
    const identity = vi.spyOn(crypto, "randomUUID"), writes = vi.spyOn(Storage.prototype, "setItem");
    await perform(hook, intent); expect(identity).not.toHaveBeenCalled(); expect(writes).not.toHaveBeenCalled(); expect(h.counts.post).toBe(0);
  });
  it.each([read, archive])("missing route version does not deny $kind", async intent => {
    const h = handlers(), hook = mount(); await ready(hook); h.world.detail.deliveries[0]!.route_version = null;
    await perform(hook, intent); expect(h.counts.post).toBe(1);
  });
  it.each(["expired", "foreign", "denied", "unknown host", "legacy switch", "read failure"])("denies %s after cached Product authority without legacy fallback", async failure => {
    const h = handlers(), hook = mount(); await ready(hook);
    if (failure === "expired") h.world.session.expires_at = "2020-01-01T00:00:00Z";
    if (failure === "foreign") h.world.session.principal_id = "foreign";
    if (failure === "denied") h.world.sessionFails = true;
    if (failure === "unknown host") h.world.health.service = "unknown";
    if (failure === "legacy switch") h.world.health.service = "sidera-main-agent-gateway";
    if (failure === "read failure") h.world.detailFails = true;
    const identity = vi.spyOn(crypto, "randomUUID"), writes = vi.spyOn(Storage.prototype, "setItem");
    await perform(hook); expect(identity).not.toHaveBeenCalled(); expect(writes).not.toHaveBeenCalled(); expect(h.counts.post).toBe(0);
    expect(h.unexpected).toEqual([]);
  });
  it("recovers a lost original with GET alone after reload and no available delivery/actions", async () => {
    const h = handlers(); h.world.lost = true; const first = mount(); await ready(first); await perform(first, requeue);
    const original = first.result.current.original; expect(first.result.current.state?.status).toBe("unknown"); first.unmount();
    h.world.detail.deliveries = []; h.world.detail.available_commands = []; h.world.detailFails = true;
    const second = mount(); await ready(second);
    const identity = vi.spyOn(crypto, "randomUUID"); await act(async () => { await second.result.current.lookupOriginal(); });
    expect(h.lookups).toEqual([original!.command_id]); expect(h.counts.post).toBe(1); expect(h.counts.detail).toBe(1);
    expect(identity).not.toHaveBeenCalled(); expect(second.result.current.state?.outcome).toMatchObject({ kind: "requeue", deliveryId: NEXT, deliveryStatus: "pending" });
  });
  it("keeps handled-unmapped and applied-null results GET recoverable and blocks new POST", async () => {
    const h = handlers(); h.world.appliedNull = true; const hook = mount(); await ready(hook); await perform(hook);
    expect(hook.result.current.state?.status).toBe("applied"); expect(hook.result.current.state?.outcome).toBeNull();
    await act(async () => { await hook.result.current.lookupOriginal(); }); await perform(hook, archive);
    expect(h.counts.lookup).toBe(1); expect(h.counts.post).toBe(1); expect(hook.result.current.canPerform).toBe(false);
  });
  it("retains the prior SAFE draft on initial save failure", async () => {
    const h = handlers(), hook = mount(); await ready(hook); await perform(hook);
    const safe = hook.result.current.state, serialized = localStorage.getItem(nativeInboxDraftKey(scope));
    const writes = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("private write error"); });
    await perform(hook, archive); expect(h.counts.post).toBe(1); expect(hook.result.current.state).toBe(safe);
    expect(localStorage.getItem(nativeInboxDraftKey(scope))).toBe(serialized); expect(hook.result.current.error).not.toContain("private");
    writes.mockRestore(); act(() => hook.result.current.retryStorage()); expect(hook.result.current.original).toEqual(safe?.original);
    expect(h.counts.post).toBe(1); expect(h.counts.lookup).toBe(0);
  });
  it("retains the actual submitted original on outcome save failure and retries storage only", async () => {
    const h = handlers(), hook = mount(); await ready(hook);
    const real = Storage.prototype.setItem; let writes = 0;
    const spy = vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (this: Storage, key, value) { writes++; if (writes === 2) throw new Error("private"); real.call(this, key, value); });
    await perform(hook); expect(hook.result.current.original).toEqual(h.posted[0]); expect(hook.result.current.state?.outcome?.kind).toBe("mark_read");
    expect(hook.result.current.storageError).not.toBeNull(); spy.mockRestore(); act(() => hook.result.current.retryStorage());
    expect(hook.result.current.storageError).toBeNull(); expect(h.counts.post).toBe(1); expect(h.counts.lookup).toBe(0);
    expect(JSON.parse(localStorage.getItem(nativeInboxDraftKey(scope))!)).toMatchObject({ original: h.posted[0], status: "applied" });
  });
  it("does not downgrade handled state when refresh fails and permits only a new explicit intent", async () => {
    const h = handlers(), hook = mount(WS, vi.fn(async () => { throw new Error("private refresh"); })); await ready(hook); await perform(hook);
    expect(hook.result.current.state?.status).toBe("applied"); expect(hook.result.current.state?.outcome?.kind).toBe("mark_read");
    expect(hook.result.current.canPerform).toBe(true); await perform(hook, archive);
    expect(h.counts.post).toBe(2); expect(h.posted[1]?.command_id).not.toBe(h.posted[0]?.command_id);
    expect(hook.result.current.error).not.toContain("private");
  });
  it("held preflight A to B allocates identity0/save0/POST0", async () => {
    const h = handlers(), hook = mount(); await ready(hook); h.world.readGate = deferred();
    const identity = vi.spyOn(crypto, "randomUUID"), writes = vi.spyOn(Storage.prototype, "setItem");
    let pending!: Promise<boolean>; act(() => { pending = hook.result.current.perform(read); });
    await waitFor(() => expect(h.counts.detail).toBe(1)); h.world.session.workspace_id = OTHER;
    hook.rerender({ workspaceId: OTHER, onRefresh: hook.onRefresh }); await ready(hook);
    await act(async () => { h.world.readGate!.release(); await pending; });
    expect(identity).not.toHaveBeenCalled(); expect(writes).not.toHaveBeenCalled(); expect(h.counts.post).toBe(0); expect(hook.onRefresh).not.toHaveBeenCalled();
  });
  it("held POST preserves A original without B state/storage/refresh mutation", async () => {
    const h = handlers(), hook = mount(); await ready(hook); h.world.postGate = deferred();
    let pending!: Promise<boolean>; act(() => { pending = hook.result.current.perform(read); }); await waitFor(() => expect(h.counts.post).toBe(1));
    const storedA = localStorage.getItem(nativeInboxDraftKey(scope)); h.world.session.workspace_id = OTHER;
    hook.rerender({ workspaceId: OTHER, onRefresh: hook.onRefresh }); await ready(hook);
    const stateB = hook.result.current.state; const writes = vi.spyOn(Storage.prototype, "setItem");
    await act(async () => { h.world.postGate!.release(); await pending; });
    expect(hook.result.current.state).toBe(stateB); expect(writes).not.toHaveBeenCalled(); expect(hook.onRefresh).not.toHaveBeenCalled();
    expect(localStorage.getItem(nativeInboxDraftKey(scope))).toBe(storedA); expect(hook.result.current.original).toBeNull();
  });
  it("live expiry and same-principal recheck allocate identity0/storage0/POST0", async () => {
    const h = handlers(), hook = mount(); await ready(hook);
    const identity = vi.spyOn(crypto, "randomUUID"), writes = vi.spyOn(Storage.prototype, "setItem");
    h.world.session = { ...SESSION, expires_at: new Date(Date.now() + 300).toISOString() };
    await act(async () => { await hook.result.current.onSessionAuthorized(h.world.session); });
    await waitFor(() => expect(hook.result.current.authorized).toBe(false), { timeout: 1500 }); expect(hook.result.current.canPerform).toBe(false);
    h.world.session = { ...SESSION }; await act(async () => { await hook.result.current.onSessionAuthorized(SESSION); }); await ready(hook);
    expect(identity).not.toHaveBeenCalled(); expect(writes).not.toHaveBeenCalled(); expect(h.counts.post).toBe(0);
  });
  it("actual equal successful refetch clears earlier denial while preserving frozen original", async () => {
    const h = handlers(); h.world.lost = true; const hook = mount(); await ready(hook); await perform(hook);
    const original = hook.result.current.original, intent = hook.result.current.state?.intent, cached = hook.result.current.authority;
    h.world.sessionFails = true; await act(async () => { await hook.result.current.lookupOriginal(); }); expect(hook.result.current.authorized).toBe(false);
    const identity = vi.spyOn(crypto, "randomUUID"), writes = vi.spyOn(Storage.prototype, "setItem");
    h.world.sessionFails = false; await act(async () => { await hook.result.current.onSessionAuthorized(SESSION); }); await ready(hook);
    expect(hook.result.current.authority).toBe(cached); expect(hook.result.current.original).toBe(original); expect(hook.result.current.state?.intent).toBe(intent);
    expect(identity).not.toHaveBeenCalled(); expect(writes).not.toHaveBeenCalled(); expect(h.counts.post).toBe(1); expect(h.counts.lookup).toBe(0);
  });
  it("denied refetch stays denied and a foreign principal gets its own slot", async () => {
    const h = handlers(); h.world.lost = true; const hook = mount(); await ready(hook); await perform(hook);
    const original = hook.result.current.original, stored = localStorage.getItem(nativeInboxDraftKey(scope));
    h.world.sessionFails = true; await act(async () => { await hook.result.current.onSessionAuthorized(SESSION); });
    expect(hook.result.current.authorized).toBe(false); expect(hook.result.current.original).toBe(original);
    h.world.sessionFails = false; h.world.session.principal_id = "foreign";
    await act(async () => { await hook.result.current.onSessionAuthorized(h.world.session); }); await ready(hook);
    expect(hook.result.current.original).toBeNull(); expect(localStorage.getItem(nativeInboxDraftKey(scope))).toBe(stored); expect(h.counts.post).toBe(1);
  });
});
