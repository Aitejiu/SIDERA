import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { server } from "@/test/server";

import { TaskDetailPage } from "./TaskDetailPage";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const ROOT_TASK_ID = "30000000-0000-0000-0000-000000000001";
const DESIGN_TASK_ID = "30000000-0000-0000-0000-000000000002";
const DESIGN_EXECUTION_ID = "60000000-0000-0000-0000-000000000001";
const DESIGN_SESSION_ID = "70000000-0000-0000-0000-000000000001";

const graph = {
  schema_version: 1,
  overview: {
    schema_version: 1,
    workspace_id: WORKSPACE_ID,
    root_task_id: ROOT_TASK_ID,
    plan_snapshot_id: "10000000-0000-0000-0000-000000000001",
    generation: 2,
    title: "Ship Task First control room",
    goal_summary: "Expose progress and exact intervention.",
    acceptance_refs: ["spec:task-first"],
    current_milestone_key: "m1",
    materialized_count: 2,
    planned_count: 1,
    running_count: 1,
    completed_count: 1,
    failed_count: 0,
    next_work_item_keys: ["build"],
    attention_items: [],
    last_change_cursor: 17,
    as_of: "2026-08-21T08:01:00Z",
  },
  nodes: [
    {
      work_item_key: "design",
      task_id: DESIGN_TASK_ID,
      kind: "child",
      title: "设计控制面",
      goal_summary: "Expose canonical projections.",
      milestone_key: "m1",
      worker_slot_key: "backend",
      required_capabilities: ["python"],
      state: "running",
      execution_id: DESIGN_EXECUTION_ID,
      session_id: DESIGN_SESSION_ID,
      worker_id: "80000000-0000-0000-0000-000000000001",
      worker_display_name: "OpenCode Worker A",
      execution_horizon: true,
      critical_path: true,
      waiting_on_plan_dependency: false,
      scheduler_enforced: true,
      current_action: "验证投影快照",
      evidence_satisfied: 1,
      evidence_required: 2,
      updated_at: "2026-08-21T08:01:00Z",
    },
    {
      work_item_key: "build",
      task_id: null,
      kind: "child",
      title: "构建 Web",
      goal_summary: "Render the control room.",
      milestone_key: "m1",
      worker_slot_key: "web",
      required_capabilities: ["react"],
      state: "planned",
      execution_id: null,
      session_id: null,
      worker_id: null,
      worker_display_name: null,
      execution_horizon: true,
      critical_path: true,
      waiting_on_plan_dependency: true,
      scheduler_enforced: false,
      current_action: null,
      evidence_satisfied: 0,
      evidence_required: 1,
      updated_at: null,
    },
  ],
  edges: [
    {
      upstream_work_item_key: "design",
      downstream_work_item_key: "build",
      kind: "hard",
      enforcement: "informational",
      scheduler_enforced: false,
      required_evidence_refs: ["spec:projection"],
    },
  ],
  milestones: [
    {
      key: "m1",
      title: "R4 foundation",
      state: "running",
      materialized_count: 1,
      completed_count: 0,
      planned_count: 1,
      order_hint: 1,
    },
  ],
};

const detail = {
  schema_version: 1,
  last_change_cursor: 17,
  node: graph.nodes[0],
  acceptance_refs: ["spec:projection", "spec:intervention"],
  expected_outputs: ["projection-api"],
  dependencies: graph.edges,
  executions: [
    {
      execution_id: DESIGN_EXECUTION_ID,
      session_id: DESIGN_SESSION_ID,
      worker_id: "80000000-0000-0000-0000-000000000001",
      status: "running",
      attempt: 1,
      started_at: "2026-08-21T08:00:00Z",
      finished_at: null,
    },
  ],
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
      acceptance_ref: "spec:projection",
      conclusion: "Projection contract verified.",
      artifact_ids: ["90000000-0000-0000-0000-000000000001"],
    },
  ],
  checkpoints: [
    {
      sequence: 2,
      title: "Projection tests passed",
      artifact_ids: ["90000000-0000-0000-0000-000000000001"],
      occurred_at: "2026-08-21T08:01:00Z",
    },
  ],
};

