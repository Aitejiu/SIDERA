import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { server } from "@/test/server";

import { PlanningPage } from "./PlanningPage";
import { planningProposalDraftKey } from "./ProposalFeedbackComposer";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const REQUEST_ID = "30000000-0000-0000-0000-000000000001";
const RUN_ID = "40000000-0000-0000-0000-000000000001";
const SECOND_RUN_ID = "40000000-0000-0000-0000-000000000002";
const PROFILE_ID = "50000000-0000-0000-0000-000000000001";
const CLARIFICATION_ID = "60000000-0000-0000-0000-000000000001";
const SECOND_CLARIFICATION_ID = "60000000-0000-0000-0000-000000000002";
const ROOT_TASK_ID = "a1000000-0000-0000-0000-000000000001";
const PROPOSAL_HASH = "a".repeat(64);

const capability = {
  structured_output: "json_schema",
  clarification: true,
  idempotency: "none",
  cancellation: "process",
  usage_reporting: "tokens",
  max_context_bytes: 200000,
  stability: "stable",
  interface_version: "1.0",
  proposal_schema_versions: [1],
};

const profile = {
  id: PROFILE_ID,
  workspace_id: WORKSPACE_ID,
  profile_key: "default-opencode",
  version: 2,
  display_name: "默认规划器",
  planner_provider: "opencode",
  planner_module_instance_id: "70000000-0000-0000-0000-000000000001",
  planner_module_config_version: 1,
  planner_interface_version: "1.0",
  status: "active",
  user_selectable: true,
  capabilities: capability,
  created_at: "2026-08-23T08:00:00Z",
};

const usage = {
  input_tokens: 120,
  output_tokens: 80,
  cost_microunits: 25,
  runtime_seconds: 12,
  context_bytes: 4000,
};

const brief = {
  planning_request_id: REQUEST_ID,
  revision: 1,
  objective: "交付任务控制台",
  constraints: ["保留已有上下文"],
  acceptance_criteria: ["用户可以看到真实状态"],
  context_refs: ["docs/spec.md"],
  requested_outputs: ["任务计划"],
  user_feedback: null,
  source_correlation: "80000000-0000-0000-0000-000000000001",
  created_by: "aitejiu",
  created_at: "2026-08-23T08:00:00Z",
};

function makeRun(overrides: Record<string, unknown> = {}) {
  return {
    id: RUN_ID,
    planning_request_id: REQUEST_ID,
    workspace_id: WORKSPACE_ID,
    brief_revision: 1,
    attempt: 1,
    operation_id: "90000000-0000-0000-0000-000000000001",
    status: "running",
    result_kind: null,
    capability_snapshot: capability,
    usage,
    provider_ref: "opencode-run-001",
    error_code: null,
    queued_at: "2026-08-23T08:00:00Z",
    started_at: "2026-08-23T08:00:05Z",
    finished_at: null,
    version: 3,
    ...overrides,
  };
}

function makeRequest(
  status = "planning",
  overrides: Record<string, unknown> = {},
) {
  return {
    id: REQUEST_ID,
    workspace_id: WORKSPACE_ID,
    kind: "create",
    status,
    current_brief_revision: 1,
    current_proposal_revision: status === "proposal_ready" ? 1 : null,
    current_proposal_content_hash: status === "proposal_ready" ? PROPOSAL_HASH : null,
    planning_profile_id: PROFILE_ID,
    planning_profile_version: 2,
    profile_key: "default-opencode",
    current_run_id: RUN_ID,
    requires_attention: status === "waiting_for_user" || status === "blocked",
    version: 4,
    created_at: "2026-08-23T08:00:00Z",
    updated_at: "2026-08-23T08:01:00Z",
    profile,
    active_run: status === "planning" || status === "waiting_for_user" || status === "blocked"
      ? makeRun()
      : null,
    pending_clarifications: [],
    usage,
    last_change_cursor: 12,
    ...overrides,
  };
}

function makeClarification(
  id: string,
  overrides: Record<string, unknown> = {},
) {
  return {
    id,
    planning_request_id: REQUEST_ID,
    planning_run_id: SECOND_RUN_ID,
    question: "需要保留兼容模式吗？",
    rationale: "这会影响执行方案的拆分方式。",
    answer_type: "text",
    options: [],
    blocking: true,
    answer: null,
    answered_by: null,
    answered_at: null,
    answer_brief_revision: null,
    created_at: "2026-08-23T08:00:05Z",
    version: 1,
    ...overrides,
  };
}

