import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CommandRequest } from "../assignments/types";
import type { LocalSession } from "../local-session/api";
import { nativeAttentionDraftKey, type NativeAttentionItem } from "../attention/nativeAttentionAcknowledgement";
import { resultView } from "../results/testFixtures";
import { server } from "@/test/server";
import { taskGraphQueryKey } from "./api";
import { TaskDetailPage } from "./TaskDetailPage";
import type { TaskGraph, TaskView } from "./types";

const WS = "20000000-0000-0000-0000-000000000001", ROOT = "30000000-0000-0000-0000-000000000001";
const OTHER = "30000000-0000-0000-0000-000000000002", WORKER = "40000000-0000-0000-0000-000000000001";
const SESSION: LocalSession = { session_id: "operator-session", workspace_id: WS, principal_id: "operator", expires_at: "2099-01-01T00:00:00Z" };
const ITEM: NativeAttentionItem = {
  key: "blocked:worker", kind: "blocked", correlation_id: "50000000-0000-0000-0000-000000000001",
  resource_id: WORKER, resource_version: 3, occurred_at: "2026-10-07T00:00:00Z", requires_operator: true,
  task_title: "Attention Task", subtask_key: "worker", subtask_title: "Review worker", reason: "Current source needs review", worker_item_id: WORKER,
};
const TASK: TaskView = { id: ROOT, workspace_id: WS, title: "Attention Task", description: "Review", acceptance_criteria: [], requested_by: "operator", priority: 1, status: "running", version: 11, created_at: "2026-10-07T00:00:00Z", updated_at: "2026-10-07T00:00:00Z" };
const GRAPH: TaskGraph = { schema_version: 1, nodes: [], edges: [], milestones: [], overview: {
  schema_version: 1, workspace_id: WS, root_task_id: ROOT, title: "Attention Task", goal_summary: "Review", plan_snapshot_id: OTHER,
  generation: 1, acceptance_refs: [], current_milestone_key: null, subtask_count: 0, planned_subtask_count: 0,
  running_subtask_count: 0, completed_subtask_count: 0, failed_subtask_count: 0, handoff_blocked_count: 0,
  handoff_running_count: 0, next_subtask_keys: [], attention_items: [ITEM], last_change_cursor: 1, as_of: "2026-10-07T00:00:00Z",
} };
const scope = { workspaceId: WS, rootTaskId: ROOT, principalId: "operator" };
const reply = (command: CommandRequest) => ({ command_id: command.command_id, aggregate_id: command.target_id, aggregate_version: command.expected_version, event_ids: [], data: {} });
function deferred() { let release!: () => void; const promise = new Promise<void>(resolve => { release = resolve; }); return { promise, release }; }
function at<T>(values: readonly T[], index = 0): T {
  const value = values[index];
  if (value === undefined) throw new Error("Expected test fixture entry");
  return value;
}

