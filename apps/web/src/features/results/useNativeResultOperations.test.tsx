import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { createElement, type PropsWithChildren } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CommandRequest } from "@/features/assignments/types";
import type { LocalSession } from "@/features/local-session/api";
import { workerWorkspaceQueryKey } from "@/features/worker-items/api";
import type { WorkerWorkspace } from "@/features/worker-items/types";
import * as client from "@/shared/api/client";
import { server } from "@/test/server";

import { buildNativeResultOperationCommand, taskResultsQueryKey, type NativeResultOperationInput, type ResultAction, type TaskResultOverview } from "./api";
import { loadNativeResultOperationDraft, nativeResultOperationDraftKey, saveNativeResultOperationDraft, type NativeResultOperationDraft } from "./nativeResultOperations";
import { RESULT_CANDIDATE_ID, RESULT_OPERATION_ID, RESULT_ROOT_TASK_ID, RESULT_WORKSPACE_ID, resultAction, resultView } from "./testFixtures";
import { useNativeResultOperations } from "./useNativeResultOperations";

const workerId = "71000000-0000-0000-0000-000000000021";
const workerB = "71000000-0000-0000-0000-000000000099";
const mappingId = "71000000-0000-0000-0000-000000000040";
const nextId = "71000000-0000-0000-0000-000000000041";
const scope = { workspace_id: RESULT_WORKSPACE_ID, root_task_id: RESULT_ROOT_TASK_ID, worker_item_id: workerId, principal_id: "human-current" };
const session: LocalSession = { session_id: "current-human-session", workspace_id: RESULT_WORKSPACE_ID, principal_id: scope.principal_id, expires_at: "2099-01-01T00:00:00Z" };
const resultsPath = `/api/v1/workspaces/${RESULT_WORKSPACE_ID}/tasks/${RESULT_ROOT_TASK_ID}/results`;
const workspacePath = `/api/v1/workspaces/${RESULT_WORKSPACE_ID}/worker-items/${workerId}/workspace`;
const kinds = ["RequestTaskRework", "AuthorizeAdditionalRework", "RetryResultVerification", "ReintegrateTaskResult"] as const;

function action(kind: typeof kinds[number] = "RequestTaskRework"): ResultAction {
  return { ...resultAction("RequestTaskRework"), command_type: kind,
    target_type: kind === "RetryResultVerification" ? "result_operation" : "task",
    target_id: kind === "RetryResultVerification" ? RESULT_OPERATION_ID : RESULT_ROOT_TASK_ID,
    expected_version: kind === "RetryResultVerification" ? 3 : 7,
    allowed_worker_item_ids: kind === "RequestTaskRework" ? [workerId] : [],
    replacement_result_ids: kind === "ReintegrateTaskResult" ? [mappingId] : [] };
}

function input(kind: typeof kinds[number] = "RequestTaskRework"): NativeResultOperationInput {
  switch (kind) {
    case "RequestTaskRework": return { command_type: kind, payload: { candidate_id: RESULT_CANDIDATE_ID,
      candidate_hash: "c".repeat(64), mode: "user", issue_keys: ["expired-credentials"],
      target_worker_item_ids: [workerId], impact_worker_item_ids: [workerId], reason: "reviewed repair", suggestion: null } };
    case "AuthorizeAdditionalRework": return { command_type: kind, payload: { additional_rounds: 1, additional_seconds: 300, reason: "reviewed budget" } };
    case "RetryResultVerification": return { command_type: kind, payload: { verification_operation_id: RESULT_OPERATION_ID,
      expected_operation_version: 3, expected_result_version: 7, candidate_id: RESULT_CANDIDATE_ID, candidate_hash: "c".repeat(64), reason: "reviewed retry" } };
    case "ReintegrateTaskResult": return { command_type: kind, payload: { source_candidate_id: RESULT_CANDIDATE_ID,
      source_candidate_hash: "c".repeat(64), replacement_result_ids: [mappingId], expected_result_version: 7,
      target_observation_id: null, reason: "reviewed integration" } };
  }
}

