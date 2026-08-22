import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { server } from "@/test/server";

import { TaskDetailPage } from "./TaskDetailPage";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const ROOT_TASK_ID = "30000000-0000-0000-0000-000000000001";
const DESIGN_TASK_ID = "30000000-0000-0000-0000-000000000002";

const attentionItem = {
  key: "attention:subtask:callback-idempotency",
  kind: "waiting_user",
  occurred_at: "2026-08-22T08:00:00Z",
  reason: "需要确认重试规则",
  resource_id: DESIGN_TASK_ID,
  resource_version: 3,
  correlation_id: "50000000-0000-0000-0000-000000000001",
  subtask_key: "callback-idempotency",
  subtask_title: "回调幂等",
  task_title: "支付重构",
};

const graph = {
  schema_version: 1,
  overview: {
    schema_version: 1,
    workspace_id: WORKSPACE_ID,
    root_task_id: ROOT_TASK_ID,
    plan_snapshot_id: "10000000-0000-0000-0000-000000000001",
    generation: 2,
    title: "支付重构",
    goal_summary: "把支付回调改造成可恢复、可观测的执行流程。",
    acceptance_refs: ["spec:payment-retry"],
    current_milestone_key: "m1",
    subtask_count: 2,
    planned_subtask_count: 1,
    running_subtask_count: 0,
    completed_subtask_count: 1,
    failed_subtask_count: 0,
    next_subtask_keys: ["verification"],
    attention_items: [attentionItem],
    last_change_cursor: 17,
    as_of: "2026-08-22T08:01:00Z",
  },
  nodes: [
    {
      subtask_key: "callback-idempotency",
      task_id: DESIGN_TASK_ID,
      kind: "child",
      title: "回调幂等",
      goal_summary: "确保重复通知不会重复扣款。",
      state: "waiting_user",
      milestone_key: "m1",
      worker_item_count: 2,
      running_worker_item_count: 0,
      attention_worker_item_count: 1,
      current_action: "等待确认重试规则",
      evidence_satisfied: 1,
      evidence_required: 2,
    },
    {
      subtask_key: "verification",
      task_id: null,
      kind: "child",
      title: "验收支付链路",
      goal_summary: "验证回调、重试和补偿链路。",
      state: "planned",
      milestone_key: "m1",
      worker_item_count: 0,
      running_worker_item_count: 0,
      attention_worker_item_count: 0,
      current_action: null,
      evidence_satisfied: 0,
      evidence_required: 1,
    },
  ],
  edges: [
    {
      upstream_subtask_key: "callback-idempotency",
      downstream_subtask_key: "verification",
      kind: "hard",
    },
  ],
  milestones: [
    {
      key: "m1",
      title: "支付回调改造",
      state: "running",
      subtask_count: 2,
      completed_count: 0,
      planned_count: 1,
      order_hint: 1,
    },
  ],
};

const rootTask = {
  id: ROOT_TASK_ID,
  workspace_id: WORKSPACE_ID,
  title: "支付重构",
  description: "把支付回调改造成可恢复、可观测的执行流程。",
  acceptance_criteria: ["重复通知不重复扣款"],
  requested_by: "developer",
  priority: 1,
  status: "running",
  version: 4,
  created_at: "2026-08-22T07:00:00Z",
  updated_at: "2026-08-22T08:01:00Z",
};

const activityItems = Array.from({ length: 8 }, (_, index) => ({
  source_type: "task",
  source_id: ROOT_TASK_ID,
  occurred_at: `2026-08-22T08:0${index}:00Z`,
  kind: "checkpoint",
  title: `活动 ${index + 1}`,
  summary: `支付重构进展 ${index + 1}`,
  correlation_id: `51000000-0000-0000-0000-00000000000${index + 1}`,
  subtask_key: index < 4 ? "callback-idempotency" : "verification",
  subtask_title: index < 4 ? "回调幂等" : "验收支付链路",
}));

const activity = {
  schema_version: 1,
  last_change_cursor: 17,
  items: activityItems,
  next_cursor: "opaque-1",
};

function LocationProbe() {
  const location = useLocation();
  return (
    <output data-testid="route-location">
      {location.pathname}{location.search}
    </output>
  );
}

function renderPage(initialEntry = `/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}`) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route path="/workspaces/:workspaceId/tasks/:rootTaskId" element={<TaskDetailPage />} />
        </Routes>
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("TaskDetailPage", () => {
  beforeEach(() => {
    server.resetHandlers();
    server.use(
      http.get(`/api/v1/tasks/${ROOT_TASK_ID}/graph`, () => HttpResponse.json(graph)),
      http.get(`/api/v1/tasks/${ROOT_TASK_ID}`, () => HttpResponse.json(rootTask)),
      http.get(`/api/v1/tasks/${ROOT_TASK_ID}/activity`, ({ request }) => {
        const url = new URL(request.url);
        if (url.searchParams.get("after") === "opaque-1") {
          return HttpResponse.json({
            ...activity,
            items: [{
              ...activityItems[0],
              title: "活动 9",
              summary: "用户确认重试规则后继续推进",
            }],
            next_cursor: null,
          });
        }
        expect(url.searchParams.get("limit")).toBe("8");
        return HttpResponse.json(activity);
      }),
    );
  });

  it("shows overall delivery and opens a Subtask workspace", async () => {
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByRole("heading", { name: "支付重构" })).toBeVisible();
    expect(screen.getByText("把支付回调改造成可恢复、可观测的执行流程。")).toBeVisible();
    expect(screen.getByText("回调幂等")).toBeVisible();
    expect(screen.getByText("等待确认重试规则")).toBeVisible();
    expect(screen.getByText(/支付重构 \/ 回调幂等/)).toBeVisible();
    expect(screen.getByText("需要确认重试规则")).toBeVisible();
    expect(screen.queryByText(/OpenCode Worker A/)).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: /介入/ })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "打开子任务：回调幂等" }));
    expect(screen.getByTestId("route-location")).toHaveTextContent(
      `/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}/subtasks/callback-idempotency`,
    );
  });

  it("defaults to the Subtask board and keeps DAG optional", async () => {
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByRole("tab", { name: "看板" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("heading", { name: "执行中" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "下一步" })).toBeVisible();
    expect(screen.queryByRole("region", { name: "任务 DAG" })).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "任务表" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "DAG" }));
    expect(await screen.findByRole("region", { name: "任务 DAG" })).toBeVisible();
    expect(await screen.findByText("依赖：callback-idempotency → verification")).toBeVisible();
  });

  it("shows at most eight recent activities before loading the next page", async () => {
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByText("活动 8")).toBeVisible();
    expect(screen.queryByText("活动 9")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "加载更多活动" })).toBeVisible();

    await user.click(screen.getByRole("button", { name: "加载更多活动" }));
    expect(await screen.findByText("活动 9")).toBeVisible();
    expect(screen.queryByRole("button", { name: "加载更多活动" })).not.toBeInTheDocument();
  });

  it("offers a retry when the task graph cannot be read", async () => {
    const user = userEvent.setup();
    let requestCount = 0;
    server.use(
      http.get(`/api/v1/tasks/${ROOT_TASK_ID}/graph`, () => {
        requestCount += 1;
        return requestCount === 1
          ? new HttpResponse(null, { status: 503 })
          : HttpResponse.json(graph);
      }),
    );
    renderPage();

    expect(await screen.findByText("无法读取任务图")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "重试读取任务图" }));
    expect(await screen.findByRole("heading", { name: "支付重构" })).toBeVisible();
  });
});
