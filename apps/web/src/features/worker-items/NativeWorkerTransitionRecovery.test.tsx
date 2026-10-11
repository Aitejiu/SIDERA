import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { server } from "@/test/server";
import { workerResultInputsFixture } from "@/test/resultInputFixtures";
import type { CommandRequest, CommandStatusResponse, NativeWorkerContextView } from "../assignments/types";
import type { LocalSession } from "../local-session/api";
import { resultView } from "../results/testFixtures";
import type { WorkerTransition, WorkerWorkspace } from "./types";
import { nativeTransitionRecoveryDraftKey } from "./nativeTransitionRecovery";
import { workerWorkspaceQueryKey } from "./api";
import { WorkerItemPage } from "./WorkerItemPage";

const WS = "20000000-0000-0000-0000-000000000001", WI = "40000000-0000-0000-0000-000000000001";
const WI_B = "40000000-0000-0000-0000-000000000002", ROOT = "30000000-0000-0000-0000-000000000001";
const WT = "a0000000-0000-0000-0000-000000000001", PT = "b0000000-0000-0000-0000-000000000001";
const EX = "60000000-0000-0000-0000-000000000001", ES = "70000000-0000-0000-0000-000000000001";
const PATCH = "c0000000-0000-0000-0000-000000000001";
const SESSION: LocalSession = { session_id: "d0000000-0000-0000-0000-000000000001", workspace_id: WS,
  principal_id: "current-operator", expires_at: "2099-01-01T00:00:00Z" };
const scope = { workspaceId: WS, principalId: SESSION.principal_id, workerItemId: WI };
const path = (wi: string) => `/workspaces/${WS}/worker-items/${wi}`;
function transitionFixture(action = "continue", fallback: string | null = null): WorkerTransition {
  return { worker_transition_id: WT, plan_transition_id: PT, version: 17, action, fallback_action: fallback,
    status: "blocked_unknown", writer_state: "unknown", source_execution_id: EX, source_session_id: ES,
    root_task: { id: ROOT, key: null, path: "root", title: "Root" }, worker_item: { id: WI, key: "worker", path: "root/worker", title: "Worker A" },
    context: { current_context_version: 4, target_context_version: 5, acknowledged_context_version: 4,
      patch_id: action === "patch" ? PATCH : null, patch_hash: action === "patch" ? "a".repeat(64) : null, patch_acknowledged: false },
    subtask: null, path: "root/worker", from_subtask_id: null, to_subtask_id: null, target_execution_id: null, target_session_id: null,
    operations: [], blocked_dependencies: [], queued_input_disposition: { policy: "supersede", queued_count: 0, superseded_count: 0,
      queued_input_ids: [], superseded_input_ids: [] }, failure_code: "adapter_result_unknown", started_at: null, completed_at: null };
}
function workspaceFixture(transition: WorkerTransition): WorkerWorkspace {
  return { workspace_id: WS, worker_item: { id: WI, root_task_id: ROOT, stable_key: "worker", title: "Worker A", responsibility: "Review",
    status: "active", version: 9, context_version: 4, acknowledged_context_version: 4, updated_at: "2026-10-07T00:00:00Z" },
    current_placement: { worker_item_key: "worker", subtask_key: "worker", subtask_task_id: ROOT, role: "implementer",
      capability_requirements: [], plan_generation: 2, plan_snapshot_id: PT },
    plan_sync: { worker_transition_id: WT, plan_transition_id: PT, status: "blocked_unknown", writer_state: "unknown", action: transition.action,
      current_context_version: 4, target_context_version: 5, acknowledged_context_version: 4, patch_id: transition.context.patch_id,
      patch_acknowledged: false, source_execution_id: EX, source_session_id: ES, target_execution_id: null, target_session_id: null,
      continuation_execution_id: null, continuation_session_id: null, source_generation: 1, target_generation: 2,
      queued_input_ids: [], superseded_input_ids: [], failure_code: "adapter_result_unknown", started_at: null, completed_at: null, trace_href: null },
    current_runtime: null, current_step: null, next_action: null, latest_checkpoint: null, placement_history: [], execution_history: [],
    artifacts: [], evidence: [], usage: { input_tokens: 0, output_tokens: 0, cached_tokens: 0, cost_microunits: 0 },
    can_intervene: false, read_only_reason: "no_active_session", intervention_context: null, trace_descriptor: null,
    last_change_cursor: 1, as_of: "2026-10-07T00:00:00Z" };
}
const recordedData = { worker_transition_id: WT, plan_transition_id: PT, version: 17, action: "continue", status: "blocked_unknown",
  writer_state: "unknown", recovery_strategy: "reconcile_adapter", adapter_call: "not_started" };
