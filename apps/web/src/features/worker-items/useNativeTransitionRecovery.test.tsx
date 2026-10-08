import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { server } from "@/test/server";
import type { LocalSession } from "../local-session/api";
import type { CommandRequest, CommandResponse, CommandStatusResponse } from "../assignments/types";
import type { WorkerTransition, WorkerWorkspace } from "./types";
import { nativeTransitionRecoveryDraftKey, type NativeTransitionRecoveryIntent } from "./nativeTransitionRecovery";
import { useNativeTransitionRecovery } from "./useNativeTransitionRecovery";

const WS = "20000000-0000-0000-0000-000000000001", WI = "40000000-0000-0000-0000-000000000001";
const WI_B = "40000000-0000-0000-0000-000000000002", ROOT = "30000000-0000-0000-0000-000000000001";
const WT = "a0000000-0000-0000-0000-000000000001", PT = "b0000000-0000-0000-0000-000000000001";
const EX = "60000000-0000-0000-0000-000000000001", ES = "70000000-0000-0000-0000-000000000001";
const SESSION: LocalSession = { session_id: "d0000000-0000-0000-0000-000000000001", workspace_id: WS,
  principal_id: "current-operator", expires_at: "2099-01-01T00:00:00Z" };
const scope = { workspaceId: WS, principalId: SESSION.principal_id, workerItemId: WI };
const intent: NativeTransitionRecoveryIntent = {
  workerTransitionId: WT, planTransitionId: PT, rootTaskId: ROOT, version: 17, action: "continue", fallbackAction: null,
  sourceExecutionId: EX, sourceSessionId: ES, currentContextVersion: 4, targetContextVersion: 5,
  acknowledgedContextVersion: 4, patchId: null, patchHash: null, patchAcknowledged: false,
  strategy: "reconcile_adapter", reason: "  reviewed source  ", evidenceRefs: [" event:stop ", " log:lease ", " "],
};
function transitionFixture(): WorkerTransition {
  return { worker_transition_id: WT, plan_transition_id: PT, version: 17, action: "continue", fallback_action: null,
    status: "blocked_unknown", writer_state: "unknown", source_execution_id: EX, source_session_id: ES,
    root_task: { id: ROOT, key: null, path: "root", title: "Root" }, worker_item: { id: WI, key: "worker", path: "root/worker", title: "Worker" },
    context: { current_context_version: 4, target_context_version: 5, acknowledged_context_version: 4,
      patch_id: null, patch_hash: null, patch_acknowledged: false },
    subtask: null, path: "root/worker", from_subtask_id: null, to_subtask_id: null, target_execution_id: null,
    target_session_id: null, operations: [], blocked_dependencies: [],
    queued_input_disposition: { policy: "supersede", queued_count: 0, superseded_count: 0, queued_input_ids: [], superseded_input_ids: [] },
    failure_code: "adapter_result_unknown", started_at: null, completed_at: null };
}
function workspaceFixture(): WorkerWorkspace {
  return { workspace_id: WS, worker_item: { id: WI, root_task_id: ROOT, stable_key: "worker", title: "Worker", responsibility: "Review",
    status: "active", version: 9, context_version: 4, acknowledged_context_version: 4, updated_at: "2026-10-07T00:00:00Z" },
    current_placement: { worker_item_key: "worker", subtask_key: "worker", subtask_task_id: ROOT, role: "implementer",
      capability_requirements: [], plan_generation: 2, plan_snapshot_id: PT },
    plan_sync: { worker_transition_id: WT, plan_transition_id: PT, status: "blocked_unknown", writer_state: "unknown", action: "continue",
      current_context_version: 4, target_context_version: 5, acknowledged_context_version: 4, patch_id: null, patch_acknowledged: false,
      source_execution_id: EX, source_session_id: ES, target_execution_id: null, target_session_id: null,
      continuation_execution_id: null, continuation_session_id: null, source_generation: 1, target_generation: 2,
      queued_input_ids: [], superseded_input_ids: [], failure_code: "adapter_result_unknown", started_at: null, completed_at: null, trace_href: null },
    current_runtime: null, current_step: null, next_action: null, latest_checkpoint: null, placement_history: [], execution_history: [],
    artifacts: [], evidence: [], usage: { input_tokens: 0, output_tokens: 0, cached_tokens: 0, cost_microunits: 0 },
    can_intervene: false, read_only_reason: "no_active_session", intervention_context: null, trace_descriptor: null,
    last_change_cursor: 1, as_of: "2026-10-07T00:00:00Z" };
}
function install() {
  const world = { session: SESSION as LocalSession | null, host: "sidera-product", workspace: workspaceFixture(), transition: transitionFixture(),
    lost: false, workspaceError: false, holdWorkspace: null as Promise<void> | null, holdTransition: null as Promise<void> | null,
    holdPost: null as Promise<void> | null, journal: null as CommandStatusResponse | null, refreshError: false,
    resultChange: null as Partial<CommandResponse> | null };
  const posts: CommandRequest[] = [], gets: string[] = [], csrf: Array<string | null> = [], durable: Array<string | null> = [];
  const reads = { workspace: 0, transition: 0 };
  const refresh = vi.fn(async () => { if (world.refreshError) throw Error("private refresh"); });
  const response = (command: CommandRequest): CommandResponse => ({ command_id: command.command_id, aggregate_id: WT,
    aggregate_version: 17, event_ids: ["e0000000-0000-0000-0000-000000000001"],
    data: { worker_transition_id: WT, plan_transition_id: PT, version: 17, action: "continue", status: "blocked_unknown",
      writer_state: "unknown", recovery_strategy: "reconcile_adapter", adapter_call: "not_started" }, ...world.resultChange });
  server.use(
    http.get("/main-agent/v1/health", () => HttpResponse.json({ status: "ok", service: world.host })),
    http.get("/product/v1/session", () => HttpResponse.json(world.session)),
    http.get("/api/v1/workspaces/:workspaceId/worker-items/:workerItemId/workspace", async () => {
      reads.workspace++; await world.holdWorkspace;
      return world.workspaceError ? HttpResponse.error() : HttpResponse.json(world.workspace);
    }),
    http.get("/api/v1/workspaces/:workspaceId/worker-transitions/:transitionId", async () => {
      reads.transition++; await world.holdTransition; return HttpResponse.json(world.transition);
    }),
    http.post("/api/v1/commands", async ({ request }) => {
      const command = await request.json() as CommandRequest;
      posts.push(command); csrf.push(request.headers.get("X-Sidera-CSRF")); durable.push(localStorage.getItem(nativeTransitionRecoveryDraftKey(scope)));
      await world.holdPost; return world.lost ? HttpResponse.error() : HttpResponse.json(response(command));
    }),
    http.get("/api/v1/workspaces/:workspaceId/commands/:commandId", ({ params }) => {
      gets.push(String(params.commandId));
      const command = posts[0];
      return command ? HttpResponse.json(world.journal ?? { command_id: command.command_id, command_type: command.command_type,
        target_type: command.target_type, target_id: command.target_id, expected_version: command.expected_version,
        payload: command.payload, status: "applied", result: response(command) }) : HttpResponse.error();
    }),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  const mount = () => renderHook(({ wi }) => useNativeTransitionRecovery({ workspaceId: WS, workerItemId: wi, onRefresh: refresh }),
    { wrapper, initialProps: { wi: WI } });
  return { world, posts, gets, csrf, durable, reads, refresh, mount };
}
function held() { let release!: () => void; const promise = new Promise<void>(resolve => { release = resolve; }); return { promise, release }; }

describe("page-scoped native transition recovery owner", () => {
  beforeEach(() => { localStorage.clear(); document.cookie = "sidera_csrf=current-csrf; Path=/"; });
  afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); document.cookie = "sidera_csrf=; Max-Age=0; Path=/"; });

  it("saves before sole human POST at the displayed Transition17 using current CSRF", async () => {
    const h = install(), mounted = h.mount();
    await waitFor(() => expect(mounted.result.current.canPerform).toBe(true));
    await act(async () => { await mounted.result.current.perform(intent); });
    expect(h.posts).toHaveLength(1); expect(h.csrf).toEqual(["current-csrf"]);
    expect(h.posts[0]).toMatchObject({ command_type: "RecoverWorkerTransition", workspace_id: WS,
      actor: { type: "user", id: "current-operator" }, target_type: "worker_transition", target_id: WT, expected_version: 17,
      payload: { strategy: "reconcile_adapter", reason: "reviewed source", evidence_refs: ["event:stop", "log:lease"] } });
    expect(Object.keys(h.posts[0]!.payload).sort()).toEqual(["evidence_refs", "reason", "strategy"]);
    expect(JSON.parse(h.durable[0]!).original).toEqual(h.posts[0]);
    expect(mounted.result.current.action?.outcome).toBe("recorded"); expect(h.refresh).toHaveBeenCalledTimes(1);
    expect(h.reads).toEqual({ workspace: 1, transition: 1 });
  });

  it("takes the duplicate lock synchronously before fresh reads", async () => {
    const h = install(), gate = held(); h.world.holdWorkspace = gate.promise;
    const mounted = h.mount(); await waitFor(() => expect(mounted.result.current.canPerform).toBe(true));
    let pending!: Promise<boolean>;
    act(() => { pending = mounted.result.current.perform(intent); void mounted.result.current.perform(intent); });
    await waitFor(() => expect(h.reads.workspace).toBe(1));
    await act(async () => { gate.release(); await pending; });
    expect(h.posts).toHaveLength(1);
  });

  it.each(["denied", "expired", "foreign", "foreign-workspace", "unknown-host"])("denies fresh %s authority before identity/save/POST", async failure => {
    const h = install(), mounted = h.mount(); await waitFor(() => expect(mounted.result.current.canPerform).toBe(true));
    if (failure === "denied") h.world.session = null;
    if (failure === "expired") h.world.session = { ...SESSION, expires_at: "2000-01-01T00:00:00Z" };
    if (failure === "foreign") h.world.session = { ...SESSION, principal_id: "foreign" };
    if (failure === "foreign-workspace") h.world.session = { ...SESSION, workspace_id: WI_B };
    if (failure === "unknown-host") h.world.host = "unknown";
    const identity = vi.spyOn(crypto, "randomUUID"), save = vi.spyOn(Storage.prototype, "setItem");
    await act(async () => { await mounted.result.current.perform(intent); });
    expect(h.posts).toHaveLength(0); expect(identity).not.toHaveBeenCalled(); expect(save).not.toHaveBeenCalled();
    expect(mounted.result.current.authorized).toBe(false);
  });

  it.each(["version", "plan", "source", "session", "action", "fallback", "context", "patch", "root", "worker", "projection", "workspace"])("refuses %s drift without silently updating the review", async drift => {
    const h = install(), mounted = h.mount(); await waitFor(() => expect(mounted.result.current.canPerform).toBe(true));
    if (drift === "version") h.world.transition.version = 18;
    if (drift === "plan") h.world.transition.plan_transition_id = WI_B;
    if (drift === "source") h.world.transition.source_execution_id = WI_B;
    if (drift === "session") h.world.transition.source_session_id = WI_B;
    if (drift === "action") h.world.transition.action = "retire";
    if (drift === "fallback") h.world.transition.fallback_action = "continue";
    if (drift === "context") h.world.transition.context.target_context_version = 6;
    if (drift === "patch") h.world.transition.context.patch_id = WI_B;
    if (drift === "root") h.world.transition.root_task.id = WI_B;
    if (drift === "worker") h.world.transition.worker_item.id = WI_B;
    if (drift === "projection") h.world.workspace.plan_sync = null;
    if (drift === "workspace") h.world.workspace.workspace_id = WI_B;
    const identity = vi.spyOn(crypto, "randomUUID"), save = vi.spyOn(Storage.prototype, "setItem");
    await act(async () => { await mounted.result.current.perform(intent); });
    expect(h.posts).toHaveLength(0); expect(identity).not.toHaveBeenCalled(); expect(save).not.toHaveBeenCalled();
    expect(mounted.result.current.error).toContain("变化");
  });

  it("keeps initial save failure editable and storage retry allocates no command", async () => {
    const h = install(), mounted = h.mount(); await waitFor(() => expect(mounted.result.current.canPerform).toBe(true));
    const save = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw Error("private"); });
    await act(async () => { await mounted.result.current.perform(intent); });
    expect(h.posts).toHaveLength(0); expect(mounted.result.current.original).toBeNull(); expect(mounted.result.current.storageError).toBe("storage_unavailable");
    save.mockRestore(); const identity = vi.spyOn(crypto, "randomUUID");
    act(() => mounted.result.current.retryStorage());
    expect(mounted.result.current.canPerform).toBe(true); expect(identity).not.toHaveBeenCalled(); expect(h.posts).toHaveLength(0);
  });

  it("keeps malformed local storage read-only and never clears it implicitly", async () => {
    localStorage.setItem(nativeTransitionRecoveryDraftKey(scope), "{bad");
    const h = install(), mounted = h.mount(); await waitFor(() => expect(mounted.result.current.storageError).toBe("invalid_draft"));
    await act(async () => { await mounted.result.current.perform(intent); mounted.result.current.retryStorage(); });
    expect(h.posts).toHaveLength(0); expect(localStorage.getItem(nativeTransitionRecoveryDraftKey(scope))).toBe("{bad");
  });

  it("retains submitted original in memory on post-submit save failure", async () => {
    const h = install(), gate = held(); h.world.holdPost = gate.promise;
    const mounted = h.mount(); await waitFor(() => expect(mounted.result.current.canPerform).toBe(true));
    let pending!: Promise<boolean>; act(() => { pending = mounted.result.current.perform(intent); });
    await waitFor(() => expect(h.posts).toHaveLength(1));
    const save = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw Error("private"); });
    await act(async () => { gate.release(); await pending; });
    expect(mounted.result.current.original).toEqual(h.posts[0]); expect(mounted.result.current.action?.outcome).toBe("recorded");
    expect(mounted.result.current.storageError).toBe("storage_unavailable");
    save.mockRestore(); act(() => mounted.result.current.retryStorage());
    expect(JSON.parse(localStorage.getItem(nativeTransitionRecoveryDraftKey(scope))!).original).toEqual(h.posts[0]);
    expect(h.posts).toHaveLength(1);
  });

  it("recovers exact lost original after reload even if canonical sync disappears and source errors", async () => {
    const h = install(); h.world.lost = true;
    const first = h.mount(); await waitFor(() => expect(first.result.current.canPerform).toBe(true));
    await act(async () => { await first.result.current.perform(intent); });
    expect(first.result.current.status).toBe("unknown"); const original = h.posts[0]; first.unmount();
    h.world.workspace.plan_sync = null; h.world.workspaceError = true; h.world.transition.version = 41;
    const reloaded = h.mount(); await waitFor(() => expect(reloaded.result.current.original).toEqual(original));
    const identity = vi.spyOn(crypto, "randomUUID");
    await act(async () => { await reloaded.result.current.lookupOriginal(); });
    expect(h.gets).toEqual([original!.command_id]); expect(h.posts).toHaveLength(1); expect(identity).not.toHaveBeenCalled();
    expect(h.reads).toEqual({ workspace: 1, transition: 1 }); expect(reloaded.result.current.action?.outcome).toBe("recorded");
    expect(reloaded.result.current.original?.expected_version).toBe(17);
  });

  it("keeps APPLIED-null original GET recoverable and blocks new recovery", async () => {
    const h = install(); h.world.lost = true;
    const mounted = h.mount(); await waitFor(() => expect(mounted.result.current.canPerform).toBe(true));
    await act(async () => { await mounted.result.current.perform(intent); });
    const original = h.posts[0]!; h.world.journal = { command_id: original.command_id, status: "applied", result: null };
    await act(async () => { await mounted.result.current.lookupOriginal(); await mounted.result.current.perform(intent); });
    expect(mounted.result.current.status).toBe("applied"); expect(mounted.result.current.action?.outcome).toBeNull();
    expect(mounted.result.current.canPerform).toBe(false); expect(h.posts).toHaveLength(1);
    h.world.journal = null; await act(async () => { await mounted.result.current.lookupOriginal(); });
    expect(mounted.result.current.action?.outcome).toBe("recorded"); expect(h.gets).toEqual([original.command_id, original.command_id]);
  });

  it.each(["pending", "missing", "foreign-payload", "foreign-command"])("never retries POST for %s original journal", async state => {
    const h = install(); h.world.lost = true;
    const mounted = h.mount(); await waitFor(() => expect(mounted.result.current.canPerform).toBe(true));
    await act(async () => { await mounted.result.current.perform(intent); });
    const original = h.posts[0]!;
    h.world.journal = { command_id: original.command_id, status: "pending", result: null };
    if (state === "missing") server.use(http.get("/api/v1/workspaces/:workspaceId/commands/:commandId", () => HttpResponse.json({}, { status: 404 })));
    if (state === "foreign-payload") h.world.journal.payload = { ...original.payload, reason: "foreign" };
    if (state === "foreign-command") h.world.journal.command_id = WI_B;
    await act(async () => { await mounted.result.current.lookupOriginal(); await mounted.result.current.perform(intent); });
    expect(h.posts).toHaveLength(1); expect(mounted.result.current.canPerform).toBe(false); expect(mounted.result.current.original).toEqual(original);
  });

  it("does not downgrade acknowledged handling when refresh fails", async () => {
    const h = install(); h.world.refreshError = true;
    const mounted = h.mount(); await waitFor(() => expect(mounted.result.current.canPerform).toBe(true));
    await act(async () => { await mounted.result.current.perform(intent); });
    expect(mounted.result.current.status).toBe("applied"); expect(mounted.result.current.action?.outcome).toBe("recorded");
    expect(mounted.result.current.error).toContain("刷新"); expect(h.posts).toHaveLength(1);
  });

  it.each(["workspace", "transition"] as const)("held %s read after A unmount has identity0/save0/POST0", async stage => {
    const h = install(), gate = held();
    if (stage === "workspace") h.world.holdWorkspace = gate.promise; else h.world.holdTransition = gate.promise;
    const mounted = h.mount(); await waitFor(() => expect(mounted.result.current.canPerform).toBe(true));
    const identity = vi.spyOn(crypto, "randomUUID"), save = vi.spyOn(Storage.prototype, "setItem");
    let pending!: Promise<boolean>; act(() => { pending = mounted.result.current.perform(intent); });
    await waitFor(() => expect(h.reads[stage]).toBe(1)); mounted.unmount();
    await act(async () => { gate.release(); await pending; });
    expect(identity).not.toHaveBeenCalled(); expect(save).not.toHaveBeenCalled(); expect(h.posts).toHaveLength(0); expect(h.refresh).not.toHaveBeenCalled();
  });

  it("held POST A->B cannot mutate B storage/state/refresh and A remains recoverable", async () => {
    const h = install(), gate = held(); h.world.holdPost = gate.promise;
    const mounted = h.mount(); await waitFor(() => expect(mounted.result.current.canPerform).toBe(true));
    let pending!: Promise<boolean>; act(() => { pending = mounted.result.current.perform(intent); });
    await waitFor(() => expect(h.posts).toHaveLength(1)); const a = localStorage.getItem(nativeTransitionRecoveryDraftKey(scope));
    mounted.rerender({ wi: WI_B }); await waitFor(() => expect(mounted.result.current.action?.scope.workerItemId).toBe(WI_B));
    const save = vi.spyOn(Storage.prototype, "setItem"); await act(async () => { gate.release(); await pending; });
    expect(save).not.toHaveBeenCalled(); expect(mounted.result.current.original).toBeNull(); expect(h.refresh).not.toHaveBeenCalled();
    expect(localStorage.getItem(nativeTransitionRecoveryDraftKey(scope))).toBe(a);
    expect(localStorage.getItem(nativeTransitionRecoveryDraftKey({ ...scope, workerItemId: WI_B }))).toBeNull();
    mounted.rerender({ wi: WI }); await waitFor(() => expect(mounted.result.current.original).toEqual(h.posts[0]));
  });

  it("renders live expiry while idle without identity/storage/POST", async () => {
    const h = install(); h.world.session = { ...SESSION, expires_at: new Date(Date.now() + 250).toISOString() };
    const mounted = h.mount(); await waitFor(() => expect(mounted.result.current.authorized).toBe(true));
    const identity = vi.spyOn(crypto, "randomUUID"), save = vi.spyOn(Storage.prototype, "setItem");
    await waitFor(() => expect(mounted.result.current.authorized).toBe(false));
    expect(mounted.result.current.canPerform).toBe(false);
    expect(identity).not.toHaveBeenCalled(); expect(save).not.toHaveBeenCalled(); expect(h.posts).toHaveLength(0);
  });

  it("clears a mounted denial from the completed same-response current-human recheck", async () => {
    const h = install(), mounted = h.mount(); await waitFor(() => expect(mounted.result.current.authorized).toBe(true));
    h.world.session = null; await act(async () => { await mounted.result.current.perform(intent); });
    expect(mounted.result.current.authorized).toBe(false); h.world.session = SESSION;
    const identity = vi.spyOn(crypto, "randomUUID"), save = vi.spyOn(Storage.prototype, "setItem");
    await act(async () => { await mounted.result.current.onSessionAuthorized(SESSION); });
    expect(mounted.result.current.authorized).toBe(true); expect(mounted.result.current.error).toBeNull();
    expect(identity).not.toHaveBeenCalled(); expect(save).not.toHaveBeenCalled(); expect(h.posts).toHaveLength(0);
  });

  it("isolates a foreign-principal recheck from the scoped original", async () => {
    const h = install(); h.world.lost = true;
    const mounted = h.mount(); await waitFor(() => expect(mounted.result.current.canPerform).toBe(true));
    await act(async () => { await mounted.result.current.perform(intent); }); const before = localStorage.getItem(nativeTransitionRecoveryDraftKey(scope));
    h.world.session = { ...SESSION, principal_id: "foreign" };
    await act(async () => { await mounted.result.current.onSessionAuthorized(h.world.session!); });
    await waitFor(() => expect(mounted.result.current.action?.scope.principalId).toBe("foreign"));
    expect(mounted.result.current.original).toBeNull(); expect(localStorage.getItem(nativeTransitionRecoveryDraftKey(scope))).toBe(before);
    expect(h.posts).toHaveLength(1);
  });

  it("keeps a failed current-human recheck denied with the original intact and no writes", async () => {
    const h = install(); h.world.lost = true;
    const mounted = h.mount(); await waitFor(() => expect(mounted.result.current.canPerform).toBe(true));
    await act(async () => { await mounted.result.current.perform(intent); });
    const original = mounted.result.current.original, before = localStorage.getItem(nativeTransitionRecoveryDraftKey(scope));
    h.world.session = null; await act(async () => { await mounted.result.current.lookupOriginal(); });
    const identity = vi.spyOn(crypto, "randomUUID"), save = vi.spyOn(Storage.prototype, "setItem");
    await act(async () => { await mounted.result.current.onSessionAuthorized(SESSION); });
    expect(mounted.result.current.authorized).toBe(false); expect(mounted.result.current.original).toEqual(original);
    expect(localStorage.getItem(nativeTransitionRecoveryDraftKey(scope))).toBe(before);
    expect(identity).not.toHaveBeenCalled(); expect(save).not.toHaveBeenCalled(); expect(h.posts).toHaveLength(1); expect(h.gets).toEqual([]);
  });

  it.each(["foreign-command", "unmapped-result"])("keeps a %s POST original recoverable without a replacement", async condition => {
    const h = install(); h.world.resultChange = condition === "foreign-command" ? { command_id: WI_B } : { aggregate_version: 18 };
    const mounted = h.mount(); await waitFor(() => expect(mounted.result.current.canPerform).toBe(true));
    await act(async () => { await mounted.result.current.perform(intent); await mounted.result.current.perform(intent); });
    expect(mounted.result.current.status).toBe(condition === "foreign-command" ? "unknown" : "applied");
    expect(mounted.result.current.action?.outcome).toBeNull(); expect(mounted.result.current.canPerform).toBe(false);
    h.world.resultChange = null; await act(async () => { await mounted.result.current.lookupOriginal(); });
    expect(mounted.result.current.action?.outcome).toBe("recorded"); expect(h.posts).toHaveLength(1);
    expect(h.gets).toEqual([h.posts[0]!.command_id]);
  });
});
