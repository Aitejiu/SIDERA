import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { server } from "@/test/server";

import { SubtaskPage } from "./SubtaskPage";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const ROOT_TASK_ID = "30000000-0000-0000-0000-000000000001";
const SUBTASK_KEY = "callback-idempotency";
const IMPLEMENTER_ID = "40000000-0000-0000-0000-000000000001";
const REVIEWER_ID = "40000000-0000-0000-0000-000000000002";

const workspace = {
  workspace_id: WORKSPACE_ID,
  root_task_id: ROOT_TASK_ID,
  task_title: "支付重构",
  plan_snapshot_id: "10000000-0000-0000-0000-000000000001",
  plan_generation: 2,
  subtask: {
    key: SUBTASK_KEY,
    kind: "child",
    task_id: "30000000-0000-0000-0000-000000000002",
    title: "回调幂等",
    goal_summary: "确保重复通知不会重复扣款。",
    milestone_key: "m1",
    state: "waiting_user",
    acceptance_refs: ["acceptance:callback", "acceptance:retry"],
    evidence_satisfied: 1,
    evidence_required: 2,
    current_action: "等待确认重试规则",
  },
  upstream_subtasks: [
    { subtask_key: "design", title: "设计回调", state: "succeeded" },
  ],
  downstream_subtasks: [
    { subtask_key: "integration", title: "支付集成", state: "waiting_dependency" },
  ],
  worker_items: [
    {
      worker_item_id: IMPLEMENTER_ID,
      stable_key: "implementer",
      title: "实现者",
      role: "implementer",
      responsibility: "实现幂等写入和重试策略",
      status: "running",
      current_step: "运行回调测试",
      blocker: null,
      current_agent_summary: "OpenCode callback worker",
      context_version: 3,
      recent_activity_at: "2026-08-23T08:00:00Z",
    },
    {
      worker_item_id: REVIEWER_ID,
      stable_key: "reviewer",
      title: "审查者",
      role: "reviewer",
      responsibility: "审查重试规则和验收证据",
      status: "waiting_dependency",
      current_step: "等待实现者完成",
      blocker: "等待实现者完成",
      current_agent_summary: null,
      context_version: 1,
      recent_activity_at: "2026-08-23T07:55:00Z",
    },
  ],
  worker_graph: {
    worker_item_ids: [IMPLEMENTER_ID, REVIEWER_ID],
    edges: [
      {
        upstream_worker_item_id: IMPLEMENTER_ID,
        downstream_worker_item_id: REVIEWER_ID,
        kind: "review",
      },
    ],
  },
  artifacts: [
    {
      artifact_id: "90000000-0000-0000-0000-000000000001",
      artifact_type: "test-report",
      media_type: "text/plain",
      size_bytes: 120,
      content_available: true,
    },
  ],
  evidence: [
    {
      evidence_id: "91000000-0000-0000-0000-000000000001",
      acceptance_ref: "acceptance:callback",
      conclusion: "重复通知测试通过。",
      artifact_ids: ["90000000-0000-0000-0000-000000000001"],
    },
  ],
  checkpoints: [
    {
      sequence: 3,
      title: "回调测试通过",
      artifact_ids: ["90000000-0000-0000-0000-000000000001"],
      execution_id: "60000000-0000-0000-0000-000000000001",
      occurred_at: "2026-08-23T08:00:00Z",
    },
  ],
  attention_items: [],
  recent_activity: [
    {
      source_type: "worker_item",
      source_id: IMPLEMENTER_ID,
      occurred_at: "2026-08-23T08:00:00Z",
      kind: "assistant.summary",
      summary: "运行回调测试",
      correlation_id: "50000000-0000-0000-0000-000000000001",
      worker_item_id: IMPLEMENTER_ID,
    },
  ],
  last_change_cursor: 19,
  as_of: "2026-08-23T08:01:00Z",
};

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="route-location">{location.pathname}{location.search}</output>;
}

function renderPage(initialEntry = `/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}/subtasks/${SUBTASK_KEY}`) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route path="/workspaces/:workspaceId/tasks/:rootTaskId/subtasks/:subtaskKey" element={<SubtaskPage />} />
        </Routes>
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("SubtaskPage", () => {
  beforeEach(() => {
    server.resetHandlers();
    server.use(
      http.get(
        `/api/v1/tasks/${ROOT_TASK_ID}/subtasks/${SUBTASK_KEY}`,
        () => HttpResponse.json(workspace),
      ),
    );
  });

  it("shows local goal and WorkerItems before the graph", async () => {
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByRole("heading", { name: "回调幂等" })).toBeVisible();
    expect(screen.getByText("实现者")).toBeVisible();
    expect(screen.getByText("审查者")).toBeVisible();
    expect(screen.getByText("等待确认重试规则")).toBeVisible();
    expect(screen.getByText("实现幂等写入和重试策略")).toBeVisible();
    expect(screen.queryByTestId("worker-graph-canvas")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "打开 Worker：实现者" }));
    expect(screen.getByTestId("route-location")).toHaveTextContent(
      `/workspaces/${WORKSPACE_ID}/worker-items/${IMPLEMENTER_ID}`,
    );
  });

  it("switches to a Worker Graph with UUID-backed review edges", async () => {
    const user = userEvent.setup();
    renderPage();

    await screen.findByRole("heading", { name: "回调幂等" });
    await user.click(screen.getByRole("tab", { name: "Graph" }));

    expect(await screen.findByTestId("worker-graph-canvas")).toBeVisible();
    expect(screen.getByText("实现者 → 审查者 · review")).toBeVisible();
    expect(screen.getByRole("button", { name: "打开 Worker：审查者" })).toBeVisible();
    expect(screen.queryByText("支付集成 → 审查者")).not.toBeInTheDocument();
  });

  it("keeps Subtask relations and delivery evidence in the summary", async () => {
    renderPage();

    expect(await screen.findByText("设计回调")).toBeVisible();
    expect(screen.getByText("支付集成")).toBeVisible();
    expect(screen.getByText("test-report")).toBeVisible();
    expect(screen.getByText("重复通知测试通过。")).toBeVisible();
    expect(screen.getByText("回调测试通过")).toBeVisible();
    expect(screen.getByText("1 / 2")).toBeVisible();
  });

  it("shows an explicit empty state when no WorkerItem is planned", async () => {
    server.use(
      http.get(
        `/api/v1/tasks/${ROOT_TASK_ID}/subtasks/${SUBTASK_KEY}`,
        () => HttpResponse.json({
          ...workspace,
          worker_items: [],
          worker_graph: { worker_item_ids: [], edges: [] },
        }),
      ),
    );
    renderPage();

    expect(await screen.findByText("这个子任务还没有 WorkerItem")).toBeVisible();
    expect(screen.getByText("等待 WorkerItem 配置")).toBeVisible();
  });

  it("offers a retry when the Subtask workspace cannot be read", async () => {
    const user = userEvent.setup();
    let requestCount = 0;
    server.use(
      http.get(`/api/v1/tasks/${ROOT_TASK_ID}/subtasks/${SUBTASK_KEY}`, () => {
        requestCount += 1;
        return requestCount === 1
          ? new HttpResponse(null, { status: 503 })
          : HttpResponse.json(workspace);
      }),
    );
    renderPage();

    expect(await screen.findByText("无法读取子任务工作区")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "重试读取子任务" }));
    expect(await screen.findByRole("heading", { name: "回调幂等" })).toBeVisible();
  });
});
