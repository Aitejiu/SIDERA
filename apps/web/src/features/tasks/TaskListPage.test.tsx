import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { server } from "@/test/server";

import { TaskListPage } from "./TaskListPage";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";

const overviews = [
  {
    schema_version: 1,
    workspace_id: WORKSPACE_ID,
    root_task_id: "30000000-0000-0000-0000-000000000001",
    plan_snapshot_id: "10000000-0000-0000-0000-000000000001",
    generation: 3,
    title: "Ship control room",
    goal_summary: "Expose progress and exact intervention.",
    acceptance_refs: ["spec:task-first"],
    current_milestone_key: "m2",
    subtask_count: 4,
    planned_subtask_count: 7,
    running_subtask_count: 2,
    completed_subtask_count: 2,
    failed_subtask_count: 1,
    next_subtask_keys: ["trace-viewer"],
    attention_items: [
      {
        key: "scheduling_block:30000000-0000-0000-0000-000000000001:trace-viewer:80000000-0000-0000-0000-000000000001",
        kind: "blocked",
        task_title: "Ship control room",
        subtask_key: "trace-viewer",
        subtask_title: "Trace viewer needs input",
        reason: "A worker is waiting for a decision.",
        resource_id: "30000000-0000-0000-0000-000000000010",
        resource_version: 4,
        correlation_id: "50000000-0000-0000-0000-000000000001",
        occurred_at: "2026-08-21T08:00:00Z",
        waiting_reason: "runtime_configuration_missing",
        waiting_details_summary: "1 个候选执行节点未配置运行环境",
        requires_operator: false,
        blocked_since: "2026-08-21T07:55:00Z",
      },
    ],
    last_change_cursor: 42,
    as_of: "2026-08-21T08:01:00Z",
    collaboration: {
      enabled: true,
      status: "completed",
      strategy_label: "all_required",
      run_id: "60000000-0000-0000-0000-000000000002",
      plan_generation: 1,
      released_count: 1,
      running_count: 0,
      waiting_count: 0,
      completed_count: 1,
      blocked_subtask_count: 0,
      blocked_subtasks: [],
      latest_decision_summary: "已完成全部协同放行",
      latest_decision_at: "2026-08-21T08:00:00Z",
      attention_required: false,
      detail_href: `/workspaces/${WORKSPACE_ID}/tasks/30000000-0000-0000-0000-000000000002/collaboration`,
    },
  },
  {
    schema_version: 1,
    workspace_id: WORKSPACE_ID,
    root_task_id: "30000000-0000-0000-0000-000000000002",
    plan_snapshot_id: "10000000-0000-0000-0000-000000000002",
    generation: 1,
    title: "Prepare release notes",
    goal_summary: "Summarize the verified release.",
    acceptance_refs: ["spec:release"],
    current_milestone_key: null,
    subtask_count: 1,
    planned_subtask_count: 2,
    running_subtask_count: 0,
    completed_subtask_count: 1,
    failed_subtask_count: 0,
    next_subtask_keys: ["publish"],
    attention_items: [],
    last_change_cursor: 42,
    as_of: "2026-08-21T08:01:00Z",
  },
];

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/tasks`]}>
        <Routes>
          <Route path="/workspaces/:workspaceId/tasks" element={<TaskListPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("TaskListPage", () => {
  beforeEach(() => {
    server.resetHandlers();
    server.use(
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/task-overviews`, () =>
        HttpResponse.json(overviews),
      ),
    );
  });

  it("shows the dispatch state of every root task in the first viewport", async () => {
    renderPage();

    expect(screen.getAllByTestId("task-row-skeleton")).toHaveLength(2);
    expect(await screen.findByText("Ship control room")).toBeVisible();
    expect(screen.getByText("Prepare release notes")).toBeVisible();
    expect(screen.getByText("GEN 3")).toBeVisible();
    expect(screen.getByText("4 / 7 已规划")).toBeVisible();
    expect(screen.getByText("运行中 2")).toBeVisible();
    expect(screen.getByText("失败 1")).toBeVisible();
    expect(screen.getByText("下一个 trace-viewer")).toBeVisible();
    expect(screen.getByText("协同已完成")).toBeVisible();
    expect(screen.getByText("Ship control room / Trace viewer needs input")).toBeVisible();
    expect(screen.getByText("调度等待")).toBeVisible();
    expect(screen.getByText("1 个候选执行节点未配置运行环境")).toBeVisible();
    expect(screen.getAllByText("需要我").length).toBeGreaterThan(0);
    expect(screen.getAllByText(/刚刚更新|分钟前更新|更新于/).length).toBeGreaterThan(0);
  });

  it("shows one empty state for an empty workspace", async () => {
    server.use(
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/task-overviews`, () =>
        HttpResponse.json([]),
      ),
    );
    renderPage();

    expect(await screen.findByText("这个工作区还没有任务")).toBeVisible();
    expect(screen.getAllByText("这个工作区还没有任务")).toHaveLength(1);
  });

  it("keeps the shell and exposes retry when the snapshot fails", async () => {
    server.resetHandlers();
    server.use(
      http.get(
        `/api/v1/workspaces/${WORKSPACE_ID}/task-overviews`,
        () => new HttpResponse(null, { status: 503 }),
      ),
    );
    renderPage();

    expect(await screen.findByRole("button", { name: "重试加载任务" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "任务" })).toBeVisible();
    expect(screen.getByText("无法读取任务快照")).toBeVisible();
  });

  it("preserves a stable skeleton row height while loading", async () => {
    let resolveResponse: (() => void) | undefined;
    const responseGate = new Promise<void>((resolve) => {
      resolveResponse = resolve;
    });
    server.use(
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/task-overviews`, async () => {
        await responseGate;
        return HttpResponse.json(overviews);
      }),
    );
    renderPage();

    const skeleton = screen.getAllByTestId("task-row-skeleton")[0];
    expect(skeleton).toHaveClass("task-row--skeleton");
    expect(skeleton).toHaveAttribute("aria-hidden", "true");
    resolveResponse?.();
    await waitFor(() => expect(screen.getByText("Ship control room")).toBeVisible());
  });
});
