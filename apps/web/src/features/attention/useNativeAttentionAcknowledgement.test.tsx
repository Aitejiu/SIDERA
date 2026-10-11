import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CommandRequest } from "../assignments/types";
import type { LocalSession } from "../local-session/api";
import type { TaskGraph, TaskView } from "../tasks/types";
import { server } from "@/test/server";
import { nativeAttentionDraftKey, type NativeAttentionItem } from "./nativeAttentionAcknowledgement";
import { useNativeAttentionAcknowledgement } from "./useNativeAttentionAcknowledgement";

const WS = "20000000-0000-0000-0000-000000000001";
const ROOT = "30000000-0000-0000-0000-000000000001";
const OTHER = "30000000-0000-0000-0000-000000000002";
const SESSION: LocalSession = { session_id: "session-operator", workspace_id: WS, principal_id: "operator", expires_at: "2099-01-01T00:00:00Z" };
const ITEM: NativeAttentionItem = {
  key: "blocked:worker", kind: "blocked", correlation_id: "50000000-0000-0000-0000-000000000001",
  resource_id: OTHER, resource_version: 3, occurred_at: "2026-10-07T00:00:00Z", requires_operator: true,
  task_title: "Task", subtask_key: "worker", subtask_title: "Worker", reason: "Review required",
};
const TASK: TaskView = { id: ROOT, workspace_id: WS, title: "Task", description: "Review", acceptance_criteria: [], requested_by: "operator", priority: 1, status: "running", version: 11, created_at: "2026-10-07T00:00:00Z", updated_at: "2026-10-07T00:00:00Z" };
const GRAPH: TaskGraph = { schema_version: 1, nodes: [], edges: [], milestones: [], overview: {
  schema_version: 1, workspace_id: WS, root_task_id: ROOT, title: "Task", goal_summary: "Review", plan_snapshot_id: OTHER,
  generation: 1, acceptance_refs: [], current_milestone_key: null, subtask_count: 0, planned_subtask_count: 0,
  running_subtask_count: 0, completed_subtask_count: 0, failed_subtask_count: 0, handoff_blocked_count: 0,
  handoff_running_count: 0, next_subtask_keys: [], attention_items: [ITEM], last_change_cursor: 1, as_of: "2026-10-07T00:00:00Z",
} };
const scope = { workspaceId: WS, rootTaskId: ROOT, principalId: "operator" };
const reply = (command: CommandRequest) => ({ command_id: command.command_id, aggregate_id: command.target_id, aggregate_version: command.expected_version, event_ids: [], data: {} });

function deferred() {
  let release!: () => void;
  const promise = new Promise<void>(resolve => { release = resolve; });
  return { promise, release };
}

function at<T>(values: readonly T[], index = 0): T {
  const value = values[index];
  if (value === undefined) throw new Error("Expected test fixture entry");
  return value;
}

function handlers() {
  const world = {
    session: SESSION as LocalSession | null, health: { status: "ok", service: "sidera-product" },
    task: structuredClone(TASK), graph: structuredClone(GRAPH), taskFail: false, graphFail: false, lost: false,
    holdTask: undefined as Promise<void> | undefined, holdGraph: undefined as Promise<void> | undefined,
    holdPost: undefined as Promise<void> | undefined, holdHealth: undefined as Promise<void> | undefined,
    response: reply as (command: CommandRequest) => Record<string, unknown>, journal: undefined as Record<string, unknown> | undefined,
  };
  const posted: CommandRequest[] = [], gets: string[] = [], csrf: (string | null)[] = [], saved: (string | null)[] = [];
  const writes: string[] = [];
  let taskReads = 0, graphReads = 0, healthReads = 0, sessionReads = 0;
  server.use(
    http.get("/main-agent/v1/health", async () => { healthReads++; await world.holdHealth; return HttpResponse.json(world.health); }),
    http.get("/product/v1/session", () => { sessionReads++; return world.session === null ? new HttpResponse(null, { status: 401 }) : HttpResponse.json(world.session); }),
    http.get("/api/v1/tasks/:taskId", async () => { taskReads++; await world.holdTask; return world.taskFail ? new HttpResponse(null, { status: 503 }) : HttpResponse.json(world.task); }),
    http.get("/api/v1/tasks/:taskId/graph", async () => { graphReads++; await world.holdGraph; return world.graphFail ? new HttpResponse(null, { status: 503 }) : HttpResponse.json(world.graph); }),
    http.post("/api/v1/commands", async ({ request }) => {
      const original = await request.json() as CommandRequest;
      posted.push(original); writes.push(new URL(request.url).pathname); csrf.push(request.headers.get("X-Sidera-CSRF"));
      saved.push(localStorage.getItem(nativeAttentionDraftKey(scope)));
      await world.holdPost;
      return world.lost ? HttpResponse.error() : HttpResponse.json(world.response(original));
    }),
    http.get("/api/v1/workspaces/:workspaceId/commands/:commandId", ({ params }) => {
      gets.push(String(params.commandId)); const original = posted[0];
      return HttpResponse.json(world.journal ?? (original ? {
        command_id: original.command_id, command_type: original.command_type, target_type: original.target_type,
        target_id: original.target_id, expected_version: original.expected_version, payload: original.payload,
        status: "applied", result: reply(original),
      } : {}));
    }),
    http.all("*", ({ request }) => { if (request.method !== "GET") writes.push(new URL(request.url).pathname); return new HttpResponse(null, { status: 404 }); }),
  );
  return { world, posted, gets, csrf, saved, writes, taskReads: () => taskReads, graphReads: () => graphReads, healthReads: () => healthReads, sessionReads: () => sessionReads };
}