function install() {
  const world = {
    health: { status: "ok", service: "sidera-product" }, session: SESSION as LocalSession | null,
    healthFail: false, taskFail: false, graphFail: false, lost: false, appliedNull: false,
    task: structuredClone(TASK), graph: structuredClone(GRAPH),
    holdHealth: undefined as Promise<void> | undefined, holdTask: undefined as Promise<void> | undefined,
    holdGraph: undefined as Promise<void> | undefined, holdPost: undefined as Promise<void> | undefined,
    response: reply as (command: CommandRequest) => Record<string, unknown>,
  };
  const posted: CommandRequest[] = [], csrf: (string | null)[] = [], persisted: (string | null)[] = [], gets: string[] = [], writes: string[] = [];
  const graphReads: Record<string, number> = {}, taskReads: Record<string, number> = {};
  let healthReads = 0;
  server.use(
    http.get("/main-agent/v1/health", async () => { healthReads++; await world.holdHealth; return world.healthFail ? new HttpResponse(null, { status: 503 }) : HttpResponse.json(world.health); }),
    http.get("/product/v1/session", () => world.session === null ? new HttpResponse(null, { status: 401 }) : HttpResponse.json(world.session)),
    http.get("/api/v1/tasks/:taskId", async ({ params }) => {
      const id = String(params.taskId); taskReads[id] = (taskReads[id] ?? 0) + 1;
      if (id === ROOT) await world.holdTask;
      return id === ROOT && world.taskFail ? new HttpResponse(null, { status: 503 }) : HttpResponse.json(id === ROOT ? world.task : { ...TASK, id, title: "Task B" });
    }),
    http.get("/api/v1/tasks/:taskId/graph", async ({ params }) => {
      const id = String(params.taskId); graphReads[id] = (graphReads[id] ?? 0) + 1;
      if (id === ROOT) await world.holdGraph;
      return id === ROOT && world.graphFail ? new HttpResponse(null, { status: 503 })
        : HttpResponse.json(id === ROOT ? world.graph : { ...GRAPH, overview: { ...GRAPH.overview, root_task_id: id, title: "Task B" } });
    }),
    http.get("/api/v1/tasks/:taskId/activity", () => HttpResponse.json({ schema_version: 1, last_change_cursor: 1, items: [], next_cursor: null })),
    http.get("/api/v1/workspaces/:workspaceId/tasks/:taskId/results", () => HttpResponse.json(resultView())),
    http.post("/api/v1/commands", async ({ request }) => {
      const original = await request.json() as CommandRequest;
      posted.push(original); writes.push(new URL(request.url).pathname); csrf.push(request.headers.get("X-Sidera-CSRF"));
      persisted.push(localStorage.getItem(nativeAttentionDraftKey(scope))); await world.holdPost;
      return world.lost ? HttpResponse.error() : HttpResponse.json(world.response(original));
    }),
    http.get("/api/v1/workspaces/:workspaceId/commands/:commandId", ({ params }) => {
      gets.push(String(params.commandId)); const original = posted[0];
      if (!original) return new HttpResponse(null, { status: 404 });
      return HttpResponse.json({ command_id: original.command_id, command_type: original.command_type, target_type: original.target_type,
        target_id: original.target_id, expected_version: original.expected_version, payload: original.payload, status: "applied", result: world.appliedNull ? null : reply(original) });
    }),
    http.all("*", ({ request }) => { if (request.method !== "GET") writes.push(new URL(request.url).pathname); return new HttpResponse(null, { status: 404 }); }),
  );
  return { world, posted, csrf, persisted, gets, writes, graphReads, taskReads, healthReads: () => healthReads };
}
function Navigation() { const navigate = useNavigate(); return <button onClick={() => navigate(`/workspaces/${WS}/tasks/${OTHER}`)}>Next task</button>; }
function mount(view = "", rootTaskId = ROOT) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const page = render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[`/workspaces/${WS}/tasks/${rootTaskId}${view}`]}>
    <Routes><Route path="/workspaces/:workspaceId/tasks/:rootTaskId" element={<TaskDetailPage />} /><Route path="*" element={null} /></Routes><Navigation />
  </MemoryRouter></QueryClientProvider>);
  return { ...page, client };
}
async function ready(title = "Attention Task") {
  expect(await screen.findByRole("heading", { name: title })).toBeVisible();
  await waitFor(() => expect(screen.getByRole("button", { name: "确认已处理" })).toBeEnabled());
}
async function click() { await userEvent.setup().click(screen.getByRole("button", { name: "确认已处理" })); }
async function unknown() { expect(await screen.findByText("原确认结果未知")).toBeVisible(); await waitFor(() => expect(screen.getByRole("button", { name: "查询原确认" })).toBeEnabled()); }

