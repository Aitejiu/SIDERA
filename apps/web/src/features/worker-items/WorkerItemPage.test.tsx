import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { server } from "@/test/server";

import { WorkerItemPage } from "./WorkerItemPage";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const WORKER_ITEM_ID = "40000000-0000-0000-0000-000000000001";
const ROOT_TASK_ID = "30000000-0000-0000-0000-000000000001";
const EXECUTION_ONE_ID = "60000000-0000-0000-0000-000000000001";
const EXECUTION_TWO_ID = "60000000-0000-0000-0000-000000000002";
const SESSION_TWO_ID = "70000000-0000-0000-0000-000000000002";

const workspace = {
  workspace_id: WORKSPACE_ID,
  worker_item: {
    id: WORKER_ITEM_ID,
    stable_key: "callback-implementer",
    title: "实现回调幂等",
    responsibility: "实现幂等写入和重试策略",
    root_task_id: ROOT_TASK_ID,
    status: "running",
    context_version: 4,
    acknowledged_context_version: 4,
    version: 7,
    updated_at: "2026-08-23T08:05:00Z",
  },
  current_placement: {
    worker_item_key: "callback-implementer",
    subtask_key: "callback-idempotency",
    subtask_task_id: "30000000-0000-0000-0000-000000000002",
    role: "implementer",
    capability_requirements: ["python"],
    plan_generation: 2,
    plan_snapshot_id: "10000000-0000-0000-0000-000000000002",
  },
  placement_history: [
    {
      worker_item_key: "callback-implementer",
      subtask_key: "callback-idempotency",
      subtask_task_id: "30000000-0000-0000-0000-000000000002",
      role: "implementer",
      capability_requirements: ["python"],
      plan_generation: 2,
      plan_snapshot_id: "10000000-0000-0000-0000-000000000002",
    },
    {
      worker_item_key: "callback-implementer",
      subtask_key: "callback-retry",
      subtask_task_id: "30000000-0000-0000-0000-000000000003",
      role: "implementer",
      capability_requirements: ["python"],
      plan_generation: 1,
      plan_snapshot_id: "10000000-0000-0000-0000-000000000001",
    },
  ],
  current_runtime: {
    adapter_type: "opencode",
    attempt: 2,
    execution_id: EXECUTION_TWO_ID,
    execution_status: "running",
    fencing_token: 8,
    input_mode: "queued",
    lease_id: "lease-2",
    session_id: SESSION_TWO_ID,
    session_status: "active",
    session_version: 4,
    task_id: "30000000-0000-0000-0000-000000000002",
    worker_display_name: "OpenCode Worker",
    worker_id: "80000000-0000-0000-0000-000000000001",
  },
  execution_history: [
    {
      execution_id: EXECUTION_TWO_ID,
      task_id: "30000000-0000-0000-0000-000000000002",
      status: "running",
      attempt: 2,
      worker_id: "80000000-0000-0000-0000-000000000001",
      session_id: SESSION_TWO_ID,
      failure_code: null,
      started_at: "2026-08-23T08:00:00Z",
      finished_at: null,
      created_at: "2026-08-23T07:59:00Z",
      updated_at: "2026-08-23T08:05:00Z",
      version: 3,
      adapter_preferences: ["opencode"],
      adapter_forbidden: [],
      scheduling_status: "assigned",
      waiting_reason: null,
      waiting_details_summary: null,
      requires_operator: false,
      blocked_since: null,
      pinned_worker_id: null,
      assigned_worker_id: "80000000-0000-0000-0000-000000000001",
      pinned_worker_summary: null,
      assigned_worker_summary: {
        id: "80000000-0000-0000-0000-000000000001",
        display_name: "OpenCode Worker",
        adapter_type: "opencode",
        status: "busy",
        desired_state: "active",
        current_load: 1,
        capacity: 2,
      },
      binding_summary: null,
    },
    {
      execution_id: EXECUTION_ONE_ID,
      task_id: "30000000-0000-0000-0000-000000000002",
      status: "succeeded",
      attempt: 1,
      worker_id: "80000000-0000-0000-0000-000000000001",
      session_id: "70000000-0000-0000-0000-000000000001",
      failure_code: null,
      started_at: "2026-08-22T08:00:00Z",
      finished_at: "2026-08-22T08:40:00Z",
      created_at: "2026-08-22T07:59:00Z",
      updated_at: "2026-08-22T08:40:00Z",
    },
  ],
  current_step: "运行回调测试",
  next_action: "等待审查者确认",
  latest_checkpoint: {
    execution_id: EXECUTION_TWO_ID,
    sequence: 3,
    title: "回调测试通过",
    artifact_ids: ["90000000-0000-0000-0000-000000000001"],
    occurred_at: "2026-08-23T08:00:00Z",
  },
  usage: {
    input_tokens: 1200,
    output_tokens: 480,
    cached_tokens: 320,
    cost_microunits: 8400,
  },
  artifacts: [
    {
      artifact_id: "90000000-0000-0000-0000-000000000001",
      task_id: "30000000-0000-0000-0000-000000000002",
      execution_id: EXECUTION_TWO_ID,
      artifact_type: "test-report",
      media_type: "text/plain",
      size_bytes: 120,
      content_available: true,
      created_at: "2026-08-23T08:00:00Z",
    },
  ],
  evidence: [
    {
      evidence_id: "91000000-0000-0000-0000-000000000001",
      task_id: "30000000-0000-0000-0000-000000000002",
      execution_id: EXECUTION_TWO_ID,
      acceptance_ref: "acceptance:callback",
      conclusion: "重复通知测试通过",
      artifact_ids: ["90000000-0000-0000-0000-000000000001"],
      created_at: "2026-08-23T08:00:00Z",
    },
  ],
  can_intervene: true,
  read_only_reason: null,
  intervention_context: {
    worker_item_id: WORKER_ITEM_ID,
    plan_generation: 2,
    context_version: 4,
    execution_id: EXECUTION_TWO_ID,
    session_id: SESSION_TWO_ID,
    session_version: 4,
    worker_id: "80000000-0000-0000-0000-000000000001",
  },
  trace_descriptor: {
    root_task_id: ROOT_TASK_ID,
    execution_id: EXECUTION_TWO_ID,
    session_id: SESSION_TWO_ID,
    route: `/api/v1/tasks/${ROOT_TASK_ID}/executions/${EXECUTION_TWO_ID}/sessions/${SESSION_TWO_ID}/trace`,
    raw_ref: null,
  },
  last_change_cursor: 42,
  as_of: "2026-08-23T08:05:00Z",
};

