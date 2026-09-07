import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
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
const KNOWLEDGE_PAGE_ID = "90000000-0000-0000-0000-000000000001";
const KNOWLEDGE_REVISION_ID = "91000000-0000-0000-0000-000000000001";
const WORK_CONTEXT_REVISION_ID = "92000000-0000-0000-0000-000000000001";

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
  collaboration: {
    enabled: true,
    effective_strategy: "all_required",
    required_input_count: 2,
    satisfied_input_count: 1,
    waiting_upstream_subtasks: [{
      subtask_key: "payment-implementation",
      title: "支付实现",
      href: `/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}/subtasks/payment-implementation`,
    }],
    missing_artifact_refs: ["artifact:payment-contract"],
    missing_evidence_refs: ["evidence:payment-contract"],
    waiting_worker_item_count: 1,
    released_worker_item_count: 1,
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
      collaboration_state: "materialized",
      collaboration_waiting_summary: null,
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
      collaboration_state: "waiting_release",
      collaboration_waiting_summary: "等待协同放行",
      attention_state: "decision_required",
      governance_mode: "paused",
      proposal_summary: "等待确认本次协同放行建议",
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
  attention_items: [
    {
      key: "scheduling_block:30000000-0000-0000-0000-000000000001:callback-idempotency:80000000-0000-0000-0000-000000000001",
      kind: "blocked",
      worker_item_id: IMPLEMENTER_ID,
      worker_item_title: "实现者",
      reason: "2 个候选执行节点暂不可用",
      occurred_at: "2026-08-23T07:55:00Z",
      waiting_reason: "no_eligible_worker",
      waiting_details_summary: "2 个候选执行节点暂不可用",
      requires_operator: true,
      blocked_since: "2026-08-23T07:55:00Z",
    },
  ],
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

const workContext = {
  id: "93000000-0000-0000-0000-000000000001",
  workspace_id: WORKSPACE_ID,
  planning_request_id: "94000000-0000-0000-0000-000000000001",
  root_task_id: ROOT_TASK_ID,
  status: "active",
  current_revision_id: WORK_CONTEXT_REVISION_ID,
  current_revision_number: 4,
  version: 4,
  created_at: "2026-08-23T07:00:00Z",
  updated_at: "2026-08-23T08:01:00Z",
  current_revision: {
    id: WORK_CONTEXT_REVISION_ID,
    work_context_id: "93000000-0000-0000-0000-000000000001",
    workspace_id: WORKSPACE_ID,
    revision_number: 4,
    brief_planning_request_id: "94000000-0000-0000-0000-000000000001",
    brief_revision: 2,
    proposal_revision_id: null,
    plan_snapshot_id: null,
    reason: "同步任务上下文",
    content_hash: "a".repeat(64),
    created_by: "main-agent",
    created_at: "2026-08-23T08:01:00Z",
    snapshot: {
      schema_version: 1,
      objective: "回调幂等",
      constraints: [],
      acceptance_criteria: [],
      requested_outputs: [],
      clarification_decisions: [],
      accepted_decisions: [],
      unresolved_questions: [],
      knowledge_refs: [{
        page_id: KNOWLEDGE_PAGE_ID,
        revision_id: KNOWLEDGE_REVISION_ID,
        revision_hash: "b".repeat(64),
        section_key: "retry-policy",
        section_hash: "c".repeat(64),
        source_ids: [],
        ordinal: 0,
      }],
      artifact_refs: [],
      evidence_refs: [],
      checkpoint_refs: [],
      missing_fact_codes: [],
    },
  },
  timeline: [],
  next_cursor: null,
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
    expect(screen.getAllByText("实现者").length).toBeGreaterThan(0);
    expect(screen.getByText("审查者")).toBeVisible();
    expect(screen.getByText("等待确认重试规则")).toBeVisible();
    expect(screen.getByText("调度等待")).toBeVisible();
    expect(screen.getByText("2 个候选执行节点暂不可用")).toBeVisible();
    expect(screen.getByText("需要我处理")).toBeVisible();
    expect(screen.getByText("协同门槛")).toBeVisible();
    expect(screen.getByText(/阻塞于/)).toBeVisible();
    expect(screen.getByRole("button", { name: "打开 WorkerItem 工作区" })).toBeVisible();
    expect(screen.getByText("实现幂等写入和重试策略")).toBeVisible();
    expect(screen.queryByTestId("worker-graph-canvas")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "打开 Worker：实现者" }));
    expect(screen.getByTestId("route-location")).toHaveTextContent(
      `/workspaces/${WORKSPACE_ID}/worker-items/${IMPLEMENTER_ID}`,
    );
  });

  it("shows fan-in progress and opens the waiting upstream subtask", async () => {
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByText("输入就绪 1 / 2")).toBeVisible();
    expect(screen.getByText("缺少 Artifact · artifact:payment-contract")).toBeVisible();
    expect(screen.getByText("缺少 Evidence · evidence:payment-contract")).toBeVisible();

    await user.click(screen.getByRole("button", { name: "打开上游子任务：支付实现" }));
    expect(screen.getByTestId("route-location")).toHaveTextContent(
      `/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}/subtasks/payment-implementation`,
    );
  });

  it("marks only the affected WorkerItem as waiting for a decision", async () => {
    const user = userEvent.setup();
    renderPage();

    const board = await screen.findByRole("region", { name: "Worker 看板" });
    const attentionColumn = within(board).getByRole("region", { name: "需要处理" });
    expect(within(attentionColumn).getByRole("button", { name: "打开 Worker：审查者" })).toBeVisible();
    expect(within(attentionColumn).getByText("等待你决定")).toBeVisible();
    expect(within(attentionColumn).queryByRole("button", { name: "打开 Worker：实现者" })).not.toBeInTheDocument();
    expect(screen.getByText("待你决定 1 个 WorkerItem")).toBeVisible();

    await user.click(within(attentionColumn).getByRole("button", { name: "打开 Worker：审查者" }));
    expect(screen.getByTestId("route-location")).toHaveTextContent(
      `/workspaces/${WORKSPACE_ID}/worker-items/${REVIEWER_ID}`,
    );
  });

  it("scopes knowledge candidates to the current subtask provenance", async () => {
    server.use(
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}/work-context`, () => HttpResponse.json(workContext)),
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/tasks/${workspace.subtask.task_id}/knowledge-submissions`, () => HttpResponse.json({ items: [], next_cursor: null })),
      http.get(`/knowledge-api/api/v1/workspaces/${WORKSPACE_ID}/knowledge/candidates`, ({ request }) => {
        const url = new URL(request.url);
        expect(url.searchParams.get("source_task_id")).toBe(ROOT_TASK_ID);
        expect(url.searchParams.get("source_subtask_id")).toBe(workspace.subtask.task_id);
        return HttpResponse.json({
          items: [{
            candidate_id: "95000000-0000-0000-0000-000000000001",
            operation: "revise_page",
            target_page_id: KNOWLEDGE_PAGE_ID,
            expected_revision_id: KNOWLEDGE_REVISION_ID,
            expected_revision_hash: "d".repeat(64),
            proposed_title: "回调规则候选",
            proposed_slug: "callback-rule",
            candidate_hash: "e".repeat(64),
            status: "pending",
            producer_type: "worker",
            producer_id: IMPLEMENTER_ID,
            task_id: ROOT_TASK_ID,
            subtask_id: workspace.subtask.task_id,
            worker_item_id: IMPLEMENTER_ID,
            execution_id: null,
            supersedes_candidate_id: null,
            version: 1,
            created_at: "2026-08-23T08:02:00Z",
            updated_at: "2026-08-23T08:02:00Z",
          }],
          next_cursor: null,
        });
      }),
      http.get(`/knowledge-api/api/v1/workspaces/${WORKSPACE_ID}/knowledge/pages/${KNOWLEDGE_PAGE_ID}/revisions/${KNOWLEDGE_REVISION_ID}`, () => HttpResponse.json({
        revision_id: KNOWLEDGE_REVISION_ID,
        page_id: KNOWLEDGE_PAGE_ID,
        workspace_id: WORKSPACE_ID,
        revision_number: 2,
        title: "回调规则",
        summary: "当前子任务的固定知识引用。",
        tags: [],
        content_hash: "f".repeat(64),
        change_reason: "导入",
        created_by: "aitejiu",
        created_at: "2026-08-23T07:00:00Z",
        accepted_candidate_id: null,
        sections: [],
        relations: [],
        source_refs: [],
        sources: [],
      })),
    );
    renderPage();

    const summary = await screen.findByRole("region", { name: "知识上下文" });
    expect(await within(summary).findByText("回调规则")).toBeVisible();
    expect(within(summary).getByText("待审核 1")).toBeVisible();
    expect(within(summary).getByRole("link", { name: "查看全部知识候选" })).toHaveAttribute(
      "href",
      `/workspaces/${WORKSPACE_ID}/knowledge/inbox?source_task_id=${ROOT_TASK_ID}&source_subtask_id=${workspace.subtask.task_id}`,
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
    expect(screen.getAllByText("1 / 2").length).toBeGreaterThan(0);
  });

  it("shows an explicit empty state when no WorkerItem is planned", async () => {
    server.use(
      http.get(
        `/api/v1/tasks/${ROOT_TASK_ID}/subtasks/${SUBTASK_KEY}`,
        () => HttpResponse.json({
          ...workspace,
          worker_items: [],
          worker_graph: { worker_item_ids: [], edges: [] },
          collaboration: null,
        }),
      ),
    );
    renderPage();

    expect(await screen.findByText("这个子任务还没有 WorkerItem")).toBeVisible();
    expect(screen.getByText("等待 WorkerItem 配置")).toBeVisible();
    expect(screen.queryByRole("region", { name: "输入汇聚" })).not.toBeInTheDocument();
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