describe("actual TaskDetailPage native attention", () => {
  beforeEach(() => { localStorage.clear(); document.cookie = "sidera_csrf=page-current-csrf; Path=/"; });
  afterEach(() => { vi.restoreAllMocks(); document.cookie = "sidera_csrf=; Max-Age=0; Path=/"; });

  it("uses current Product human authority and unchanged Task11 before locally hiding the exact item", async () => {
    const h = install(); mount(); await ready(); await click();
    expect(await screen.findByText("已确认处理")).toBeVisible();
    await waitFor(() => expect(screen.queryByText(ITEM.reason)).not.toBeInTheDocument());
    expect(h.posted).toHaveLength(1); expect(h.csrf).toEqual(["page-current-csrf"]);
    expect(at(h.posted)).toMatchObject({ workspace_id: WS, command_type: "AcknowledgeAttentionItem", actor: { type: "user", id: "operator" }, target_type: "task", target_id: ROOT, expected_version: 11 });
    expect(at(h.posted).payload).toEqual({ attention_key: ITEM.key, source_correlation_id: ITEM.correlation_id, source_resource_version: 3 });
    expect(JSON.parse(h.persisted[0]!)).toMatchObject({ original: at(h.posted), status: "pending" });
    expect(h.writes).toEqual(["/api/v1/commands"]);
  });

  it("preserves an unknown row and original across unmount/reload then GETs it after the row disappears", async () => {
    const h = install(); h.world.lost = true; const page = mount(); await ready(); await click(); await unknown();
    expect(screen.getByText(ITEM.reason)).toBeVisible(); const stored = localStorage.getItem(nativeAttentionDraftKey(scope)); page.unmount();
    h.world.graph.overview.attention_items = []; mount();
    const get = await screen.findByRole("button", { name: "查询原确认" });
    expect(await screen.findByText("当前没有待确认事项")).toBeVisible();
    const identity = vi.spyOn(crypto, "randomUUID"); await userEvent.setup().click(get);
    expect(await screen.findByText("已确认处理")).toBeVisible();
    expect(h.gets).toEqual([at(h.posted).command_id]); expect(h.posted).toHaveLength(1); expect(identity).not.toHaveBeenCalled();
    expect(stored).toContain(at(h.posted).command_id); expect(h.writes).toEqual(["/api/v1/commands"]);
  });

  it("keeps recovery before graph failure and preserves mapping when the follow-up graph read also fails", async () => {
    const h = install(); h.world.lost = true; const first = mount(); await ready(); await click(); await unknown(); first.unmount();
    h.world.graphFail = true; mount(); expect(await screen.findByText("无法读取任务图")).toBeVisible();
    const recovery = await screen.findByRole("region", { name: "待处理事项确认记录" });
    const error = screen.getByText("无法读取任务图"); expect(recovery.compareDocumentPosition(error) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    await userEvent.setup().click(await screen.findByRole("button", { name: "查询原确认" }));
    expect(await screen.findByText("已确认处理")).toBeVisible(); expect(screen.getByText("无法读取任务图")).toBeVisible();
    expect(h.gets).toEqual([at(h.posted).command_id]); expect(h.posted).toHaveLength(1);
  });

  it("keeps the same recovery owner when switching board, DAG and results", async () => {
    const h = install(); h.world.lost = true; mount(); await ready(); await click(); await unknown(); const user = userEvent.setup();
    for (const view of ["DAG", "成果", "看板"]) {
      await user.click(screen.getByRole("tab", { name: view }));
      expect(screen.getByRole("button", { name: "查询原确认" })).toBeVisible(); expect(screen.getByText("原确认结果未知")).toBeVisible();
    }
    await user.click(screen.getByRole("button", { name: "查询原确认" }));
    expect(await screen.findByText("已确认处理")).toBeVisible(); expect(h.posted).toHaveLength(1); expect(h.writes).toEqual(["/api/v1/commands"]);
  });

  it("allows only one POST for same-tick repeated clicks on the real page", async () => {
    const h = install(), hold = deferred(); h.world.holdPost = hold.promise; mount(); await ready();
    const button = screen.getByRole("button", { name: "确认已处理" });
    act(() => { fireEvent.click(button); fireEvent.click(button); });
    await waitFor(() => expect(h.posted).toHaveLength(1));
    hold.release(); expect(await screen.findByText("已确认处理")).toBeVisible(); expect(h.posted).toHaveLength(1);
  });

  it.each([
    ["denied", null], ["expired", { ...SESSION, expires_at: "2000-01-01T00:00:00Z" }],
    ["foreign", { ...SESSION, principal_id: "foreign" }],
  ])("blocks fresh %s authority with no identity, persistence or POST", async (_name, session) => {
    const h = install(); mount(); await ready(); h.world.session = session;
    const identity = vi.spyOn(crypto, "randomUUID"), save = vi.spyOn(Storage.prototype, "setItem"); await click();
    expect(await screen.findByText("当前操作身份未确认，请重新授权本地会话。")).toBeVisible();
    expect(screen.getByText(ITEM.reason)).toBeVisible(); expect(identity).not.toHaveBeenCalled(); expect(save).not.toHaveBeenCalled(); expect(h.posted).toHaveLength(0);
  });

  it.each(["host unavailable", "unrecognized host", "missing session", "expired session", "foreign workspace"])("never uses legacy writes on initial %s", async (kind) => {
    const h = install();
    if (kind === "host unavailable") h.world.healthFail = true;
    if (kind === "unrecognized host") h.world.health = { status: "ok", service: "unknown" };
    if (kind === "missing session") h.world.session = null;
    if (kind === "expired session") h.world.session = { ...SESSION, expires_at: "2000-01-01T00:00:00Z" };
    if (kind === "foreign workspace") h.world.session = { ...SESSION, workspace_id: OTHER };
    mount(); expect(await screen.findByRole("heading", { name: "Attention Task" })).toBeVisible();
    expect(await screen.findByText("当前操作身份未确认")).toBeVisible();
    expect(screen.getByRole("button", { name: "确认已处理" })).toBeDisabled(); await click();
    expect(h.writes).toEqual([]); expect(screen.getByRole("button", { name: "本地会话" })).toBeVisible();
  });

  it.each(["task", "graph"])("blocks a failed fresh %s read without dropping the displayed row", async (source) => {
    const h = install(); mount(); await ready(); h.world.taskFail = source === "task"; h.world.graphFail = source === "graph";
    const identity = vi.spyOn(crypto, "randomUUID"); await click();
    expect(await screen.findByText("当前待处理事项暂不可用，请重新读取。")).toBeVisible();
    expect(screen.getByText(ITEM.reason)).toBeVisible(); expect(identity).not.toHaveBeenCalled(); expect(h.posted).toHaveLength(0);
  });

  it("blocks an unreadable original slot", async () => {
    const h = install(); vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("private read"); });
    mount(); expect(await screen.findByRole("heading", { name: "Attention Task" })).toBeVisible();
    expect(await screen.findByRole("button", { name: "重试本地记录" })).toBeVisible();
    expect(screen.getByRole("button", { name: "确认已处理" })).toBeDisabled(); expect(h.posted).toHaveLength(0);
  });

  it("blocks a failed initial save and keeps an unsent command out of recovery", async () => {
    const h = install(); mount(); await ready(); vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("private save"); });
    await click(); expect(await screen.findByRole("button", { name: "重试本地记录" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "查询原确认" })).not.toBeInTheDocument(); expect(h.posted).toHaveLength(0); expect(screen.getByText(ITEM.reason)).toBeVisible();
  });

  it("keeps applied-null GET visible and never locally hides its row", async () => {
    const h = install(); h.world.lost = true; h.world.appliedNull = true; mount(); await ready(); await click(); await unknown();
    await userEvent.setup().click(screen.getByRole("button", { name: "查询原确认" }));
    expect(await screen.findByText("结果尚未映射")).toBeVisible();
    expect(screen.queryByText("已确认处理")).not.toBeInTheDocument(); expect(screen.getByText(ITEM.reason)).toBeVisible();
    expect(screen.getByRole("button", { name: "查询原确认" })).toBeEnabled(); expect(screen.getByRole("button", { name: "确认已处理" })).toBeDisabled();
    expect(h.posted).toHaveLength(1);
  });

  it.each(["version", "correlation", "resource", "removed"])("blocks changed %s facts without silently substituting current versions", async (kind) => {
    const h = install(); mount(); await ready();
    if (kind === "version") h.world.task.version = 12;
    if (kind === "correlation") at(h.world.graph.overview.attention_items).correlation_id = OTHER;
    if (kind === "resource") at(h.world.graph.overview.attention_items).resource_version = 4;
    if (kind === "removed") h.world.graph.overview.attention_items = [];
    const identity = vi.spyOn(crypto, "randomUUID"), save = vi.spyOn(Storage.prototype, "setItem"); await click();
    expect(await screen.findByText("待处理事项已变化，请刷新后重新确认。")).toBeVisible();
    expect(identity).not.toHaveBeenCalled(); expect(save).not.toHaveBeenCalled(); expect(h.posted).toHaveLength(0);
  });

  it.each(["command", "root", "version"])("does not hide the item on an uncorrelated %s response", async (kind) => {
    const h = install(); h.world.response = command => ({ ...reply(command), ...(kind === "command" ? { command_id: OTHER } : kind === "root" ? { aggregate_id: OTHER } : { aggregate_version: 12 }) });
    mount(); await ready(); await click(); await unknown();
    expect(screen.getByText(ITEM.reason)).toBeVisible(); expect(screen.queryByText("已确认处理")).not.toBeInTheDocument(); expect(h.posted).toHaveLength(1);
  });

  it.each(["task", "graph"])("fences held %s preflight after A unmount before identity/save/POST", async (stage) => {
    const h = install(), page = mount(); await ready(); const hold = deferred();
    if (stage === "task") h.world.holdTask = hold.promise; else h.world.holdGraph = hold.promise;
    const identity = vi.spyOn(crypto, "randomUUID"), save = vi.spyOn(Storage.prototype, "setItem");
    await click(); await waitFor(() => expect(stage === "task" ? h.taskReads[ROOT] : h.graphReads[ROOT]).toBe(2));
    page.unmount(); mount("", OTHER);
    hold.release(); await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)); });
    expect(identity).not.toHaveBeenCalled(); expect(save).not.toHaveBeenCalled(); expect(h.posted).toHaveLength(0);
    expect(localStorage.getItem(nativeAttentionDraftKey(scope))).toBeNull();
  });

  it("isolates a held A POST from B storage, state, graph cache and refresh", async () => {
    const h = install(), hold = deferred(); h.world.holdPost = hold.promise;
    const page = mount(); await ready(); await click(); await waitFor(() => expect(h.posted).toHaveLength(1));
    const savedA = localStorage.getItem(nativeAttentionDraftKey(scope));
    await userEvent.setup().click(screen.getByRole("button", { name: "Next task" })); await ready("Task B");
    const graphB = page.client.getQueryData(taskGraphQueryKey(WS, OTHER)), readsB = h.graphReads[OTHER], readsA = h.graphReads[ROOT];
    hold.release(); await act(async () => { await new Promise(resolve => setTimeout(resolve, 30)); });
    expect(page.client.getQueryData(taskGraphQueryKey(WS, OTHER))).toBe(graphB);
    expect(h.graphReads[OTHER]).toBe(readsB); expect(h.graphReads[ROOT]).toBe(readsA);
    expect(localStorage.getItem(nativeAttentionDraftKey(scope))).toBe(savedA);
    expect(localStorage.getItem(nativeAttentionDraftKey({ ...scope, rootTaskId: OTHER }))).toBeNull();
    expect(screen.queryByRole("button", { name: "查询原确认" })).not.toBeInTheDocument(); expect(h.posted).toHaveLength(1);
  });

  it("exposes idle expired current-human authority without an automatic command or storage write", async () => {
    const h = install(); h.world.session = { ...SESSION, expires_at: new Date(Date.now() + 500).toISOString() }; mount(); await ready();
    const identity = vi.spyOn(crypto, "randomUUID"), save = vi.spyOn(Storage.prototype, "setItem");
    expect(await screen.findByText("当前操作身份未确认", {}, { timeout: 1500 })).toBeVisible();
    expect(screen.getByRole("button", { name: "本地会话" })).toBeVisible(); expect(screen.getByRole("button", { name: "确认已处理" })).toBeDisabled();
    expect(identity).not.toHaveBeenCalled(); expect(save).not.toHaveBeenCalled(); expect(h.writes).toEqual([]);
  });

  it("preserves exact WorkerItem and Proposal decision links without acknowledging navigation", async () => {
    const h = install(); const proposal = { ...ITEM, key: "proposal:release", kind: "collaboration_release_proposal", subtask_title: "Release proposal", deep_link: `/workspaces/${WS}/worker-items/${WORKER}?view=governance` };
    h.world.graph.overview.attention_items.push(proposal); mount(); await ready();
    expect(screen.getByRole("link", { name: /Attention Task \/ Review worker/ })).toHaveAttribute("href", `/workspaces/${WS}/worker-items/${WORKER}`);
    expect(screen.getByRole("link", { name: "查看建议：Release proposal" })).toHaveAttribute("href", proposal.deep_link);
    expect(screen.getAllByRole("button", { name: "确认已处理" })).toHaveLength(1);
    await userEvent.setup().click(screen.getByRole("link", { name: "查看建议：Release proposal" })); expect(h.writes).toEqual([]);
  });
});