function makeProposal() {
  return {
    id: "a0000000-0000-0000-0000-000000000001",
    planning_request_id: REQUEST_ID,
    revision: 1,
    brief_revision: 1,
    planning_run_id: RUN_ID,
    proposal_schema_version: 2,
    kind: "create",
    profile_version: 2,
    created_at: "2026-08-23T08:00:00Z",
    content_hash: PROPOSAL_HASH,
    derived_diff: {
      root_task_added: true,
      added_subtask_keys: ["implementation"],
      added_worker_item_keys: ["implementation-worker"],
      immediately_ready_worker_item_keys: ["implementation-worker"],
      blocked_worker_item_keys: [],
    },
    display: {
      root_task: {
        title: "交付任务控制台",
        goal_summary: "确认后进入任务工作台。",
        acceptance_refs: ["可追踪"],
      },
      subtasks: [{
        key: "implementation",
        kind: "implementation",
        title: "实现控制台",
        goal_summary: "交付页面。",
        acceptance_refs: ["页面可用"],
        expected_outputs: ["页面"],
        milestone_key: null,
        source_refs: ["brief:1"],
      }],
      worker_items: [{
        key: "implementation-worker",
        subtask_key: "implementation",
        title: "实现 Worker",
        role: "frontend",
        responsibility: "实现页面。",
        capability_requirements: ["ui"],
        worker_policy_ref: null,
        configuration_ref: null,
        concurrency_limit: 1,
        source_refs: ["brief:1"],
        pinned_worker_id: null,
      }],
      milestones: [],
      subtask_dependencies: [],
      worker_item_dependencies: [],
      execution_intents: [],
      decision_summary: {
        understanding: "交付一个可追踪的任务控制台。",
        assumptions: [],
        risks: [],
        estimated_tokens: 100,
        estimated_cost_microunits: 1,
        estimated_runtime_seconds: 10,
        estimated_concurrency: 1,
        scope_includes: ["页面"],
        scope_excludes: [],
      },
      impact_policy: {
        activate_immediately: false,
        budget_level: "standard",
        failure_policy: "block_downstream",
        manual_gate: true,
        revision_operation: null,
      },
    },
  };
}