function mount(onRefresh = async () => undefined) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  const hook = renderHook(({ workspaceId, rootTaskId }) => useNativeAttentionAcknowledgement({ workspaceId, rootTaskId, onRefresh }), {
    wrapper, initialProps: { workspaceId: WS, rootTaskId: ROOT },
  });
  return { ...hook, client };
}
async function ready(hook: ReturnType<typeof mount>) {
  await waitFor(() => expect(hook.result.current.canAcknowledge).toBe(true));
}
async function acknowledge(hook: ReturnType<typeof mount>, item = ITEM, version = 11) {
  let result = false;
  await act(async () => { result = await hook.result.current.acknowledge(item, version); });
  return result;
}

describe("Page-owned native attention lifecycle", () => {
  beforeEach(() => { localStorage.clear(); document.cookie = "sidera_csrf=attention-current-csrf; Path=/"; });
  afterEach(() => { vi.restoreAllMocks(); document.cookie = "sidera_csrf=; Max-Age=0; Path=/"; });

  it("restores equal cached authority after an explicit successful current-human recheck", async () => {
    const h = handlers(), hook = mount(); await ready(hook);
    const cached = hook.result.current.authority;
    const identity = vi.spyOn(crypto, "randomUUID"), save = vi.spyOn(Storage.prototype, "setItem");
    h.world.session = null;
    expect(await acknowledge(hook)).toBe(false); expect(hook.result.current.authorized).toBe(false);
    expect(hook.result.current.canAcknowledge).toBe(false);
    h.world.session = SESSION;
    act(() => { void hook.result.current.onSessionAuthorized(SESSION); });
    await waitFor(() => expect(h.sessionReads()).toBe(3));
    await waitFor(() => expect(hook.client.getQueryState(["native-attention-authority", WS, ROOT])?.fetchStatus).toBe("idle"));
    expect(hook.result.current.authority).toBe(cached);
    await waitFor(() => expect(hook.result.current.authorized).toBe(true));
    expect(hook.result.current.canAcknowledge).toBe(true);
    expect(identity).not.toHaveBeenCalled(); expect(save).not.toHaveBeenCalled(); expect(h.posted).toHaveLength(0);
  });

  it("keeps a denied explicit recheck denied instead of trusting its supplied session", async () => {
    const h = handlers(), hook = mount(); await ready(hook); h.world.session = null;
    expect(await acknowledge(hook)).toBe(false);
    const identity = vi.spyOn(crypto, "randomUUID"), save = vi.spyOn(Storage.prototype, "setItem");
    await act(async () => { await hook.result.current.onSessionAuthorized(SESSION); });
    expect(h.sessionReads()).toBe(3); expect(hook.result.current.authorized).toBe(false);
    expect(hook.result.current.canAcknowledge).toBe(false);
    expect(identity).not.toHaveBeenCalled(); expect(save).not.toHaveBeenCalled(); expect(h.posted).toHaveLength(0);
  });

  it("restores equal authority while preserving the same-principal unresolved original", async () => {
    const h = handlers(); h.world.lost = true; const hook = mount(); await ready(hook); await acknowledge(hook);
    const original = hook.result.current.original, draft = hook.result.current.state;
    const stored = localStorage.getItem(nativeAttentionDraftKey(scope)), cached = hook.result.current.authority;
    const identity = vi.spyOn(crypto, "randomUUID"), save = vi.spyOn(Storage.prototype, "setItem");
    h.world.session = null; await act(async () => { await hook.result.current.lookupOriginal(); });
    expect(hook.result.current.authorized).toBe(false); expect(h.gets).toHaveLength(0);
    h.world.session = SESSION; await act(async () => { await hook.result.current.onSessionAuthorized(SESSION); });
    await waitFor(() => expect(hook.result.current.authorized).toBe(true));
    expect(hook.result.current.authority).toBe(cached); expect(hook.result.current.original).toBe(original);
    expect(hook.result.current.state).toBe(draft); expect(hook.result.current.canAcknowledge).toBe(false);
    expect(localStorage.getItem(nativeAttentionDraftKey(scope))).toBe(stored);
    expect(identity).not.toHaveBeenCalled(); expect(save).not.toHaveBeenCalled(); expect(h.posted).toHaveLength(1);
  });

  it("fences a held explicit authority recheck from a new root and foreign principal", async () => {
    const h = handlers(), hook = mount(); await ready(hook); h.world.session = null; await acknowledge(hook);
    const identity = vi.spyOn(crypto, "randomUUID"), save = vi.spyOn(Storage.prototype, "setItem");
    const hold = deferred(); h.world.holdHealth = hold.promise; h.world.session = SESSION;
    let completed!: Promise<unknown>;
    act(() => { completed = Promise.resolve(hook.result.current.onSessionAuthorized(SESSION)); });
    await waitFor(() => expect(h.healthReads()).toBe(3));
    hook.rerender({ workspaceId: WS, rootTaskId: OTHER });
    h.world.session = { ...SESSION, principal_id: "foreign" }; hold.release();
    await act(async () => { await completed; }); await ready(hook);
    expect(hook.result.current.state?.scope).toEqual({ workspaceId: WS, rootTaskId: OTHER, principalId: "foreign" });
    expect(hook.result.current.original).toBeNull(); expect(localStorage.length).toBe(0);
    expect(identity).not.toHaveBeenCalled(); expect(save).not.toHaveBeenCalled(); expect(h.posted).toHaveLength(0);
  });

  it("sends the sole user command with exact current facts and CSRF only after durable original save", async () => {
    const h = handlers(), hook = mount(); await ready(hook);
    expect(h.posted).toHaveLength(0);
    expect(await acknowledge(hook)).toBe(true);
    expect(h.posted).toHaveLength(1); expect(h.csrf).toEqual(["attention-current-csrf"]);
    expect(at(h.posted)).toMatchObject({ workspace_id: WS, command_type: "AcknowledgeAttentionItem", actor: { type: "user", id: "operator" }, target_type: "task", target_id: ROOT, expected_version: 11 });
    expect(at(h.posted).payload).toEqual({ attention_key: ITEM.key, source_correlation_id: ITEM.correlation_id, source_resource_version: 3 });
    expect(JSON.parse(h.saved[0]!)).toMatchObject({ status: "pending", original: at(h.posted) });
    expect(hook.result.current.state?.outcome).toBe("acknowledged");
    expect(h.writes).toEqual(["/api/v1/commands"]);
  });

  it("holds a synchronous lock across same-tick double clicks", async () => {
    const h = handlers(), hold = deferred(); h.world.holdPost = hold.promise;
    const hook = mount(); await ready(hook);
    let first!: Promise<boolean>, second!: Promise<boolean>;
    act(() => { first = hook.result.current.acknowledge(ITEM, 11); second = hook.result.current.acknowledge(ITEM, 11); });
    await waitFor(() => expect(h.posted).toHaveLength(1));
    await expect(second).resolves.toBe(false);
    hold.release(); await act(async () => { expect(await first).toBe(true); });
    expect(h.posted).toHaveLength(1);
  });

  it.each([
    ["denied", null], ["expired", { ...SESSION, expires_at: "2000-01-01T00:00:00Z" }],
    ["foreign principal", { ...SESSION, principal_id: "foreign" }], ["foreign workspace", { ...SESSION, workspace_id: OTHER }],
  ])("fences fresh %s authority before identity, save or POST", async (_name, session) => {
    const h = handlers(), hook = mount(); await ready(hook); h.world.session = session;
    const identity = vi.spyOn(crypto, "randomUUID"), save = vi.spyOn(Storage.prototype, "setItem");
    expect(await acknowledge(hook)).toBe(false); expect(hook.result.current.authorized).toBe(false);
    expect(identity).not.toHaveBeenCalled(); expect(save).not.toHaveBeenCalled(); expect(h.posted).toHaveLength(0);
    expect(hook.result.current.error).not.toMatch(/private|401/);
  });

  it("never falls back to legacy for an unavailable host or missing current session", async () => {
    const h = handlers(); h.world.health = { status: "ok", service: "unknown" };
    const hook = mount(); await waitFor(() => expect(hook.result.current.authority?.kind).toBe("unavailable"));
    expect(await acknowledge(hook)).toBe(false); expect(h.posted).toHaveLength(0);
    h.world.health = { status: "ok", service: "sidera-product" }; h.world.session = null;
    await act(async () => { await hook.result.current.onSessionAuthorized(SESSION); });
    await waitFor(() => expect(hook.result.current.authority).toEqual({ kind: "unavailable", reason: "session_unavailable" }));
    expect(await acknowledge(hook)).toBe(false); expect(h.posted).toHaveLength(0);
  });

  it("selects legacy only on positive gateway health and allocates no native identity", async () => {
    const h = handlers(); h.world.health = { status: "ok", service: "sidera-main-agent-gateway" };
    const identity = vi.spyOn(crypto, "randomUUID"), hook = mount();
    await waitFor(() => expect(hook.result.current.authority?.kind).toBe("legacy"));
    expect(await acknowledge(hook)).toBe(false); expect(identity).not.toHaveBeenCalled(); expect(h.posted).toHaveLength(0);
  });

  it.each(["root version", "root id", "root workspace", "graph root", "graph workspace", "key", "correlation", "source version", "removed", "proposal"])("blocks changed %s facts without identity or writes", async (change) => {
    const h = handlers(), hook = mount(); await ready(hook);
    if (change === "root version") h.world.task.version = 12;
    if (change === "root id") h.world.task.id = OTHER;
    if (change === "root workspace") h.world.task.workspace_id = OTHER;
    if (change === "graph root") h.world.graph.overview.root_task_id = OTHER;
    if (change === "graph workspace") h.world.graph.overview.workspace_id = OTHER;
    const item = at(h.world.graph.overview.attention_items);
    if (change === "key") item.key = "other";
    if (change === "correlation") item.correlation_id = OTHER;
    if (change === "source version") item.resource_version = 4;
    if (change === "removed") h.world.graph.overview.attention_items = [];
    if (change === "proposal") item.kind = "collaboration_release_proposal";
    const identity = vi.spyOn(crypto, "randomUUID"), save = vi.spyOn(Storage.prototype, "setItem");
    expect(await acknowledge(hook)).toBe(false);
    expect(identity).not.toHaveBeenCalled(); expect(save).not.toHaveBeenCalled(); expect(h.posted).toHaveLength(0);
  });

  it.each(["task", "graph"])("blocks a failed fresh %s read with safe finite error", async (source) => {
    const h = handlers(), hook = mount(); await ready(hook);
    h.world.taskFail = source === "task"; h.world.graphFail = source === "graph";
    const identity = vi.spyOn(crypto, "randomUUID"), save = vi.spyOn(Storage.prototype, "setItem");
    expect(await acknowledge(hook)).toBe(false); expect(hook.result.current.error).toBe("当前待处理事项暂不可用，请重新读取。");
    expect(identity).not.toHaveBeenCalled(); expect(save).not.toHaveBeenCalled(); expect(h.posted).toHaveLength(0);
  });

  it("blocks an unreadable slot and recovers only by explicit storage retry", async () => {
    const h = handlers(), read = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("private read"); });
    const hook = mount(); await waitFor(() => expect(hook.result.current.storageError).toBe("storage_unavailable"));
    expect(await acknowledge(hook)).toBe(false); expect(h.posted).toHaveLength(0);
    read.mockRestore(); act(() => hook.result.current.retryStorage()); await ready(hook);
  });

  it("keeps the prior safe action when initial save fails and never stores an unsent pending command on retry", async () => {
    const h = handlers(), hook = mount(); await ready(hook);
    const write = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("private save"); });
    expect(await acknowledge(hook)).toBe(false); expect(hook.result.current.state?.status).toBe("idle");
    expect(hook.result.current.original).toBeNull(); expect(h.posted).toHaveLength(0);
    write.mockRestore(); act(() => hook.result.current.retryStorage()); await ready(hook);
    expect(JSON.parse(localStorage.getItem(nativeAttentionDraftKey(scope))!).original).toBeNull();
  });

  it("preserves the immutable original after a post-submit storage failure and can GET it", async () => {
    const h = handlers(); h.world.lost = true;
    const hook = mount(); await ready(hook);
    const realSet = Storage.prototype.setItem; let writes = 0;
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (this: Storage, key, value) {
      if (++writes > 1) throw new Error("private post-submit save");
      return realSet.call(this, key, value);
    });
    expect(await acknowledge(hook)).toBe(false); expect(hook.result.current.storageError).toBe("storage_unavailable");
    expect(hook.result.current.original).toEqual(at(h.posted));
    expect(Object.isFrozen(hook.result.current.original?.payload)).toBe(true);
    await act(async () => { await hook.result.current.lookupOriginal(); });
    expect(h.gets).toEqual([at(h.posted).command_id]); expect(h.posted).toHaveLength(1);
    expect(hook.result.current.state?.outcome).toBe("acknowledged");
  });

  it("preserves a post-submit storage fault during equal successful reauthorization", async () => {
    const h = handlers(); h.world.lost = true; const hook = mount(); await ready(hook);
    const realSet = Storage.prototype.setItem; let writes = 0;
    const save = vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (this: Storage, key, value) {
      if (++writes > 1) throw new Error("private storage detail"); return realSet.call(this, key, value);
    });
    await acknowledge(hook); const draft = hook.result.current.state, original = hook.result.current.original;
    expect(hook.result.current.storageError).toBe("storage_unavailable"); save.mockClear();
    const identity = vi.spyOn(crypto, "randomUUID"); h.world.session = null;
    await act(async () => { await hook.result.current.lookupOriginal(); });
    h.world.session = SESSION; await act(async () => { await hook.result.current.onSessionAuthorized(SESSION); });
    await waitFor(() => expect(hook.result.current.authorized).toBe(true));
    expect(hook.result.current.state).toBe(draft); expect(hook.result.current.original).toBe(original);
    expect(hook.result.current.storageError).toBe("storage_unavailable"); expect(hook.result.current.canAcknowledge).toBe(false);
    expect(identity).not.toHaveBeenCalled(); expect(save).not.toHaveBeenCalled(); expect(h.posted).toHaveLength(1);
  });

  it.each(["mapped", "rejected"])("requires a fresh explicit click to replace a known %s original", async (terminal) => {
    const h = handlers(); h.world.lost = terminal === "rejected"; const hook = mount(); await ready(hook); await acknowledge(hook);
    if (terminal === "rejected") {
      const command = at(h.posted);
      h.world.journal = { command_id: command.command_id, command_type: command.command_type, target_type: "task", target_id: ROOT, expected_version: 11, status: "rejected", result: null };
      await act(async () => { await hook.result.current.lookupOriginal(); });
    }
    expect(h.posted).toHaveLength(1); expect(hook.result.current.canAcknowledge).toBe(true);
    const nextItem = { ...ITEM, resource_version: 4, correlation_id: OTHER };
    h.world.task.version = 12; h.world.graph.overview.attention_items = [nextItem]; h.world.lost = false;
    expect(await acknowledge(hook, nextItem, 12)).toBe(true);
    expect(h.posted.map(command => command.expected_version)).toEqual([11, 12]);
    expect(h.posted.map(command => command.payload.source_resource_version)).toEqual([3, 4]);
    expect(at(h.posted, 1).command_id).not.toBe(at(h.posted).command_id);
    expect(at(h.posted, 1).idempotency_key).not.toBe(at(h.posted).idempotency_key);
    expect(localStorage.length).toBe(1); expect(hook.result.current.original).toEqual(at(h.posted, 1));
  });

  it("rehydrates response loss and queries only the original even after source removal", async () => {
    const h = handlers(); h.world.lost = true; const first = mount(); await ready(first);
    expect(await acknowledge(first)).toBe(false); expect(first.result.current.state?.status).toBe("unknown");
    const stored = localStorage.getItem(nativeAttentionDraftKey(scope)); first.unmount();
    h.world.graph.overview.attention_items = []; const hook = mount();
    await waitFor(() => expect(hook.result.current.original?.command_id).toBe(at(h.posted).command_id));
    const identity = vi.spyOn(crypto, "randomUUID");
    expect(await acknowledge(hook)).toBe(false);
    await act(async () => { await hook.result.current.lookupOriginal(); });
    expect(h.gets).toEqual([at(h.posted).command_id]); expect(h.posted).toHaveLength(1); expect(identity).not.toHaveBeenCalled();
    expect(hook.result.current.state?.outcome).toBe("acknowledged"); expect(stored).toContain(at(h.posted).command_id);
  });

  it.each(["command", "root", "version"])("returns false and preserves recovery for uncorrelated %s replies", async (change) => {
    const h = handlers(); h.world.response = command => ({ ...reply(command), ...(change === "command" ? { command_id: OTHER } : change === "root" ? { aggregate_id: OTHER } : { aggregate_version: 12 }) });
    const hook = mount(); await ready(hook);
    expect(await acknowledge(hook)).toBe(false); expect(hook.result.current.state?.status).toBe("unknown");
    expect(hook.result.current.canAcknowledge).toBe(false); expect(hook.result.current.original).toEqual(at(h.posted));
  });

  it("keeps applied-null handled but unmapped and blocks replacement until a legal mapping", async () => {
    const h = handlers(); h.world.lost = true; const hook = mount(); await ready(hook); await acknowledge(hook);
    const original = at(h.posted);
    h.world.journal = { command_id: original.command_id, command_type: original.command_type, target_type: "task", target_id: ROOT, expected_version: 11, status: "applied", result: null };
    await act(async () => { await hook.result.current.lookupOriginal(); });
    expect(hook.result.current.state).toMatchObject({ status: "applied", outcome: null, original });
    expect(await acknowledge(hook)).toBe(false); expect(h.posted).toHaveLength(1);
    h.world.journal = undefined; await act(async () => { await hook.result.current.lookupOriginal(); });
    expect(hook.result.current.state?.outcome).toBe("acknowledged");
  });

  it.each(["missing", "pending", "foreign payload", "wrong version"])("retains unresolved original on %s journal reads", async (kind) => {
    const h = handlers(); h.world.lost = true; const hook = mount(); await ready(hook); await acknowledge(hook);
    const original = at(h.posted); const journal = { command_id: original.command_id, command_type: original.command_type, target_type: "task", target_id: ROOT, expected_version: 11, payload: original.payload, status: "applied", result: reply(original) };
    h.world.journal = kind === "missing" ? {} : { ...journal, ...(kind === "pending" ? { status: "pending", result: null } : kind === "foreign payload" ? { payload: { ...original.payload, attention_key: "other" } } : { expected_version: 12 }) };
    await act(async () => { await hook.result.current.lookupOriginal(); });
    expect(hook.result.current.state?.status).toBe("unknown"); expect(hook.result.current.canAcknowledge).toBe(false); expect(h.posted).toHaveLength(1);
  });

  it("does not downgrade a mapped outcome when canonical refresh fails", async () => {
    const h = handlers(), hook = mount(async () => { throw new Error("private refresh"); }); await ready(hook);
    expect(await acknowledge(hook)).toBe(true); expect(hook.result.current.state?.outcome).toBe("acknowledged");
    expect(hook.result.current.error).toBe("确认已处理，当前事实暂未刷新。"); expect(h.posted).toHaveLength(1);
  });

  it.each(["health", "task", "graph"])("invalidates a held %s preflight on A unmount before any identity or save", async (stage) => {
    const h = handlers(), hook = mount(); await ready(hook); const hold = deferred();
    if (stage === "health") h.world.holdHealth = hold.promise;
    if (stage === "task") h.world.holdTask = hold.promise;
    if (stage === "graph") h.world.holdGraph = hold.promise;
    const identity = vi.spyOn(crypto, "randomUUID"), save = vi.spyOn(Storage.prototype, "setItem");
    let completion!: Promise<boolean>; act(() => { completion = hook.result.current.acknowledge(ITEM, 11); });
    await waitFor(() => expect(stage === "health" ? h.healthReads() : stage === "task" ? h.taskReads() : h.graphReads()).toBeGreaterThan(stage === "health" ? 1 : 0));
    hook.unmount(); hold.release(); await act(async () => { expect(await completion).toBe(false); });
    expect(identity).not.toHaveBeenCalled(); expect(save).not.toHaveBeenCalled(); expect(h.posted).toHaveLength(0);
  });

  it("isolates held POST completion from B state, storage, cache and refresh", async () => {
    const h = handlers(), refresh = vi.fn(async () => undefined), hold = deferred(); h.world.holdPost = hold.promise;
    const hook = mount(refresh); await ready(hook); let completion!: Promise<boolean>;
    act(() => { completion = hook.result.current.acknowledge(ITEM, 11); });
    await waitFor(() => expect(h.posted).toHaveLength(1));
    const a = localStorage.getItem(nativeAttentionDraftKey(scope));
    hook.rerender({ workspaceId: WS, rootTaskId: OTHER }); await ready(hook);
    const b = nativeAttentionDraftKey({ ...scope, rootTaskId: OTHER });
    hook.client.setQueryData(["task-graph", WS, OTHER], "B-canonical");
    hold.release(); await act(async () => { expect(await completion).toBe(false); });
    expect(localStorage.getItem(nativeAttentionDraftKey(scope))).toBe(a); expect(localStorage.getItem(b)).toBeNull();
    expect(hook.result.current.original).toBeNull(); expect(refresh).not.toHaveBeenCalled();
    expect(hook.client.getQueryData(["task-graph", WS, OTHER])).toBe("B-canonical");
  });

  it("renders live expiry and same-principal reauthorization without identity or storage writes", async () => {
    const h = handlers(), hook = mount(); await ready(hook);
    const identity = vi.spyOn(crypto, "randomUUID"), save = vi.spyOn(Storage.prototype, "setItem");
    const short = { ...SESSION, expires_at: new Date(Date.now() + 400).toISOString() }; h.world.session = short;
    await act(async () => { await hook.result.current.onSessionAuthorized(short); });
    await waitFor(() => expect(hook.result.current.authority).toEqual({ kind: "product", session: short }));
    await waitFor(() => expect(hook.result.current.authorized).toBe(false), { timeout: 1500 });
    expect(hook.result.current.canAcknowledge).toBe(false);
    h.world.session = SESSION; await act(async () => { await hook.result.current.onSessionAuthorized(SESSION); }); await ready(hook);
    expect(identity).not.toHaveBeenCalled(); expect(save).not.toHaveBeenCalled(); expect(h.posted).toHaveLength(0);
  });

  it("preserves the original on same-principal reauthorization and isolates a new principal", async () => {
    const h = handlers(); h.world.lost = true; const hook = mount(); await ready(hook); await acknowledge(hook);
    const original = hook.result.current.original, stored = localStorage.getItem(nativeAttentionDraftKey(scope));
    const identity = vi.spyOn(crypto, "randomUUID"), save = vi.spyOn(Storage.prototype, "setItem");
    await act(async () => { await hook.result.current.onSessionAuthorized(SESSION); });
    await waitFor(() => expect(hook.result.current.authority?.kind).toBe("product"));
    expect(hook.result.current.original).toBe(original);
    const foreign = { ...SESSION, principal_id: "foreign" }; h.world.session = foreign;
    await act(async () => { await hook.result.current.onSessionAuthorized(foreign); }); await ready(hook);
    expect(hook.result.current.original).toBeNull(); expect(localStorage.getItem(nativeAttentionDraftKey(scope))).toBe(stored);
    expect(identity).not.toHaveBeenCalled(); expect(save).not.toHaveBeenCalled(); expect(h.posted).toHaveLength(1);
  });
});