const firstActivityPage = {
  last_change_cursor: 42,
  next_cursor: "cursor-older",
  items: [
    {
      activity_id: "a0000000-0000-0000-0000-000000000001",
      source_type: "session",
      source_id: SESSION_TWO_ID,
      execution_id: EXECUTION_TWO_ID,
      session_id: SESSION_TWO_ID,
      occurred_at: "2026-08-23T08:04:00Z",
      kind: "session.started",
      title: "Session 2 · 当前",
      summary: "Session 2 · 当前",
      correlation_id: "b0000000-0000-0000-0000-000000000001",
      body: null,
      delivery_status: null,
      artifact_ids: [],
      trace_descriptor: workspace.trace_descriptor,
    },
    {
      activity_id: "a0000000-0000-0000-0000-000000000002",
      source_type: "worker_item",
      source_id: WORKER_ITEM_ID,
      execution_id: EXECUTION_TWO_ID,
      session_id: SESSION_TWO_ID,
      occurred_at: "2026-08-23T08:03:00Z",
      kind: "user_input",
      title: "用户介入",
      summary: "只修改这个回调重试规则",
      correlation_id: "b0000000-0000-0000-0000-000000000002",
      body: "只修改这个回调重试规则",
      delivery_status: "delivered",
      artifact_ids: [],
      trace_descriptor: null,
    },
    {
      activity_id: "a0000000-0000-0000-0000-000000000003",
      source_type: "session",
      source_id: "70000000-0000-0000-0000-000000000001",
      execution_id: EXECUTION_ONE_ID,
      session_id: "70000000-0000-0000-0000-000000000001",
      occurred_at: "2026-08-22T08:40:00Z",
      kind: "session.closed",
      title: "Session 1 已关闭",
      summary: "Session 1 已关闭",
      correlation_id: "b0000000-0000-0000-0000-000000000003",
      body: null,
      delivery_status: null,
      artifact_ids: [],
      trace_descriptor: null,
    },
  ],
};