function install(action = "continue", fallback: string | null = null) {
  const transition = transitionFixture(action, fallback);
  const world = { transition, workspace: workspaceFixture(transition), session: SESSION as LocalSession | null,
    host: "sidera-product", lost: false, workspaceError: false, transitionError: false,
    holdWorkspace: null as Promise<void> | null, holdTransition: null as Promise<void> | null, holdPost: null as Promise<void> | null,
    journal: null as CommandStatusResponse | null, responseVersion: 17, responseData: { ...recordedData } as Record<string, unknown> };
  const posts: CommandRequest[] = [], csrf: Array<string | null> = [], journals: string[] = [], otherWrites: string[] = [];
  const reads = { workspace: 0, transition: 0, completed: 0 };
  const context: NativeWorkerContextView = { workspace_id: WS, worker_item_id: WI, execution_mode: "native_profile",
    worker_item_version: 9, execution_version: null, binding: null, execution: null, context: null, run: null,
    input_enabled: false, read_only_reason: "unsupported_native_capability", as_of: "2026-10-07T00:00:00Z" };
  server.use(
    http.get("/main-agent/v1/health", () => HttpResponse.json({ status: "ok", service: world.host })),
    http.get("/product/v1/session", () => HttpResponse.json(world.session)),
    http.get("/api/v1/workspaces/:workspaceId/assignments/worker_item/:workerItemId", () => HttpResponse.json(null)),
    http.get("/api/v1/workspaces/:workspaceId/worker-items/:workerItemId/native-context", ({ params }) => HttpResponse.json({ ...context, worker_item_id: params.workerItemId })),
    http.get("/api/v1/tasks/:taskId/graph", () => HttpResponse.json({ overview: { title: "Root" }, nodes: [] })),
    http.get("/api/v1/workspaces/:workspaceId/tasks/:taskId/results", () => HttpResponse.json(resultView)),
    http.get("/api/v1/workspaces/:workspaceId/worker-items/:workerItemId/result-inputs", () => {
      const view = workerResultInputsFixture(); view.available_actions = []; return HttpResponse.json(view);
    }),
    http.get("/api/v1/workspaces/:workspaceId/worker-items/:workerItemId/activity", () => HttpResponse.json({ items: [], next_cursor: null })),
    http.get("/api/v1/workspaces/:workspaceId/worker-items/:workerItemId/workspace", async ({ params }) => {
      reads.workspace++; if (params.workerItemId === WI) await world.holdWorkspace;
      reads.completed++;
      if (world.workspaceError && params.workerItemId === WI) return HttpResponse.error();
      return HttpResponse.json(params.workerItemId === WI ? world.workspace : { ...world.workspace,
        worker_item: { ...world.workspace.worker_item, id: WI_B, title: "Worker B" }, plan_sync: null });
    }),
    http.get("/api/v1/workspaces/:workspaceId/worker-transitions/:transitionId", async () => {
      reads.transition++; await world.holdTransition; reads.completed++;
      return world.transitionError ? HttpResponse.error() : HttpResponse.json(world.transition);
    }),
    http.post("/api/v1/commands", async ({ request }) => {
      const command = await request.json() as CommandRequest; posts.push(command); csrf.push(request.headers.get("X-Sidera-CSRF"));
      await world.holdPost;
      return world.lost ? HttpResponse.error() : HttpResponse.json({ command_id: command.command_id, aggregate_id: WT,
        aggregate_version: world.responseVersion, event_ids: ["e0000000-0000-0000-0000-000000000001"], data: world.responseData });
    }),
    http.get("/api/v1/workspaces/:workspaceId/commands/:commandId", ({ params }) => {
      journals.push(String(params.commandId)); const command = posts[0];
      return command ? HttpResponse.json(world.journal ?? { command_id: command.command_id, command_type: command.command_type,
        target_type: command.target_type, target_id: command.target_id, expected_version: command.expected_version, payload: command.payload,
        status: "applied", result: { command_id: command.command_id, aggregate_id: WT, aggregate_version: world.responseVersion,
          event_ids: ["e0000000-0000-0000-0000-000000000001"], data: world.responseData } }) : HttpResponse.error();
    }),
    http.post("*", ({ request }) => { otherWrites.push(new URL(request.url).pathname); return HttpResponse.error(); }),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
  function Subject() {
    const navigate = useNavigate();
    return <><button onClick={() => void navigate(path(WI_B))}>打开 Worker B</button>
      <button onClick={() => void navigate(path(WI))}>打开 Worker A</button><WorkerItemPage /></>;
  }
  const mount = () => render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path(WI)]}>
    <Routes><Route path="/workspaces/:workspaceId/worker-items/:workerItemId" element={<Subject />} /></Routes>
  </MemoryRouter></QueryClientProvider>);
  return { world, posts, csrf, journals, otherWrites, reads, client, mount };
}
async function openReview(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "恢复计划同步" }));
  const dialog = screen.getByRole("dialog", { name: "恢复计划同步" });
  expect(await within(dialog).findByText("Transition V17")).toBeVisible();
  await user.type(within(dialog).getByRole("textbox", { name: "恢复原因" }), "  reviewed source  ");
  await user.type(within(dialog).getByRole("textbox", { name: "证据引用" }), " event:stop , log:lease ");
  await waitFor(() => expect(within(dialog).getByRole("button", { name: "提交恢复策略" })).toBeEnabled());
  return dialog;
}
function held() { let release!: () => void; const promise = new Promise<void>(resolve => { release = resolve; }); return { promise, release }; }
async function settle(release: () => void) { await act(async () => { release(); await new Promise(resolve => setTimeout(resolve, 30)); }); }