const activity = {
  schema_version: 1,
  last_change_cursor: 17,
  items: [
    {
      source_type: "execution",
      source_id: DESIGN_EXECUTION_ID,
      occurred_at: "2026-08-21T08:01:00Z",
      kind: "checkpoint",
      title: "Projection tests passed",
      summary: "The projection contract is verified.",
      correlation_id: "50000000-0000-0000-0000-000000000001",
      causal_parent_id: null,
      scheduler_enforced: false,
    },
  ],
  next_cursor: null,
};

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
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("TaskDetailPage", () => {
  beforeEach(() => {
    server.resetHandlers();
    server.use(
      http.get(`/api/v1/tasks/${ROOT_TASK_ID}/graph`, () => HttpResponse.json(graph)),
      http.get(
        `/api/v1/tasks/${ROOT_TASK_ID}/work-items/design`,
        () => HttpResponse.json(detail),
      ),
      http.get(`/api/v1/tasks/${ROOT_TASK_ID}/activity`, () => HttpResponse.json(activity)),
    );
  });

  it("opens with a task status board and keeps DAG optional", async () => {
    renderPage();

    expect(await screen.findByRole("heading", { name: "任务状态看板" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "执行中" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "下一步" })).toBeVisible();
    expect(screen.getByRole("button", { name: "查看设计控制面" })).toBeVisible();
    expect(screen.queryByRole("region", { name: "任务 DAG" })).not.toBeInTheDocument();
  });

  it("keeps graph, ledger, horizon and inspector on one selected work item", async () => {
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByText("Ship Task First control room")).toBeVisible();
    expect(screen.getByText("执行视野")).toBeVisible();
    expect(screen.getAllByText("构建 Web").length).toBeGreaterThan(0);
    expect(screen.getAllByText("计划中").length).toBeGreaterThan(0);
    expect(screen.getAllByText("设计控制面").length).toBeGreaterThan(0);
    expect(await screen.findByText("目标与验收")).toBeVisible();
    expect(screen.getAllByText("Projection tests passed").length).toBeGreaterThan(0);
    await user.click(screen.getByRole("tab", { name: /^DAG$/ }));
    expect(await screen.findByText("依赖：design → build")).toBeVisible();
    await user.click(screen.getByRole("tab", { name: /^任务表$/ }));
    await user.click(screen.getByRole("button", { name: "在任务表中选择构建 Web" }));

    expect(screen.getByRole("button", { name: "在任务表中选择构建 Web" })).toHaveAttribute("data-selected", "true");
    expect(screen.getAllByText("计划中").length).toBeGreaterThan(0);
    expect(screen.queryByRole("button", { name: /介入/ })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "在任务表中选择设计控制面" }));
    await waitFor(() => expect(screen.getAllByText("OpenCode Worker A").length).toBeGreaterThan(0));
    expect(screen.getByText("WorkerSlot")).toBeVisible();
    expect(screen.getByText("backend")).toBeVisible();
    expect(screen.getByText("验收证据")).toBeVisible();
    expect(screen.getByText("Checkpoint")).toBeVisible();
  });

  it("keeps activity pagination cursor opaque", async () => {
    server.use(
      http.get(`/api/v1/tasks/${ROOT_TASK_ID}/activity`, ({ request }) => {
        const url = new URL(request.url);
        expect(url.searchParams.get("after")).toBeNull();
        return HttpResponse.json(activity);
      }),
    );
    renderPage();
    expect(await screen.findByText("Projection tests passed")).toBeVisible();
    expect(screen.queryByRole("button", { name: "加载更多活动" })).not.toBeInTheDocument();
  });

  it("loads the next activity page with the opaque cursor", async () => {
    const user = userEvent.setup();
    let requestCount = 0;
    server.use(
      http.get(`/api/v1/tasks/${ROOT_TASK_ID}/activity`, ({ request }) => {
        requestCount += 1;
        const url = new URL(request.url);
        if (url.searchParams.get("after") === null) {
          return HttpResponse.json({ ...activity, next_cursor: "opaque-1" });
        }
        expect(url.searchParams.get("after")).toBe("opaque-1");
        return HttpResponse.json({
          ...activity,
          items: [{ ...activity.items[0], title: "后续活动" }],
          next_cursor: null,
        });
      }),
    );

    renderPage();
    expect(await screen.findByRole("button", { name: "加载更多活动" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "加载更多活动" }));
    expect(await screen.findByText("后续活动")).toBeVisible();
    expect(requestCount).toBe(2);
    expect(screen.queryByRole("button", { name: "加载更多活动" })).not.toBeInTheDocument();
  });

  it("exposes recoverable error states for node details and activity", async () => {
    server.use(
      http.get(
        `/api/v1/tasks/${ROOT_TASK_ID}/work-items/design`,
        () => new HttpResponse(null, { status: 503 }),
      ),
      http.get(
        `/api/v1/tasks/${ROOT_TASK_ID}/activity`,
        () => new HttpResponse(null, { status: 503 }),
      ),
    );

    renderPage();

    expect(await screen.findByText("无法读取节点细节")).toBeVisible();
    expect(await screen.findByText("无法读取任务动态。")).toBeVisible();
    expect(screen.getByRole("button", { name: "重试读取节点" })).toBeVisible();
    expect(screen.getByRole("button", { name: "重试读取动态" })).toBeVisible();
  });
});