function installHandlers({
  request,
  runs = [request.active_run].filter(Boolean),
  briefs = [brief],
  clarifications = request.pending_clarifications,
}: {
  request: ReturnType<typeof makeRequest>;
  runs?: unknown[];
  briefs?: unknown[];
  clarifications?: unknown[];
}) {
  server.use(
    http.get(`/api/v1/workspaces/${WORKSPACE_ID}/planning-requests/${REQUEST_ID}`, () =>
      HttpResponse.json(request),
    ),
    http.get(`/api/v1/workspaces/${WORKSPACE_ID}/planning-requests/${REQUEST_ID}/runs`, () =>
      HttpResponse.json({ items: runs }),
    ),
    http.get(`/api/v1/workspaces/${WORKSPACE_ID}/planning-requests/${REQUEST_ID}/briefs`, () =>
      HttpResponse.json({ items: briefs }),
    ),
    http.get(`/api/v1/workspaces/${WORKSPACE_ID}/planning-requests/${REQUEST_ID}/clarifications`, () =>
      HttpResponse.json({ items: clarifications }),
    ),
    http.get(`/api/v1/workspaces/${WORKSPACE_ID}/planning-requests/${REQUEST_ID}/proposals/1`, () =>
      HttpResponse.json(makeProposal()),
    ),
  );
}

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/planning/${REQUEST_ID}`]}>
        <Routes>
          <Route path="/workspaces/:workspaceId/planning/:planningRequestId" element={<PlanningPage />} />
          <Route path="/workspaces/:workspaceId/tasks/:rootTaskId" element={<div data-testid="root-task-route">Root Task</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("PlanningPage", () => {
  beforeEach(() => {
    server.resetHandlers();
    window.localStorage.clear();
  });

  it.each([
    ["submitted", "已提交"],
    ["planning", "规划中"],
    ["waiting_for_user", "等待你的回答"],
    ["proposal_ready", "方案待确认"],
    ["failed", "规划失败"],
    ["cancelled", "已取消"],
    ["rejected", "已拒绝"],
    ["materialized", "已转为任务"],
  ])("renders a recoverable state label for %s", async (status, label) => {
    const request = makeRequest(status);
    installHandlers({ request });
    renderPage();

    expect(await screen.findByText(label)).toBeVisible();
    expect(screen.queryByText(/\d+%/)).not.toBeInTheDocument();
  });

  it("opens a Main Agent draft with the planning request reference", async () => {
    installHandlers({ request: makeRequest("planning") });
    renderPage();

    const link = await screen.findByRole("link", { name: "与 Main Agent 讨论" });
    expect(link).toHaveAttribute(
      "href",
      `/workspaces/${WORKSPACE_ID}/chat?context_ref=planning_request:${REQUEST_ID}`,
    );
  });

  it("shows real running metadata without inventing percentage progress", async () => {
    const request = makeRequest("planning", {
      active_run: makeRun({
        attempt: 2,
        started_at: "2026-08-23T08:00:05Z",
        provider_ref: "opencode-run-002",
      }),
    });
    installHandlers({ request });
    renderPage();

    expect(await screen.findByText("默认规划器")).toBeVisible();
    expect(within(screen.getByRole("complementary", { name: "规划请求信息" })).getByText(/opencode/)).toBeVisible();
    expect(screen.getByText("第 2 次尝试")).toBeVisible();
    expect(screen.getByText(/已运行/)).toBeVisible();
    expect(screen.queryByText(/\d+%/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "取消规划" })).toBeVisible();
  });

  it("shows blocked runs safely without exposing a retry command", async () => {
    const request = makeRequest("blocked", {
      active_run: makeRun({
        status: "blocked",
        provider_ref: "provider-ref-001",
        error_code: "provider_unavailable",
      }),
    });
    installHandlers({ request });
    renderPage();

    await screen.findByText("provider-ref-001");
    expect(screen.getAllByText("需要运维恢复").length).toBeGreaterThan(0);
    expect(screen.getByText("provider-ref-001")).toBeVisible();
    expect(screen.queryByRole("button", { name: /重试/ })).not.toBeInTheDocument();
  });

  it("submits all blocking clarification answers together", async () => {
    const first = makeClarification(CLARIFICATION_ID);
    const second = makeClarification(SECOND_CLARIFICATION_ID, {
      question: "选择兼容策略",
      answer_type: "single_choice",
      options: ["保守", "激进"],
    });
    const request = makeRequest("waiting_for_user", {
      active_run: makeRun({ status: "needs_clarification" }),
      pending_clarifications: [first, second],
    });
    installHandlers({ request, clarifications: [first, second] });
    let commandBody: Record<string, unknown> | null = null;
    server.use(
      http.post("/api/v1/commands", async ({ request: command }) => {
        commandBody = await command.json() as Record<string, unknown>;
        return HttpResponse.json({
          aggregate_id: REQUEST_ID,
          aggregate_version: 5,
          command_id: "a0000000-0000-0000-0000-000000000001",
          event_ids: [],
          data: {},
        });
      }),
    );
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByText("需要保留兼容模式吗？")).toBeVisible();
    const submit = screen.getByRole("button", { name: "提交澄清答复" });
    expect(submit).toBeDisabled();
    await user.type(screen.getAllByLabelText("澄清答复")[0]!, "保留");
    await user.selectOptions(screen.getByLabelText("选择兼容策略"), "保守");
    expect(submit).toBeEnabled();
    await user.click(submit);

    await waitFor(() => expect(commandBody).not.toBeNull());
    const submittedCommand = commandBody as unknown as Record<string, unknown>;
    expect(submittedCommand.command_type).toBe("AnswerPlanningClarification");
    expect(submittedCommand.target_type).toBe("planning_request");
    expect(submittedCommand.target_id).toBe(REQUEST_ID);
    expect(submittedCommand.expected_version).toBe(4);
    expect((submittedCommand.payload as { answers: unknown[] }).answers).toHaveLength(2);
  });

  it("preserves proposal feedback drafts after a version conflict", async () => {
    const request = makeRequest("proposal_ready");
    installHandlers({ request });
    server.use(
      http.post("/api/v1/commands", () =>
        HttpResponse.json({
          error: { code: "version_conflict", message: "方案已更新，请重新确认", details: {} },
        }, { status: 409 }),
      ),
    );
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "要求修改" }));
    const feedback = screen.getByLabelText("修改意见");
    await user.type(feedback, "请保留原有 Worker 的上下文");
    await user.click(screen.getByRole("button", { name: "提交修改意见" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("方案已更新，请重新确认");
    expect(window.localStorage.getItem(
      planningProposalDraftKey(WORKSPACE_ID, REQUEST_ID, 1),
    )).toContain("请保留原有 Worker 的上下文");
    expect(feedback).toHaveValue("请保留原有 Worker 的上下文");
    expect(screen.getByRole("button", { name: "提交修改意见" })).toBeDisabled();
  });

  it("requires explicit confirmation before rejecting a proposal", async () => {
    const request = makeRequest("proposal_ready");
    installHandlers({ request });
    let commandCount = 0;
    let commandBody: Record<string, unknown> | null = null;
    server.use(
      http.post("/api/v1/commands", async ({ request: command }) => {
        commandCount += 1;
        commandBody = await command.json() as Record<string, unknown>;
        return HttpResponse.json({
          aggregate_id: REQUEST_ID,
          aggregate_version: 5,
          command_id: "a0000000-0000-0000-0000-000000000002",
          event_ids: [],
          data: {},
        });
      }),
    );
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "拒绝方案" }));
    const dialog = await screen.findByRole("dialog", { name: "确认拒绝方案" });
    expect(within(dialog).getByText(/不会删除已有任务/)).toBeVisible();
    await user.click(within(dialog).getByRole("button", { name: "返回" }));
    expect(commandCount).toBe(0);
    await user.click(screen.getByRole("button", { name: "拒绝方案" }));
    await user.click(within(await screen.findByRole("dialog", { name: "确认拒绝方案" })).getByRole("button", { name: "确认拒绝" }));

    await waitFor(() => expect(commandCount).toBe(1));
    const submittedCommand = commandBody as unknown as Record<string, unknown>;
    expect(submittedCommand.command_type).toBe("RejectPlanningProposal");
    expect(submittedCommand.expected_version).toBe(4);
    expect((submittedCommand.payload as { current_proposal_revision: number }).current_proposal_revision).toBe(1);
  });

  it("confirms the reviewed proposal and opens the returned Root Task", async () => {
    const request = makeRequest("proposal_ready");
    installHandlers({ request });
    let commandBody: Record<string, unknown> | null = null;
    server.use(
      http.post("/api/v1/commands", async ({ request: command }) => {
        commandBody = await command.json() as Record<string, unknown>;
        return HttpResponse.json({
          aggregate_id: REQUEST_ID,
          aggregate_version: 5,
          command_id: "a0000000-0000-0000-0000-000000000004",
          event_ids: [],
          data: { root_task_id: ROOT_TASK_ID },
        });
      }),
    );
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "确认并创建任务" }));

    await waitFor(() => expect(commandBody).not.toBeNull());
    const submittedCommand = commandBody as unknown as Record<string, unknown>;
    expect(submittedCommand.command_type).toBe("ConfirmPlanningProposal");
    expect(submittedCommand.target_type).toBe("planning_request");
    expect(submittedCommand.target_id).toBe(REQUEST_ID);
    expect(submittedCommand.expected_version).toBe(4);
    expect(submittedCommand.payload).toEqual({
      proposal_revision: 1,
      proposal_content_hash: PROPOSAL_HASH,
      profile_id: PROFILE_ID,
      profile_version: 2,
    });
    expect(await screen.findByTestId("root-task-route")).toHaveTextContent("Root Task");
  });

  it("requires explicit confirmation before cancelling a running request", async () => {
    const request = makeRequest("planning");
    installHandlers({ request });
    let commandBody: Record<string, unknown> | null = null;
    server.use(
      http.post("/api/v1/commands", async ({ request: command }) => {
        commandBody = await command.json() as Record<string, unknown>;
        return HttpResponse.json({
          aggregate_id: REQUEST_ID,
          aggregate_version: 5,
          command_id: "a0000000-0000-0000-0000-000000000003",
          event_ids: [],
          data: {},
        });
      }),
    );
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "取消规划" }));
    const dialog = await screen.findByRole("dialog", { name: "确认取消规划" });
    await user.click(within(dialog).getByRole("button", { name: "确认取消" }));

    await waitFor(() => expect(commandBody).not.toBeNull());
    const submittedCommand = commandBody as unknown as Record<string, unknown>;
    expect(submittedCommand.command_type).toBe("CancelPlanningRequest");
    expect(submittedCommand.target_type).toBe("planning_request");
    expect(submittedCommand.target_id).toBe(REQUEST_ID);
  });

  it("keeps terminal requests read-only", async () => {
    const request = makeRequest("materialized", {
      current_run_id: null,
      active_run: null,
    });
    installHandlers({ request });
    renderPage();

    expect(await screen.findByText("已转为任务")).toBeVisible();
    expect(screen.getByText("只读历史")).toBeVisible();
    expect(screen.queryByRole("button", { name: "取消规划" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "要求修改" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "拒绝方案" })).not.toBeInTheDocument();
  });
});