describe("actual WorkerItemPage native transition recovery", () => {
  beforeEach(() => { localStorage.clear(); document.cookie = "sidera_csrf=page-current-csrf; Path=/"; });
  afterEach(() => { vi.restoreAllMocks(); document.cookie = "sidera_csrf=; Max-Age=0; Path=/"; });

  it.each([
    { strategy: "reconcile_adapter", action: "continue", fallback: null, choice: "重新核对 Adapter 状态", version: 17,
      data: recordedData, outcome: "恢复请求已记录，Adapter 尚未调用" },
    { strategy: "continue_after_confirmed_stop", action: "continue", fallback: null, choice: "确认停止后继续", version: 18,
      data: { worker_transition_id: WT, plan_transition_id: PT, version: 18, action: "continue", status: "pending", writer_state: "fenced", recovery_strategy: "continue_after_confirmed_stop" }, outcome: "恢复已排队" },
    { strategy: "continue_after_confirmed_patch_absence", action: "patch", fallback: "continue", choice: "确认补丁未落地后继续", version: 18,
      data: { worker_transition_id: WT, plan_transition_id: PT, version: 18, action: "continue", status: "pending", writer_state: "fenced", recovery_strategy: "continue_after_confirmed_patch_absence" }, outcome: "恢复已排队" },
    { strategy: "fail_closed_branch", action: "retire", fallback: null, choice: "将分支安全关闭", version: 18,
      data: { worker_transition_id: WT, plan_transition_id: PT, version: 18, action: "retire", status: "failed", writer_state: "closed", recovery_strategy: "fail_closed_branch" }, outcome: "分支已安全关闭" },
  ])("uses the public human command and exact $strategy outcome", async example => {
    const h = install(example.action, example.fallback); h.world.responseVersion = example.version; h.world.responseData = example.data;
    const user = userEvent.setup(); h.mount(); const dialog = await openReview(user);
    expect(within(dialog).getByText(WI)).toBeVisible(); expect(within(dialog).getByText(ROOT)).toBeVisible();
    await user.click(within(dialog).getByRole("radio", { name: new RegExp(example.choice) }));
    await user.click(within(dialog).getByRole("button", { name: "提交恢复策略" }));
    const record = await screen.findByRole("region", { name: "计划同步恢复记录" });
    expect(await within(record).findByText(example.outcome)).toBeVisible();
    expect(h.posts).toHaveLength(1); expect(h.csrf).toEqual(["page-current-csrf"]);
    expect(h.posts[0]).toMatchObject({ workspace_id: WS, actor: { type: "user", id: "current-operator" },
      command_type: "RecoverWorkerTransition", target_type: "worker_transition", target_id: WT, expected_version: 17,
      payload: { strategy: example.strategy, reason: "reviewed source", evidence_refs: ["event:stop", "log:lease"] } });
    expect(Object.keys(h.posts[0]!.payload).sort()).toEqual(["evidence_refs", "reason", "strategy"]);
    expect(h.otherWrites).toEqual([]); expect(h.posts.map(command => command.command_type)).toEqual(["RecoverWorkerTransition"]);
    expect(within(record).queryByText(/同步成功|任务成功/)).not.toBeInTheDocument();
  });

  it.each(["patch", "create", "move", "preserve"])("does not expose confirmed stop for %s even with a source execution", async action => {
    const h = install(action, action === "patch" ? "continue" : null); const user = userEvent.setup(); h.mount();
    const dialog = await openReview(user);
    expect(within(dialog).queryByRole("radio", { name: /确认停止后继续/ })).not.toBeInTheDocument();
    expect(within(dialog).queryByRole("radio", { name: /确认补丁未落地后继续/ }) !== null).toBe(action === "patch");
    expect(h.posts).toHaveLength(0);
  });

  it("disables submission until the actual displayed Transition read completes", async () => {
    const h = install(), gate = held(); h.world.holdTransition = gate.promise;
    const user = userEvent.setup(); h.mount(); await user.click(await screen.findByRole("button", { name: "恢复计划同步" }));
    const dialog = screen.getByRole("dialog", { name: "恢复计划同步" });
    await user.type(within(dialog).getByRole("textbox", { name: "恢复原因" }), "reviewed");
    await user.type(within(dialog).getByRole("textbox", { name: "证据引用" }), "event:stop");
    expect(within(dialog).getByRole("button", { name: "提交恢复策略" })).toBeDisabled();
    expect(within(dialog).queryByText("Transition V9")).not.toBeInTheDocument(); expect(h.posts).toHaveLength(0);
    await settle(gate.release); expect(await within(dialog).findByText("Transition V17")).toBeVisible();
    expect(within(dialog).getByRole("button", { name: "提交恢复策略" })).toBeEnabled();
  });

  it("recovers lost POST after closed dialog, removed sync, source error and reload using original GET only", async () => {
    const h = install(), user = userEvent.setup(); h.world.lost = true;
    const mounted = h.mount(), dialog = await openReview(user);
    await user.click(within(dialog).getByRole("button", { name: "提交恢复策略" }));
    const record = await screen.findByRole("region", { name: "计划同步恢复记录" });
    expect(await within(record).findByText("原恢复结果未知")).toBeVisible();
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "恢复计划同步" })).not.toBeInTheDocument());
    const original = h.posts[0]!; h.world.workspace.plan_sync = null; h.world.transition.version = 41;
    await act(async () => { await h.client.invalidateQueries({ queryKey: workerWorkspaceQueryKey(WS, WI) }); });
    await waitFor(() => expect(screen.queryByRole("region", { name: "计划同步" })).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: "查询原恢复" })).toBeEnabled();
    h.world.workspaceError = true;
    await act(async () => { await h.client.invalidateQueries({ queryKey: workerWorkspaceQueryKey(WS, WI) }); });
    expect(await screen.findByText("无法读取 WorkerItem 工作区")).toBeVisible();
    expect(screen.getByRole("button", { name: "查询原恢复" })).toBeEnabled();
    mounted.unmount(); h.client.clear(); h.mount();
    const identity = vi.spyOn(crypto, "randomUUID");
    await user.click(await screen.findByRole("button", { name: "查询原恢复" }));
    expect(await screen.findByText("恢复请求已记录，Adapter 尚未调用")).toBeVisible();
    expect(h.journals).toEqual([original.command_id]); expect(h.posts).toHaveLength(1); expect(identity).not.toHaveBeenCalled();
    expect(h.otherWrites).toEqual([]);
    expect(JSON.parse(localStorage.getItem(nativeTransitionRecoveryDraftKey(scope))!).original).toEqual(original);
  });

  it("keeps original outside a manually closed dialog while POST is held", async () => {
    const h = install(), gate = held(), user = userEvent.setup(); h.world.holdPost = gate.promise; h.world.lost = true;
    h.mount(); const dialog = await openReview(user); await user.click(within(dialog).getByRole("button", { name: "提交恢复策略" }));
    await waitFor(() => expect(h.posts).toHaveLength(1));
    await user.click(within(dialog).getByRole("button", { name: "关闭恢复窗口" }));
    expect(screen.queryByRole("dialog", { name: "恢复计划同步" })).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: "计划同步恢复记录" })).toBeVisible();
    await settle(gate.release); expect(await screen.findByText("原恢复结果未知")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "恢复计划同步" }));
    expect(screen.getByRole("dialog", { name: "恢复计划同步" })).toBeVisible();
    expect(screen.getByRole("button", { name: "提交恢复策略" })).toBeDisabled(); expect(h.posts).toHaveLength(1);
  });

  it("maps APPLIED-null only after another exact original GET, with no second POST", async () => {
    const h = install(), user = userEvent.setup(); h.world.lost = true; h.mount(); const dialog = await openReview(user);
    await user.click(within(dialog).getByRole("button", { name: "提交恢复策略" }));
    await screen.findByText("原恢复结果未知"); const original = h.posts[0]!;
    h.world.journal = { command_id: original.command_id, status: "applied", result: null };
    await user.click(screen.getByRole("button", { name: "查询原恢复" }));
    expect(await screen.findByText("原恢复已处理")).toBeVisible();
    expect(screen.queryByText("恢复请求已记录，Adapter 尚未调用")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "恢复计划同步" }));
    expect(screen.getByRole("button", { name: "提交恢复策略" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "关闭恢复窗口" }));
    h.world.journal = null; await user.click(screen.getByRole("button", { name: "查询原恢复" }));
    expect(await screen.findByText("恢复请求已记录，Adapter 尚未调用")).toBeVisible();
    expect(h.journals).toEqual([original.command_id, original.command_id]); expect(h.posts).toHaveLength(1);
  });

  it("submits only once on a double click", async () => {
    const h = install(), gate = held(), user = userEvent.setup(); h.world.holdPost = gate.promise;
    h.mount(); const dialog = await openReview(user);
    await user.dblClick(within(dialog).getByRole("button", { name: "提交恢复策略" }));
    await waitFor(() => expect(h.posts).toHaveLength(1)); await settle(gate.release);
    expect(await screen.findByText("恢复请求已记录，Adapter 尚未调用")).toBeVisible(); expect(h.posts).toHaveLength(1);
  });

  it.each(["denied", "expired", "foreign", "source-drift", "version-drift"])("performs fresh %s rejection with identity0/save0/POST0", async failure => {
    const h = install(), user = userEvent.setup(); h.mount(); const dialog = await openReview(user);
    if (failure === "denied") h.world.session = null;
    if (failure === "expired") h.world.session = { ...SESSION, expires_at: "2000-01-01T00:00:00Z" };
    if (failure === "foreign") h.world.session = { ...SESSION, principal_id: "foreign" };
    if (failure === "source-drift") h.world.transition.source_execution_id = WI_B;
    if (failure === "version-drift") h.world.transition.version = 18;
    const identity = vi.spyOn(crypto, "randomUUID"), save = vi.spyOn(Storage.prototype, "setItem");
    await user.click(within(dialog).getByRole("button", { name: "提交恢复策略" }));
    await waitFor(() => expect(screen.getAllByRole("alert").some(alert => /身份未确认|依据已变化/.test(alert.textContent ?? ""))).toBe(true));
    expect(identity).not.toHaveBeenCalled(); expect(save).not.toHaveBeenCalled(); expect(h.posts).toHaveLength(0); expect(h.otherWrites).toEqual([]);
  });

  it("leaves unknown host visibly read-only without entering legacy recovery", async () => {
    const h = install(), user = userEvent.setup(); h.world.host = "unidentified"; h.mount();
    await user.click(await screen.findByRole("button", { name: "恢复计划同步" }));
    const dialog = screen.getByRole("dialog", { name: "恢复计划同步" });
    await user.type(within(dialog).getByRole("textbox", { name: "恢复原因" }), "reviewed");
    await user.type(within(dialog).getByRole("textbox", { name: "证据引用" }), "event:stop");
    expect(within(dialog).getByRole("button", { name: "提交恢复策略" })).toBeDisabled();
    expect(h.posts).toHaveLength(0); expect(h.reads.transition).toBe(0);
  });

  it("keeps unavailable displayed source read-only and exposes original status independently", async () => {
    const h = install(), user = userEvent.setup(); h.world.transitionError = true; h.mount();
    await user.click(await screen.findByRole("button", { name: "恢复计划同步" }));
    const dialog = screen.getByRole("dialog", { name: "恢复计划同步" });
    expect(await within(dialog).findByText("当前 Transition 暂不可用，请重新打开恢复窗口。" )).toBeVisible();
    expect(within(dialog).getByRole("button", { name: "提交恢复策略" })).toBeDisabled(); expect(h.posts).toHaveLength(0);
  });

  it.each(["load", "save"])("makes storage %s failure visible with POST0", async failure => {
    const h = install(), user = userEvent.setup();
    if (failure === "load") localStorage.setItem(nativeTransitionRecoveryDraftKey(scope), "{bad");
    h.mount();
    if (failure === "load") {
      expect(await screen.findByRole("button", { name: "重试本地记录" })).toBeVisible();
      await user.click(await screen.findByRole("button", { name: "恢复计划同步" }));
      expect(screen.getByRole("button", { name: "提交恢复策略" })).toBeDisabled();
    } else {
      const dialog = await openReview(user);
      vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw Error("private storage"); });
      await user.click(within(dialog).getByRole("button", { name: "提交恢复策略" }));
      expect(await screen.findByRole("button", { name: "重试本地记录" })).toBeVisible();
    }
    expect(h.posts).toHaveLength(0); expect(h.otherWrites).toEqual([]);
  });

  it.each(["workspace", "transition"] as const)("held fresh %s read A->B cannot allocate/save/POST or affect B cache", async stage => {
    const h = install(), user = userEvent.setup(), gate = held(); h.mount(); const dialog = await openReview(user);
    if (stage === "workspace") h.world.holdWorkspace = gate.promise; else h.world.holdTransition = gate.promise;
    const initial = h.reads[stage], identity = vi.spyOn(crypto, "randomUUID"), save = vi.spyOn(Storage.prototype, "setItem");
    await user.click(within(dialog).getByRole("button", { name: "提交恢复策略" }));
    await waitFor(() => expect(h.reads[stage]).toBe(initial + 1));
    await user.click(screen.getByRole("button", { name: "打开 Worker B" }));
    await screen.findByRole("heading", { name: "Worker B" });
    const b = h.client.getQueryData(workerWorkspaceQueryKey(WS, WI_B)); await settle(gate.release);
    expect(identity).not.toHaveBeenCalled(); expect(save).not.toHaveBeenCalled(); expect(h.posts).toHaveLength(0);
    expect(h.client.getQueryData(workerWorkspaceQueryKey(WS, WI_B))).toBe(b);
    expect(localStorage.getItem(nativeTransitionRecoveryDraftKey({ ...scope, workerItemId: WI_B }))).toBeNull();
  });

  it("held POST A->B preserves A original and cannot mutate B record/cache or refresh", async () => {
    const h = install(), user = userEvent.setup(), gate = held(); h.world.holdPost = gate.promise;
    h.mount(); const dialog = await openReview(user); await user.click(within(dialog).getByRole("button", { name: "提交恢复策略" }));
    await waitFor(() => expect(h.posts).toHaveLength(1)); const a = localStorage.getItem(nativeTransitionRecoveryDraftKey(scope));
    await user.click(screen.getByRole("button", { name: "打开 Worker B" })); await screen.findByRole("heading", { name: "Worker B" });
    const b = h.client.getQueryData(workerWorkspaceQueryKey(WS, WI_B)), reads = h.reads.workspace;
    const save = vi.spyOn(Storage.prototype, "setItem"); await settle(gate.release);
    expect(save).not.toHaveBeenCalled(); expect(h.reads.workspace).toBe(reads);
    expect(h.client.getQueryData(workerWorkspaceQueryKey(WS, WI_B))).toBe(b);
    expect(localStorage.getItem(nativeTransitionRecoveryDraftKey(scope))).toBe(a);
    expect(localStorage.getItem(nativeTransitionRecoveryDraftKey({ ...scope, workerItemId: WI_B }))).toBeNull();
    expect(screen.queryByRole("button", { name: "查询原恢复" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "打开 Worker A" }));
    await user.click(await screen.findByRole("button", { name: "查询原恢复" }));
    expect(await screen.findByText("恢复请求已记录，Adapter 尚未调用")).toBeVisible();
    expect(h.posts).toHaveLength(1); expect(h.journals).toEqual([h.posts[0]!.command_id]); expect(h.otherWrites).toEqual([]);
  });

  it("requires a new review when the displayed Transition advances while the dialog is open", async () => {
    const h = install(), user = userEvent.setup(); h.mount(); const dialog = await openReview(user);
    h.world.transition.version = 18;
    await act(async () => { await h.client.invalidateQueries({ queryKey: ["native-transition-recovery-review"] }); });
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "提交恢复策略" })).toBeDisabled());
    expect(within(dialog).getByText("Transition V17")).toBeVisible(); expect(within(dialog).queryByText("Transition V18")).not.toBeInTheDocument();
    expect(h.posts).toHaveLength(0);
    await user.click(within(dialog).getByRole("button", { name: "关闭恢复窗口" }));
    await user.click(screen.getByRole("button", { name: "恢复计划同步" }));
    expect(await screen.findByText("Transition V18")).toBeVisible(); expect(h.posts).toHaveLength(0);
  });

  it.each(["missing-source", "wrong-fallback"])("uses actual strategy eligibility for %s", async condition => {
    const h = install(condition === "wrong-fallback" ? "patch" : "continue", condition === "wrong-fallback" ? "retire" : null);
    if (condition === "missing-source") {
      h.world.transition.source_execution_id = null; h.world.transition.source_session_id = null;
      h.world.workspace.plan_sync!.source_execution_id = null; h.world.workspace.plan_sync!.source_session_id = null;
    }
    const user = userEvent.setup(); h.mount(); const dialog = await openReview(user);
    expect(within(dialog).queryByRole("radio", { name: /确认停止后继续/ })).not.toBeInTheDocument();
    expect(within(dialog).queryByRole("radio", { name: /确认补丁未落地后继续/ })).not.toBeInTheDocument();
    expect(h.posts).toHaveLength(0);
  });
});