function workspace(): WorkerWorkspace {
  return { workspace_id: RESULT_WORKSPACE_ID, worker_item: { id: workerId, root_task_id: RESULT_ROOT_TASK_ID,
    stable_key: "implementation", title: "Implementation", responsibility: "Local repair", status: "active", version: 4,
    context_version: 1, acknowledged_context_version: 1, updated_at: "2026-10-07T00:00:00Z" },
    current_placement: { worker_item_key: "implementation", role: "implementation", subtask_key: "repair",
      subtask_task_id: null, plan_snapshot_id: mappingId, plan_generation: 1, capability_requirements: [] },
    current_runtime: null, current_step: null, next_action: null, latest_checkpoint: null,
    usage: { input_tokens: 0, output_tokens: 0, cached_tokens: 0, cost_microunits: 0 },
    artifacts: [], evidence: [], execution_history: [], placement_history: [], intervention_context: null,
    trace_descriptor: null, can_intervene: false, read_only_reason: "adapter_unavailable",
    last_change_cursor: 19, as_of: "2026-10-07T00:00:00Z" };
}

function receipt(command: CommandRequest) {
  const data: Record<string, Record<string, unknown>> = {
    RequestTaskRework: { rework_record_id: mappingId, request_ids: [nextId], target_worker_item_ids: [workerId], impact_worker_item_ids: [workerId], status: "requested", round_number: null },
    AuthorizeAdditionalRework: { auto_rounds_used: 2, auto_rounds_limit: 4, auto_seconds_limit: 1200, auto_deadline: null },
    RetryResultVerification: { operation_id: nextId, previous_operation_id: RESULT_OPERATION_ID, attempt: 2, status: "pending", execution_request_id: mappingId },
    ReintegrateTaskResult: { source_candidate_id: RESULT_CANDIDATE_ID, execution_request_id: mappingId, input_selection_id: nextId,
      release_id: mappingId, integration_operation_id: nextId, rework_record_id: mappingId, target_observation_id: null, status: "scheduled" },
  };
  return { command_id: command.command_id, aggregate_id: command.command_type === "RetryResultVerification" ? nextId : RESULT_ROOT_TASK_ID,
    aggregate_version: command.command_type === "RetryResultVerification" ? 1 : command.command_type === "RequestTaskRework" ? 7 : 8,
    event_ids: [], data: data[command.command_type] };
}

function routes(kind: typeof kinds[number] = "RequestTaskRework") {
  const observed = { commands: [] as CommandRequest[], lookups: [] as string[], csrf: [] as (string | null)[],
    savedAtPost: [] as unknown[], results: 0, workspaces: 0, health: 0, sessions: 0 };
  server.use(
    http.get("/main-agent/v1/health", () => { observed.health += 1; return HttpResponse.json({ status: "ok", service: "sidera-product" }); }),
    http.get("/product/v1/session", () => { observed.sessions += 1; return HttpResponse.json(session); }),
    http.get(resultsPath, () => { observed.results += 1; return HttpResponse.json(resultView({ available_actions: [action(kind)] })); }),
    http.get(workspacePath, () => { observed.workspaces += 1; return HttpResponse.json(workspace()); }),
    http.post("/api/v1/commands", async ({ request }) => {
      const command = await request.json() as CommandRequest;
      observed.commands.push(command); observed.csrf.push(request.headers.get("X-Sidera-CSRF"));
      observed.savedAtPost.push(loadNativeResultOperationDraft(scope));
      return HttpResponse.json(receipt(command));
    }),
    http.get(`/api/v1/workspaces/${RESULT_WORKSPACE_ID}/commands/:commandId`, ({ params }) => {
      observed.lookups.push(String(params.commandId));
      const command = observed.commands[0]!;
      return HttpResponse.json({ command_id: command.command_id, command_type: command.command_type, target_type: command.target_type,
        target_id: command.target_id, expected_version: command.expected_version, payload: command.payload, status: "applied", result: receipt(command) });
    }),
  );
  return observed;
}