const olderActivityPage = {
  last_change_cursor: 38,
  next_cursor: null,
  items: [
    firstActivityPage.items[1],
    {
      activity_id: "a0000000-0000-0000-0000-000000000004",
      source_type: "session",
      source_id: "70000000-0000-0000-0000-000000000000",
      execution_id: EXECUTION_ONE_ID,
      session_id: "70000000-0000-0000-0000-000000000000",
      occurred_at: "2026-08-22T07:40:00Z",
      kind: "session.started",
      title: "更早的历史活动",
      summary: "更早的历史活动",
      correlation_id: "b0000000-0000-0000-0000-000000000004",
      body: null,
      delivery_status: null,
      artifact_ids: [],
      trace_descriptor: null,
    },
  ],
};

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/worker-items/${WORKER_ITEM_ID}`]}>
        <Routes>
          <Route path="/workspaces/:workspaceId/worker-items/:workerItemId" element={<WorkerItemPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("WorkerItemPage", () => {
  beforeEach(() => {
    server.resetHandlers();
    server.use(
      http.get(
        `/api/v1/tasks/${ROOT_TASK_ID}/graph`,
        () => HttpResponse.json({
          overview: { title: "支付重构" },
          nodes: [{ subtask_key: "callback-idempotency", title: "回调幂等" }],
        }),
      ),
      http.get(
        `/api/v1/workspaces/${WORKSPACE_ID}/worker-items/${WORKER_ITEM_ID}/workspace`,
        () => HttpResponse.json(workspace),
      ),
      http.get(
        `/api/v1/workspaces/${WORKSPACE_ID}/worker-items/${WORKER_ITEM_ID}/activity`,
        ({ request }) => new URL(request.url).searchParams.get("after") === "cursor-older"
          ? HttpResponse.json(olderActivityPage)
          : HttpResponse.json(firstActivityPage),
      ),
      http.get(
        `/api/v1/tasks/${ROOT_TASK_ID}/executions/${EXECUTION_TWO_ID}/sessions/${SESSION_TWO_ID}/trace`,
        () => HttpResponse.json({
          schema_version: 1,
          execution_id: EXECUTION_TWO_ID,
          session_id: SESSION_TWO_ID,
          worker_id: "80000000-0000-0000-0000-000000000001",
          status: "running",
          current_action: "运行回调测试",
          plan_generation: 2,
          checkpoint_sequence: 3,
          freshness_at: "2026-08-23T08:05:00Z",
          last_change_cursor: 42,
          timing: { input_ms: 10, model_ms: 20, tools_ms: 30, total_ms: 60 },
          events: [{
            schema_version: 1,
            workspace_id: WORKSPACE_ID,
            root_task_id: ROOT_TASK_ID,
            work_item_key: "callback-implementer",
            plan_snapshot_id: "10000000-0000-0000-0000-000000000002",
            execution_id: EXECUTION_TWO_ID,
            session_id: SESSION_TWO_ID,
            sequence: 3,
            occurred_at: "2026-08-23T08:05:00Z",
            category: "tool",
            status: "running",
            title: "运行回调测试",
            summary: "运行回调测试",
            duration_ms: 30,
            usage: null,
            artifact_refs: [],
            provider_payload_ref: null,
            provider_extensions: {},
          }],
          previous_before_sequence: null,
          context_branch: null,
        }),
      ),
      http.get("/api/v1/gateway/sessions", () => HttpResponse.json({
        items: [{
          id: SESSION_TWO_ID,
          workspace_id: WORKSPACE_ID,
          execution_id: EXECUTION_TWO_ID,
          worker_id: "80000000-0000-0000-0000-000000000001",
          adapter_session_id: "adapter-session-2",
          status: "active",
          last_sequence: 3,
          last_activity_at: "2026-08-23T08:05:00Z",
          input_mode: "queued",
          version: 4,
          adapter_module_id: null,
          adapter_config_version: null,
          capability_snapshot: null,
          created_at: "2026-08-23T08:00:00Z",
          updated_at: "2026-08-23T08:05:00Z",
          workspace_config_version: null,
          workspace_module_id: null,
          workspace_ref: null,
        }],
        next_cursor: null,
      })),
      http.get(`/api/v1/executions/${EXECUTION_TWO_ID}`, () => HttpResponse.json({ status: "running" })),
    );
  });

  it("renders a stable WorkerItem workspace across sessions", async () => {
    renderPage();

    expect(await screen.findByRole("heading", { name: "实现回调幂等" })).toBeVisible();
    expect((await screen.findAllByText("支付重构 / 回调幂等")).length).toBeGreaterThan(0);
    expect(screen.getByText("Session 1 已关闭")).toBeVisible();
    expect(screen.getByText("Session 2 · 当前")).toBeVisible();
    expect(screen.getByText("只修改这个回调重试规则")).toBeVisible();
    expect(screen.getAllByText("OpenCode Worker").length).toBeGreaterThan(0);
    expect(screen.getByText("执行尝试 #2")).toBeVisible();
    expect(screen.getByText("当前执行已绑定；更换节点需要停止并创建新的执行尝试")).toBeVisible();
    expect(screen.getByText("运行回调测试")).toBeVisible();
    expect(screen.queryByText(/raw_payload/)).not.toBeInTheDocument();
  });

  it("loads older activity without duplicating an existing item", async () => {
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByText("用户介入")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "加载更多活动" }));

    expect(await screen.findByText("更早的历史活动")).toBeVisible();
    expect(screen.getAllByText("只修改这个回调重试规则")).toHaveLength(1);
  });

  it("shows placement, usage, delivery facts, and a clear read-only state", async () => {
    renderPage();

    const sidebar = await screen.findByRole("complementary", { name: "WorkerItem 事实" });
    expect(within(sidebar).getByText("第 2 代 · callback-idempotency")).toBeVisible();
    expect(within(sidebar).getByText("context V4 · 已同步")).toBeVisible();
    expect(within(sidebar).getByText("1,200")).toBeVisible();
    expect(within(sidebar).getByText("test-report")).toBeVisible();
    expect(within(sidebar).getByText("重复通知测试通过")).toBeVisible();
  });

  it("opens Agent Trace from an activity and returns focus to the WorkerItem page", async () => {
    const user = userEvent.setup();
    renderPage();

    const traceButton = await screen.findByRole("button", { name: "查看 Agent Trace" });
    await user.click(traceButton);

    const dialog = await screen.findByRole("dialog", { name: "Agent Trace" });
    expect(within(dialog).getAllByText("运行回调测试").length).toBeGreaterThan(0);
    await user.click(within(dialog).getByRole("button", { name: "关闭 Agent Trace" }));

    expect(screen.queryByRole("dialog", { name: "Agent Trace" })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "实现回调幂等" })).toBeVisible();
    expect(traceButton).toHaveFocus();
  });

  it("keeps a retired WorkerItem visibly read-only", async () => {
    server.use(
      http.get(
        `/api/v1/workspaces/${WORKSPACE_ID}/worker-items/${WORKER_ITEM_ID}/workspace`,
        () => HttpResponse.json({
          ...workspace,
          worker_item: { ...workspace.worker_item, status: "retired" },
          current_runtime: null,
          can_intervene: false,
          read_only_reason: "worker_item_retired",
          intervention_context: null,
        }),
      ),
    );
    renderPage();

    const readOnly = await screen.findByRole("status");
    expect(within(readOnly).getByText("只读")).toBeVisible();
    expect(screen.getAllByText("这个 WorkerItem 已退役，只能查看历史上下文").length).toBeGreaterThan(0);
  });
});
