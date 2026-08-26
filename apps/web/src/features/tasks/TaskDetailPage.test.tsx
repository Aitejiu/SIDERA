import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { server } from "@/test/server";

import { TaskDetailPage } from "./TaskDetailPage";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const ROOT_TASK_ID = "30000000-0000-0000-0000-000000000001";
const DESIGN_TASK_ID = "30000000-0000-0000-0000-000000000002";
const KNOWLEDGE_PAGE_ID = "90000000-0000-0000-0000-000000000001";
const KNOWLEDGE_REVISION_ID = "91000000-0000-0000-0000-000000000001";
const WORK_CONTEXT_REVISION_ID = "92000000-0000-0000-0000-000000000001";

const attentionItem = {
  key: "scheduling_block:30000000-0000-0000-0000-000000000001:callback-idempotency:80000000-0000-0000-0000-000000000001",
  kind: "waiting_user",
  occurred_at: "2026-08-22T08:00:00Z",
  reason: "需要确认重试规则",
  resource_id: DESIGN_TASK_ID,
  resource_version: 3,
  correlation_id: "50000000-0000-0000-0000-000000000001",
  subtask_key: "callback-idempotency",
  subtask_title: "回调幂等",
  task_title: "支付重构",
  waiting_reason: "no_eligible_worker",
  waiting_details_summary: "2 个候选执行节点暂不可用",
  requires_operator: true,
  blocked_since: "2026-08-22T07:55:00Z",
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

const workContext = {
  id: "93000000-0000-0000-0000-000000000001",
  workspace_id: WORKSPACE_ID,
  planning_request_id: "94000000-0000-0000-0000-000000000001",
  root_task_id: ROOT_TASK_ID,
  status: "active",
  current_revision_id: WORK_CONTEXT_REVISION_ID,
  current_revision_number: 4,
  version: 4,
  created_at: "2026-08-22T07:00:00Z",
  updated_at: "2026-08-22T08:01:00Z",
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
    created_at: "2026-08-22T08:01:00Z",
    snapshot: {
      schema_version: 1,
      objective: "支付重构",
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

const exactKnowledgeRevision = {
  revision_id: KNOWLEDGE_REVISION_ID,
  page_id: KNOWLEDGE_PAGE_ID,
  workspace_id: WORKSPACE_ID,
  revision_number: 2,
  title: "支付回调规则",
  summary: "固定的回调重试规则。",
  tags: [],
  content_hash: "d".repeat(64),
  change_reason: "导入",
  created_by: "aitejiu",
  created_at: "2026-08-22T07:00:00Z",
  accepted_candidate_id: null,
  sections: [],
  relations: [],
  source_refs: [],
  sources: [],
};

const knowledgeCandidates = {
  items: [
    {
      candidate_id: "95000000-0000-0000-0000-000000000001",
      operation: "revise_page",
      target_page_id: KNOWLEDGE_PAGE_ID,
      expected_revision_id: KNOWLEDGE_REVISION_ID,
      expected_revision_hash: "d".repeat(64),
      proposed_title: "支付回调规则更新",
      proposed_slug: "payment-callback",
      candidate_hash: "e".repeat(64),
      status: "pending",
      producer_type: "worker",
      producer_id: "worker-1",
      task_id: ROOT_TASK_ID,
      subtask_id: DESIGN_TASK_ID,
      worker_item_id: null,
      execution_id: null,
      supersedes_candidate_id: null,
      version: 1,
      created_at: "2026-08-22T08:02:00Z",
      updated_at: "2026-08-22T08:02:00Z",
    },
    {
      candidate_id: "95000000-0000-0000-0000-000000000002",
      operation: "create_page",
      target_page_id: null,
      expected_revision_id: null,
      expected_revision_hash: null,
      proposed_title: "重试故障记录",
      proposed_slug: "retry-incident",
      candidate_hash: "f".repeat(64),
      status: "changes_requested",
      producer_type: "worker",
      producer_id: "worker-2",
      task_id: ROOT_TASK_ID,
      subtask_id: null,
      worker_item_id: null,
      execution_id: null,
      supersedes_candidate_id: null,
      version: 2,
      created_at: "2026-08-22T08:03:00Z",
      updated_at: "2026-08-22T08:03:00Z",
    },
    {
      candidate_id: "95000000-0000-0000-0000-000000000003",
      operation: "revise_page",
      target_page_id: KNOWLEDGE_PAGE_ID,
      expected_revision_id: KNOWLEDGE_REVISION_ID,
      expected_revision_hash: "d".repeat(64),
      proposed_title: "过期规则",
      proposed_slug: "expired-rule",
      candidate_hash: "1".repeat(64),
      status: "stale",
      producer_type: "worker",
      producer_id: "worker-3",
      task_id: ROOT_TASK_ID,
      subtask_id: null,
      worker_item_id: null,
      execution_id: null,
      supersedes_candidate_id: null,
      version: 1,
      created_at: "2026-08-22T08:04:00Z",
      updated_at: "2026-08-22T08:04:00Z",
    },
  ],
  next_cursor: null,
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
    expect(screen.getAllByText("回调幂等").length).toBeGreaterThan(0);
    expect(screen.getByText("等待确认重试规则")).toBeVisible();
    expect(screen.getByText(/支付重构 \/ 回调幂等/)).toBeVisible();
    expect(screen.getByText("需要确认重试规则")).toBeVisible();
    expect(screen.getByText("2 个候选执行节点暂不可用")).toBeVisible();
    expect(screen.getAllByText("调度等待").length).toBeGreaterThan(0);
    expect(screen.getByText(/阻塞于/)).toBeVisible();
    expect(screen.getByText("需要我处理")).toBeVisible();
    expect(screen.queryByText(/OpenCode Worker A/)).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: /介入/ })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "打开子任务：回调幂等" }));
    expect(screen.getByTestId("route-location")).toHaveTextContent(
      `/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}/subtasks/callback-idempotency`,
    );
  });

  it("shows exact knowledge refs, delivery retry state, and review counts without loading body", async () => {
    server.use(
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}/work-context`, () => HttpResponse.json(workContext)),
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}/knowledge-submissions`, () => HttpResponse.json({
        items: [{
          submission_id: "96000000-0000-0000-0000-000000000001",
          workspace_id: WORKSPACE_ID,
          producer_type: "worker",
          producer_id: "worker-1",
          planning_run_id: null,
          execution_id: null,
          worker_item_id: null,
          task_id: ROOT_TASK_ID,
          subtask_task_id: null,
          proposal_hash: "2".repeat(64),
          status: "retry_wait",
          attempt: 2,
          knowledge_command_id: null,
          terminal_candidate_id: null,
          candidate_deep_link: null,
          error_code: "knowledge_unavailable",
          created_at: "2026-08-22T08:02:00Z",
          updated_at: "2026-08-22T08:03:00Z",
        }],
        next_cursor: null,
      })),
      http.get(`/knowledge-api/api/v1/workspaces/${WORKSPACE_ID}/knowledge/candidates`, () => HttpResponse.json(knowledgeCandidates)),
      http.get(`/knowledge-api/api/v1/workspaces/${WORKSPACE_ID}/knowledge/pages/${KNOWLEDGE_PAGE_ID}/revisions/${KNOWLEDGE_REVISION_ID}`, () => HttpResponse.json(exactKnowledgeRevision)),
    );
    renderPage();

    const summary = await screen.findByRole("region", { name: "知识上下文" });
    expect(await within(summary).findByText("支付回调规则")).toBeVisible();
    expect(within(summary).getByText("1 个固定引用")).toBeVisible();
    expect(within(summary).getByText("待审核 1")).toBeVisible();
    expect(within(summary).getByText("需修改 1")).toBeVisible();
    expect(within(summary).getByText("已过期 1")).toBeVisible();
    expect(within(summary).getByText("投递等待重试 · 第 2 次尝试")).toBeVisible();
    expect(within(summary).getByRole("link", { name: "查看全部知识候选" })).toHaveAttribute(
      "href",
      `/workspaces/${WORKSPACE_ID}/knowledge/inbox?source_task_id=${ROOT_TASK_ID}`,
    );
    expect(within(summary).queryByText(/固定的回调重试规则/)).not.toBeInTheDocument();
  });

  it("offers a Main Agent draft scoped to the root task", async () => {
    renderPage();

    const link = await screen.findByRole("link", { name: "与 Main Agent 讨论" });
    expect(link).toHaveAttribute(
      "href",
      `/workspaces/${WORKSPACE_ID}/chat?context_ref=task:${ROOT_TASK_ID}`,
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