function mount(view: TaskResultOverview | undefined = resultView({ available_actions: [action()] }), wi = workerId,
  refresh: () => Promise<void> = async () => {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const wrapper = ({ children }: PropsWithChildren) => createElement(QueryClientProvider, { client: queryClient }, children);
  const initialProps: { view: TaskResultOverview | undefined; workerItemId: string; canOperateResult: boolean } = {
    view, workerItemId: wi, canOperateResult: true,
  };
  const rendered = renderHook((props: typeof initialProps) => useNativeResultOperations({ workspaceId: RESULT_WORKSPACE_ID,
    rootTaskId: RESULT_ROOT_TASK_ID, ...props, onRefresh: refresh }), { wrapper, initialProps });
  return { ...rendered, queryClient };
}

async function ready(hook: ReturnType<typeof mount>) {
  await waitFor(() => expect(hook.result.current.authorized).toBe(true));
  await waitFor(() => expect(hook.result.current.canSubmit).toBe(true));
}

function deferred() {
  let release!: () => void;
  const promise = new Promise<void>(resolve => { release = resolve; });
  return { promise, release };
}

beforeEach(() => { localStorage.clear(); document.cookie = "sidera_csrf=result-operations-csrf; path=/"; });
afterEach(() => { vi.restoreAllMocks(); document.cookie = "sidera_csrf=; Max-Age=0; path=/"; });

describe("native Worker result-operation owner", () => {
  it("hydrates current authority without identities, business reads, storage writes, or commands", async () => {
    const observed = routes();
    const identity = vi.spyOn(client, "createCommandIdentity");
    const hook = mount(); await ready(hook);
    expect(hook.result.current.authority).toMatchObject({ kind: "product", session });
    expect(identity).not.toHaveBeenCalled();
    expect(observed.results + observed.workspaces + observed.commands.length + observed.lookups.length).toBe(0);
    expect(localStorage.length).toBe(0);
  });

  it("locks same-tick submissions, snapshots input, and saves the original before its sole current-human CSRF POST", async () => {
    const observed = routes(); const hook = mount(); await ready(hook);
    const choices = input();
    await act(async () => {
      const first = hook.result.current.submit(action(), choices);
      const second = hook.result.current.submit(action(), choices);
      choices.payload.reason = "changed during preflight";
      await Promise.all([first, second]);
    });
    expect(observed.commands).toHaveLength(1);
    expect(observed.commands[0]).toMatchObject({ actor: { type: "user", id: scope.principal_id }, expected_version: 7,
      payload: { reason: "reviewed repair", mode: "user", target_worker_item_ids: [workerId], impact_worker_item_ids: [workerId] } });
    expect(observed.savedAtPost).toEqual([{ ok: true, draft: expect.objectContaining({ original: observed.commands[0], status: "pending" }) }]);
    expect(observed.csrf).toEqual(["result-operations-csrf"]);
    expect([observed.health, observed.sessions, observed.results, observed.workspaces]).toEqual([2, 2, 1, 1]);
    expect(hook.result.current.status).toBe("applied");
    expect(hook.result.current.outcome).toBe("rework_requested");
    expect(Object.isFrozen(hook.result.current.original?.payload)).toBe(true);
  });

  it.each(kinds)("uses the actual %s outcome without input-adapter or execution-session admission", async (kind) => {
    const observed = routes(kind); const hook = mount(resultView({ available_actions: [action(kind)] })); await ready(hook);
    await act(() => hook.result.current.submit(action(kind), input(kind)));
    expect(observed.commands).toHaveLength(1);
    expect(hook.result.current.status).toBe("applied"); expect(hook.result.current.mappingAvailable).toBe(true);
    expect(hook.result.current.original?.expected_version).toBe(kind === "RetryResultVerification" ? 3 : 7);
  });

  it("retains inspection of the original UNKNOWN verification as handled, without another attempt", async () => {
    const observed = routes("RetryResultVerification");
    server.use(http.post("/api/v1/commands", async ({ request }) => {
      const command = await request.json() as CommandRequest; observed.commands.push(command);
      return HttpResponse.json({ command_id: command.command_id, aggregate_id: RESULT_OPERATION_ID, aggregate_version: 3,
        event_ids: [], data: { operation_id: RESULT_OPERATION_ID, attempt: 1, status: "unknown", inspection_required: true } });
    }));
    const hook = mount(resultView({ available_actions: [action("RetryResultVerification")] })); await ready(hook);
    await act(() => hook.result.current.submit(action("RetryResultVerification"), input("RetryResultVerification")));
    expect(hook.result.current.status).toBe("applied"); expect(hook.result.current.outcome).toBe("verification_inspection_required");
    await act(() => hook.result.current.submit(action("RetryResultVerification"), input("RetryResultVerification")));
    expect(observed.commands).toHaveLength(1);
  });

  it("keeps a lost original through action removal, candidate change, missing view, and reload, using only original GET", async () => {
    const observed = routes();
    server.use(http.post("/api/v1/commands", async ({ request }) => { observed.commands.push(await request.json() as CommandRequest); return HttpResponse.error(); }));
    const first = mount(); await ready(first); await act(() => first.result.current.submit(action(), input()));
    const original = first.result.current.original;
    expect(first.result.current.status).toBe("unknown");
    first.rerender({ view: resultView({ available_actions: [], current_candidate: null, result_version: 8 }), workerItemId: workerId, canOperateResult: false });
    await act(() => first.result.current.submit(action(), input()));
    first.unmount(); const second = mount();
    second.rerender({ view: undefined, workerItemId: workerId, canOperateResult: false });
    await waitFor(() => expect(second.result.current.original).toEqual(original));
    const identity = vi.spyOn(client, "createCommandIdentity");
    await act(() => second.result.current.lookupOriginal());
    expect(second.result.current.status).toBe("applied"); expect(second.result.current.original).toEqual(original);
    expect(observed.lookups).toEqual([original!.command_id]); expect(observed.commands).toHaveLength(1);
    expect([observed.results, observed.workspaces]).toEqual([1, 1]); expect(identity).not.toHaveBeenCalled();
  });

  it.each(["uncorrelated", "foreign aggregate", "missing mapping"])("keeps an acknowledged %s reply tied to the sole original", async (reply) => {
    const observed = routes();
    server.use(http.post("/api/v1/commands", async ({ request }) => {
      const command = await request.json() as CommandRequest; observed.commands.push(command);
      return HttpResponse.json({ ...receipt(command),
        ...(reply === "uncorrelated" ? { command_id: nextId } : {}),
        ...(reply === "foreign aggregate" ? { aggregate_id: nextId } : {}),
        ...(reply === "missing mapping" ? { data: null } : {}) });
    }));
    const hook = mount(); await ready(hook); await act(() => hook.result.current.submit(action(), input()));
    expect(hook.result.current.status).toBe(reply === "missing mapping" ? "applied" : "unknown");
    expect(hook.result.current.mappingAvailable).toBe(false);
    await act(() => hook.result.current.submit(action(), input()));
    const original = hook.result.current.original!;
    await act(() => hook.result.current.lookupOriginal());
    expect(observed.commands).toHaveLength(1); expect(observed.lookups).toEqual([original.command_id]);
    expect(hook.result.current.mappingAvailable).toBe(true); expect(hook.result.current.original).toEqual(original);
  });

  it("rejects a same-scope action removed while canonical preflight is held, before identity or storage", async () => {
    const observed = routes(); const held = deferred();
    server.use(http.get(resultsPath, async () => { observed.results += 1; await held.promise;
      return HttpResponse.json(resultView({ available_actions: [action()] })); }));
    const hook = mount(); await ready(hook); const identity = vi.spyOn(client, "createCommandIdentity");
    let submitting!: Promise<unknown>; act(() => { submitting = hook.result.current.submit(action(), input()); });
    await waitFor(() => expect(observed.results).toBe(1));
    hook.rerender({ view: resultView({ available_actions: [] }), workerItemId: workerId, canOperateResult: true });
    await act(async () => { held.release(); await submitting; });
    expect(observed.commands).toHaveLength(0); expect(identity).not.toHaveBeenCalled(); expect(localStorage.length).toBe(0);
  });

  it.each(["removed action", "changed candidate", "changed result version", "foreign root"])("rejects canonical %s before identity or POST", async (change) => {
    const observed = routes(); const hook = mount(); await ready(hook);
    server.use(http.get(resultsPath, () => HttpResponse.json(resultView({
      ...(change === "removed action" ? { available_actions: [] } : { available_actions: [action()] }),
      ...(change === "changed candidate" ? { current_candidate: null } : {}),
      ...(change === "changed result version" ? { result_version: 8 } : {}),
      ...(change === "foreign root" ? { root_task_id: workerB } : {}),
    }))));
    const identity = vi.spyOn(client, "createCommandIdentity");
    await act(() => hook.result.current.submit(action(), input()));
    expect(observed.commands).toHaveLength(0); expect(identity).not.toHaveBeenCalled(); expect(localStorage.length).toBe(0);
    expect(hook.result.current.error).toBe("结果操作已变化，请重新审阅");
  });

  it.each(["paused", "retired", "context_not_acknowledged", "projection_stale", "foreign Wi", "foreign root", "foreign workspace"])("rejects canonical Worker scope %s with POST0", async (change) => {
    const observed = routes(); const hook = mount(); await ready(hook);
    const current = workspace();
    if (["paused", "retired"].includes(change)) current.worker_item.status = change;
    if (["context_not_acknowledged", "projection_stale"].includes(change)) current.read_only_reason = change;
    if (change === "foreign Wi") current.worker_item.id = workerB;
    if (change === "foreign root") current.worker_item.root_task_id = workerB;
    if (change === "foreign workspace") current.workspace_id = workerB;
    server.use(http.get(workspacePath, () => HttpResponse.json(current)));
    const identity = vi.spyOn(client, "createCommandIdentity");
    await act(() => hook.result.current.submit(action(), input()));
    expect(observed.commands).toHaveLength(0); expect(identity).not.toHaveBeenCalled();
  });

  it.each(["expired", "foreign", "unknown host", "unavailable host"])("rejects fresh %s authority with POST0", async (change) => {
    const observed = routes(); const hook = mount(); await ready(hook);
    if (change === "expired" || change === "foreign") server.use(http.get("/product/v1/session", () => HttpResponse.json({ ...session,
      ...(change === "expired" ? { expires_at: "2000-01-01T00:00:00Z" } : { principal_id: "foreign-human" }) })));
    else server.use(http.get("/main-agent/v1/health", () => change === "unknown host" ? HttpResponse.json({ status: "ok", service: "unknown-native" }) : HttpResponse.error()));
    const identity = vi.spyOn(client, "createCommandIdentity");
    await act(() => hook.result.current.submit(action(), input()));
    expect(observed.commands).toHaveLength(0); expect(identity).not.toHaveBeenCalled(); expect(localStorage.length).toBe(0);
  });

  it("does not allocate or save from a held A preflight released after unmount and B mount", async () => {
    const observed = routes(); const held = deferred();
    server.use(http.get(resultsPath, async () => { observed.results += 1; await held.promise; return HttpResponse.json(resultView({ available_actions: [action()] })); }));
    const first = mount(); await ready(first); const identity = vi.spyOn(client, "createCommandIdentity");
    let submitting!: Promise<unknown>;
    act(() => { submitting = first.result.current.submit(action(), input()); });
    await waitFor(() => expect(observed.results).toBe(1)); first.unmount();
    const second = mount(undefined, workerB); second.rerender({ view: undefined, workerItemId: workerB, canOperateResult: true });
    await waitFor(() => expect(second.result.current.authorized).toBe(true));
    await act(async () => { held.release(); await submitting; });
    expect(identity).not.toHaveBeenCalled(); expect(localStorage.length).toBe(0); expect(observed.commands).toHaveLength(0);
    expect(second.result.current.original).toBeNull();
  });

  it("leaves the pending A original recoverable and B caches untouched when a held POST completes after unmount", async () => {
    const observed = routes(); const held = deferred(); const refresh = vi.fn(async () => {});
    server.use(http.post("/api/v1/commands", async ({ request }) => { const command = await request.json() as CommandRequest;
      observed.commands.push(command); await held.promise; return HttpResponse.json(receipt(command)); }));
    const first = mount(undefined, workerId, refresh); await ready(first);
    let submitting!: Promise<unknown>; act(() => { submitting = first.result.current.submit(action(), input()); });
    await waitFor(() => expect(observed.commands).toHaveLength(1));
    const original = observed.commands[0]!; first.unmount(); const second = mount(undefined, workerB);
    second.rerender({ view: undefined, workerItemId: workerB, canOperateResult: true });
    const resultKey = taskResultsQueryKey(RESULT_WORKSPACE_ID, RESULT_ROOT_TASK_ID);
    const workspaceKey = workerWorkspaceQueryKey(RESULT_WORKSPACE_ID, workerB);
    second.queryClient.setQueryData(resultKey, "B result cache"); second.queryClient.setQueryData(workspaceKey, "B worker cache");
    await act(async () => { held.release(); await submitting; });
    expect(refresh).not.toHaveBeenCalled(); expect(second.result.current.original).toBeNull();
    expect(second.queryClient.getQueryState(resultKey)?.isInvalidated).toBe(false);
    expect(second.queryClient.getQueryData(workspaceKey)).toBe("B worker cache");
    expect(loadNativeResultOperationDraft(scope)).toMatchObject({ ok: true, draft: { status: "pending", original } });
    const reopened = mount(); await waitFor(() => expect(reopened.result.current.original).toEqual(original));
    await act(() => reopened.result.current.lookupOriginal()); expect(reopened.result.current.status).toBe("applied");
    expect(observed.commands).toHaveLength(1); expect(observed.lookups).toEqual([original.command_id]);
  });

  it("retains acknowledged handling when current-view refresh fails", async () => {
    const observed = routes(); const hook = mount(undefined, workerId, async () => { throw new Error("private provider detail"); }); await ready(hook);
    await act(() => hook.result.current.submit(action(), input()));
    expect(hook.result.current.status).toBe("applied"); expect(hook.result.current.mappingAvailable).toBe(true);
    expect(hook.result.current.error).toBe("结果操作已确认，当前成果暂未刷新"); expect(observed.commands).toHaveLength(1);
  });

  it("keeps an outcome-write failure's submitted original and retries storage without another command", async () => {
    const observed = routes(); const hook = mount(); await ready(hook);
    const originalSet = Storage.prototype.setItem;
    const set = vi.spyOn(Storage.prototype, "setItem"); let writes = 0;
    set.mockImplementation(function (this: Storage, key: string, value: string) { writes += 1;
      if (writes === 2) throw new Error("private quota detail"); originalSet.call(this, key, value); });
    await act(() => hook.result.current.submit(action(), input()));
    expect(hook.result.current.status).toBe("applied"); expect(hook.result.current.original).toEqual(observed.commands[0]);
    expect(hook.result.current.storageError).toBe("本地结果操作记录暂不可用");
    set.mockRestore(); act(() => hook.result.current.retryStorage());
    expect(hook.result.current.storageError).toBeNull(); expect(observed.commands).toHaveLength(1); expect(observed.lookups).toHaveLength(0);
  });

  it.each(["different original", "identical original/review", "same ID changed payload", "same ID changed review", "empty draft", "failed read"])(
    "storage reload outcome identity: %s", async (replacement) => {
      const observed = routes("AuthorizeAdditionalRework");
      const hook = mount(resultView({ available_actions: [action("AuthorizeAdditionalRework")] })); await ready(hook);
      const identity = vi.spyOn(client, "createCommandIdentity");
      await act(() => hook.result.current.submit(action("AuthorizeAdditionalRework"), input("AuthorizeAdditionalRework")));
      expect(identity).toHaveBeenCalledTimes(1); expect(observed.commands).toHaveLength(1);
      expect(hook.result.current.mappingAvailable).toBe(true); expect(hook.result.current.outcome).toBe("budget_authorized");
      const acknowledged = hook.result.current.draft!;
      hook.rerender({ view: resultView({ available_actions: [action()] }), workerItemId: workerId, canOperateResult: true });
      server.use(http.get(resultsPath, () => HttpResponse.json(resultView({ available_actions: [action()] }))));
      const set = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("private initial-save detail"); });
      identity.mockClear(); await act(() => hook.result.current.submit(action(), input()));
      expect(identity).toHaveBeenCalledTimes(1); expect(observed.commands).toHaveLength(1);
      expect(hook.result.current.original).toEqual(acknowledged.original);
      expect(hook.result.current.storageError).toBe("本地结果操作记录暂不可用");
      expect(hook.result.current.outcome).toBe("budget_authorized"); set.mockRestore();

      let stored: NativeResultOperationDraft | null = acknowledged;
      identity.mockClear();
      if (["different original", "same ID changed payload", "same ID changed review"].includes(replacement)) {
        const budgetAction = action("AuthorizeAdditionalRework");
        if (replacement === "same ID changed review") budgetAction.plan_content_hash = null;
        const budgetInput = input("AuthorizeAdditionalRework");
        if (replacement !== "same ID changed review") budgetInput.payload.reason = "another reviewed disk command";
        const built = buildNativeResultOperationCommand(RESULT_WORKSPACE_ID, RESULT_ROOT_TASK_ID, workerId, budgetAction, budgetInput, session);
        expect(identity).toHaveBeenCalledTimes(1);
        stored = { ...acknowledged,
          original: replacement === "different original" ? built : { ...acknowledged.original, payload: built.payload },
          review: { ...acknowledged.review, plan_content_hash: budgetAction.plan_content_hash }, status: "applied" };
        expect(saveNativeResultOperationDraft(scope, stored)).toEqual({ ok: true });
      } else if (replacement === "empty draft") {
        stored = null; localStorage.removeItem(nativeResultOperationDraftKey(scope));
      } else {
        expect(identity).not.toHaveBeenCalled();
      }
      const get = replacement === "failed read" ? vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("private read detail"); }) : null;
      const retained = ["identical original/review", "failed read"].includes(replacement);
      identity.mockClear(); act(() => hook.result.current.retryStorage());
      expect(identity).not.toHaveBeenCalled(); expect(observed.commands).toHaveLength(1);
      expect(hook.result.current.original).toEqual(stored?.original ?? null);
      expect(hook.result.current.mappingAvailable).toBe(retained);
      expect(hook.result.current.outcome).toBe(retained ? "budget_authorized" : null);
      expect(hook.result.current.canSubmit).toBe(replacement === "identical original/review" || replacement === "empty draft");
      get?.mockRestore();
      if (stored !== null && !retained) {
        const original = stored.original;
        server.use(http.get(`/api/v1/workspaces/${RESULT_WORKSPACE_ID}/commands/:commandId`, ({ params }) => {
          observed.lookups.push(String(params.commandId));
          return HttpResponse.json({ command_id: original.command_id, command_type: original.command_type,
            target_type: original.target_type, target_id: original.target_id, expected_version: original.expected_version,
            payload: original.payload, status: "applied", result: null });
        }));
        await act(() => hook.result.current.lookupOriginal());
        expect(observed.lookups).toEqual([original.command_id]); expect(identity).not.toHaveBeenCalled();
        expect(hook.result.current.mappingAvailable).toBe(false); expect(hook.result.current.outcome).toBeNull();
        expect(hook.result.current.canSubmit).toBe(false); expect(hook.result.current.original).toEqual(original);
        await act(() => hook.result.current.submit(action(), input()));
        expect(observed.commands).toHaveLength(1); expect(identity).not.toHaveBeenCalled();
      }
    },
  );

  it("exposes storage acquisition recovery with POST0 and no automatic reset", async () => {
    const observed = routes(); const get = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("private storage"); });
    const hook = mount(); await waitFor(() => expect(hook.result.current.storageError).toBe("本地结果操作记录暂不可用"));
    await act(() => hook.result.current.submit(action(), input())); expect(observed.commands).toHaveLength(0);
    get.mockRestore(); act(() => hook.result.current.retryStorage()); expect(hook.result.current.storageError).toBeNull();
    expect(observed.commands).toHaveLength(0);
  });

  it("requires pending storage success before POST and never submits on storage retry alone", async () => {
    const observed = routes(); const hook = mount(); await ready(hook);
    const set = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("private quota"); });
    await act(() => hook.result.current.submit(action(), input()));
    expect(observed.commands).toHaveLength(0); expect(hook.result.current.original).toBeNull();
    expect(hook.result.current.storageError).toBe("本地结果操作记录暂不可用");
    set.mockRestore(); act(() => hook.result.current.retryStorage());
    expect(hook.result.current.storageError).toBeNull(); expect(observed.commands).toHaveLength(0);
  });

  it.each(["results", "workspace"])("keeps failed fresh %s reads before identity and POST", async (read) => {
    const observed = routes(); const hook = mount(); await ready(hook);
    server.use(http.get(read === "results" ? resultsPath : workspacePath, () => HttpResponse.error()));
    const identity = vi.spyOn(client, "createCommandIdentity");
    await act(() => hook.result.current.submit(action(), input()));
    expect(observed.commands).toHaveLength(0); expect(identity).not.toHaveBeenCalled();
    expect(hook.result.current.error).not.toContain("private");
  });

  it.each(["pending", "rejected", "missing mapping", "foreign metadata", "network error"])("handles original journal %s without reposting or inventing rejection", async (reply) => {
    const observed = routes(); server.use(http.post("/api/v1/commands", async ({ request }) => { observed.commands.push(await request.json() as CommandRequest); return HttpResponse.error(); }));
    const hook = mount(); await ready(hook); await act(() => hook.result.current.submit(action(), input()));
    const original = hook.result.current.original!;
    server.use(http.get(`/api/v1/workspaces/${RESULT_WORKSPACE_ID}/commands/:commandId`, ({ params }) => {
      observed.lookups.push(String(params.commandId)); if (reply === "network error") return HttpResponse.error();
      return HttpResponse.json({ command_id: original.command_id, command_type: original.command_type, target_type: original.target_type,
        target_id: reply === "foreign metadata" ? workerB : original.target_id, expected_version: original.expected_version,
        status: reply === "pending" || reply === "rejected" ? reply : "applied", result: null });
    }));
    await act(() => hook.result.current.lookupOriginal());
    expect(hook.result.current.status).toBe(reply === "rejected" ? "rejected" : reply === "pending" ? "pending" : reply === "missing mapping" ? "applied" : "unknown");
    expect(hook.result.current.original).toEqual(original); expect(observed.commands).toHaveLength(1); expect(observed.lookups).toEqual([original.command_id]);
    if (reply !== "rejected") await act(() => hook.result.current.submit(action(), input()));
    expect(observed.commands).toHaveLength(1);
  });

  it("denies original GET to a changed current principal and never exposes another principal's saved draft", async () => {
    const observed = routes(); server.use(http.post("/api/v1/commands", async ({ request }) => { observed.commands.push(await request.json() as CommandRequest); return HttpResponse.error(); }));
    const hook = mount(); await ready(hook); await act(() => hook.result.current.submit(action(), input()));
    server.use(http.get("/product/v1/session", () => HttpResponse.json({ ...session, principal_id: "foreign-human" })));
    await act(() => hook.result.current.lookupOriginal()); expect(observed.lookups).toHaveLength(0);
    hook.unmount(); const foreign = mount(); await ready(foreign); expect(foreign.result.current.original).toBeNull();
    expect(localStorage.getItem(nativeResultOperationDraftKey(scope))).not.toBeNull();
  });
});
