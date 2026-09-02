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
const CONTEXT_PACKAGE_ONE_ID = "90000000-0000-0000-0000-000000000001";
const CONTEXT_PACKAGE_TWO_ID = "90000000-0000-0000-0000-000000000002";
const WORKER_TRANSITION_ID = "a0000000-0000-0000-0000-000000000001";
const PLAN_TRANSITION_ID = "b0000000-0000-0000-0000-000000000001";
const PATCH_ID = "c0000000-0000-0000-0000-000000000001";
const HANDOFF_TARGET_ID = "80000000-0000-0000-0000-000000000002";

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
  collaboration: {
    enabled: true,
    state: "materialized",
    upstream_links: [],
    waiting_summary: null,
  },
  plan_sync: {
    worker_transition_id: WORKER_TRANSITION_ID,
    plan_transition_id: PLAN_TRANSITION_ID,
    action: "continue",
    status: "blocked_unknown",
    writer_state: "unknown",
    source_generation: 1,
    target_generation: 2,
    current_context_version: 3,
    acknowledged_context_version: 3,
    target_context_version: 4,
    patch_id: PATCH_ID,
    patch_acknowledged: false,
    source_execution_id: EXECUTION_ONE_ID,
    source_session_id: "70000000-0000-0000-0000-000000000001",
    target_execution_id: EXECUTION_TWO_ID,
    target_session_id: SESSION_TWO_ID,
    continuation_execution_id: EXECUTION_TWO_ID,
    continuation_session_id: SESSION_TWO_ID,
    queued_input_ids: [],
    superseded_input_ids: ["d0000000-0000-0000-0000-000000000001"],
    failure_code: "adapter_result_unknown",
    started_at: "2026-08-23T07:50:00Z",
    completed_at: null,
    trace_href: null,
  },
};

const handoffCandidates = {
  workspace_id: WORKSPACE_ID,
  worker_item_id: WORKER_ITEM_ID,
  source_execution_id: EXECUTION_TWO_ID,
  source_session_id: SESSION_TWO_ID,
  source_worker_id: "80000000-0000-0000-0000-000000000001",
  source_worker_item_version: 7,
  candidates: [{
    worker_id: HANDOFF_TARGET_ID,
    display_name: "Target Agent",
    adapter_type: "opencode",
    eligible: true,
    recommended: true,
    ineligibility_code: null,
    runtime_snapshot: {},
    capability_snapshot_hash: "a".repeat(64),
    policy_reason: { selected: "recommended" },
    workspace_compatibility: { compatible: true },
    runtime_profile_label: "OpenCode · v1",
    capabilities_satisfied: ["python"],
    health_summary: "idle; load 0/2",
    policy_reasons: [{ code: "recommended" }],
    inspected_at: "2026-08-23T08:05:00Z",
  }],
  recommended_worker_id: HANDOFF_TARGET_ID,
  blocking_codes: [],
  inspected_at: "2026-08-23T08:05:00Z",
};

function makeExecutionContextPackage(packageId: string, executionId: string, contextVersion: number) {
  return {
    package_id: packageId,
    workspace_id: WORKSPACE_ID,
    purpose: "execution",
    consumer_type: "worker",
    consumer_id: executionId,
    worker_item_id: WORKER_ITEM_ID,
    work_context_revision_id: "a1000000-0000-0000-0000-000000000001",
    work_context_revision_number: contextVersion,
    work_context_content_hash: "a".repeat(64),
    context_version: contextVersion,
    control_plane_fact_snapshot: {},
    knowledge_refs: [],
    artifact_refs: [],
    evidence_refs: [],
    budget: {
      profile_id: "a2000000-0000-0000-0000-000000000001",
      profile_version: 1,
      max_sections: 4,
      max_chars: 1000,
      max_input_tokens: 300,
      actual_sections: 0,
      actual_chars: 0,
      actual_input_tokens: 0,
    },
    expires_at: "2026-08-27T08:00:00Z",
    idempotency_key: packageId,
    package_hash: `${packageId.slice(0, 1)}${"b".repeat(63)}`,
    status: "active",
    created_at: "2026-08-23T08:00:00Z",
    materialized_at: null,
    revoked_at: null,
    revocation_reason: null,
    revocation_scope: null,
    assembly: {
      request_id: "a3000000-0000-0000-0000-000000000001",
      status: "ready",
      attempt: 1,
      next_attempt_at: null,
      error_code: null,
      request_fingerprint: packageId,
      version: 1,
      created_at: "2026-08-23T08:00:00Z",
      updated_at: "2026-08-23T08:00:00Z",
    },
    acknowledgements: [{
      package_id: packageId,
      consumer_type: "worker",
      consumer_id: executionId,
      context_version: contextVersion,
      package_hash: `${packageId.slice(0, 1)}${"b".repeat(63)}`,
      adapter_module_id: "a4000000-0000-0000-0000-000000000001",
      adapter_config_version: 1,
      acknowledged_at: "2026-08-23T08:05:00Z",
    }],
    materialization_audits: [],
  };
}

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
      activity_id: "a0000000-0000-0000-0000-000000000005",
      source_type: "execution_input",
      source_id: "d0000000-0000-0000-0000-000000000001",
      execution_id: EXECUTION_ONE_ID,
      session_id: "70000000-0000-0000-0000-000000000001",
      occurred_at: "2026-08-23T07:45:00Z",
      kind: "user_input",
      title: "已失效输入",
      summary: "旧 Session 上的重试提示",
      correlation_id: "b0000000-0000-0000-0000-000000000005",
      body: "旧 Session 上的重试提示",
      delivery_status: "superseded",
      artifact_ids: [],
      trace_descriptor: null,
    },
    {
      activity_id: "a0000000-0000-0000-0000-000000000006",
      source_type: "execution_event",
      source_id: "e0000000-0000-0000-0000-000000000001",
      execution_id: EXECUTION_TWO_ID,
      session_id: SESSION_TWO_ID,
      occurred_at: "2026-08-23T08:00:00Z",
      kind: "checkpoint",
      title: "回调测试通过",
      summary: "回调测试通过",
      correlation_id: "b0000000-0000-0000-0000-000000000006",
      body: null,
      delivery_status: null,
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
        `/api/v1/workspaces/${WORKSPACE_ID}/worker-items/${WORKER_ITEM_ID}/handoff-candidates`,
        () => HttpResponse.json(handoffCandidates),
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
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/executions/${EXECUTION_TWO_ID}/context-package`, () => HttpResponse.json(makeExecutionContextPackage(CONTEXT_PACKAGE_ONE_ID, EXECUTION_TWO_ID, 4))),
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/executions/${EXECUTION_ONE_ID}/context-package`, () => HttpResponse.json(makeExecutionContextPackage(CONTEXT_PACKAGE_TWO_ID, EXECUTION_ONE_ID, 3))),
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

  it("opens the handoff chooser in the current WorkerItem tab only when a target is eligible", async () => {
    const user = userEvent.setup();
    renderPage();

    const sidebar = await screen.findByRole("complementary", { name: "WorkerItem 事实" });
    await user.click(await within(sidebar).findByRole("button", { name: "调整执行 Agent" }));
    expect(await screen.findByRole("dialog", { name: "调整执行 Agent" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "实现回调幂等" })).toBeVisible();
    expect(window.location.pathname).toBe(`/workspaces/${WORKSPACE_ID}/worker-items/${WORKER_ITEM_ID}`);
  });

  it("does not offer handoff for a read-only source", async () => {
    server.use(
      http.get(
        `/api/v1/workspaces/${WORKSPACE_ID}/worker-items/${WORKER_ITEM_ID}/workspace`,
        () => HttpResponse.json({
          ...workspace,
          can_intervene: false,
          read_only_reason: "no_active_session",
          current_runtime: null,
          intervention_context: null,
        }),
      ),
    );
    renderPage();

    await screen.findByRole("heading", { name: "实现回调幂等" });
    expect(screen.queryByRole("button", { name: "调整执行 Agent" })).not.toBeInTheDocument();
  });

  it("keeps the WorkerItem workspace primary while showing release wait", async () => {
    server.use(
      http.get(
        `/api/v1/workspaces/${WORKSPACE_ID}/worker-items/${WORKER_ITEM_ID}/workspace`,
        () => HttpResponse.json({
          ...workspace,
          collaboration: {
            enabled: true,
            state: "waiting_release",
            upstream_links: [{
              subtask_key: "payment-implementation",
              title: "支付实现",
              href: `/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}/subtasks/payment-implementation`,
            }],
            waiting_summary: "等待协同放行",
          },
          current_runtime: null,
          execution_history: [],
          can_intervene: false,
          read_only_reason: "no_active_session",
          intervention_context: null,
        }),
      ),
    );
    renderPage();

    expect(await screen.findByRole("heading", { name: "实现回调幂等" })).toBeVisible();
    expect(screen.getByText("等待协同放行")).toBeVisible();
    expect(screen.getByRole("textbox", { name: "介入这个 Worker" })).toBeVisible();
    expect(screen.getByRole("textbox", { name: "介入这个 Worker" })).toBeDisabled();
    expect(screen.getByRole("link", { name: "查看上游：支付实现" })).toHaveAttribute(
      "href",
      `/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}/subtasks/payment-implementation`,
    );
    expect(screen.queryByRole("button", { name: /强制放行/ })).not.toBeInTheDocument();
  });

  it("shows plan sync state and keeps transition facts separate from Agent Trace", async () => {
    renderPage();

    const sync = await screen.findByRole("region", { name: "计划同步" });
    expect(within(sync).getByText("GEN 1 → GEN 2")).toBeVisible();
    expect(within(sync).getByText("未知结果")).toBeVisible();
    expect(within(sync).getByRole("button", { name: "恢复计划同步" })).toBeVisible();
    expect(screen.getByText("PLAN GENERATION")).toBeVisible();
    expect(screen.getByText("CONTEXT PATCH")).toBeVisible();
    expect(screen.getByText("CONTINUATION")).toBeVisible();
    expect(screen.getByText("RECOVERY")).toBeVisible();
    expect(screen.getByText("CHECKPOINT")).toBeVisible();
    expect(screen.queryByRole("button", { name: /查看.*计划同步/ })).not.toBeInTheDocument();
  });

  it("offers bounded recovery details only for blocked unknown transitions", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "恢复计划同步" }));
    const dialog = screen.getByRole("dialog", { name: "恢复计划同步" });
    expect(within(dialog).getByText("影响范围")).toBeVisible();
    expect(within(dialog).getByText("adapter_result_unknown")).toBeVisible();
    expect(within(dialog).getByRole("textbox", { name: "恢复原因" })).toBeVisible();
    expect(within(dialog).getByRole("textbox", { name: "证据引用" })).toBeVisible();
    expect(within(dialog).getByRole("radio", { name: /重新核对 Adapter 状态/ })).toBeVisible();
    expect(within(dialog).getByRole("radio", { name: /将分支安全关闭/ })).toBeVisible();
    expect(within(dialog).queryByText("强制成功")).not.toBeInTheDocument();
  });

  it("does not expose recovery for a converging transition", async () => {
    server.use(
      http.get(
        `/api/v1/workspaces/${WORKSPACE_ID}/worker-items/${WORKER_ITEM_ID}/workspace`,
        () => HttpResponse.json({
          ...workspace,
          plan_sync: { ...workspace.plan_sync, status: "converging", writer_state: "active" },
        }),
      ),
    );
    renderPage();

    await screen.findByRole("heading", { name: "实现回调幂等" });
    expect(screen.queryByRole("button", { name: "恢复计划同步" })).not.toBeInTheDocument();
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

  it("shows package and acknowledgement state for current and historical executions", async () => {
    renderPage();

    const context = await screen.findByRole("region", { name: "WorkerItem 上下文" });
    expect(within(context).getByText("执行尝试 #2 · 当前")).toBeVisible();
    expect(await within(context).findAllByText("Package active · 9bbbbbbb")).toHaveLength(2);
    expect(within(context).getByText("context V4 · 已确认")).toBeVisible();
    expect(within(context).getByText("执行尝试 #1 · 历史")).toBeVisible();
    expect(within(context).getAllByRole("link", { name: /查看本次运行上下文/ })).toHaveLength(2);
    expect(within(context).getByRole("link", { name: "查看本次运行上下文：执行尝试 #2" })).toHaveAttribute(
      "href",
      `/workspaces/${WORKSPACE_ID}/context-packages/${CONTEXT_PACKAGE_ONE_ID}`,
    );
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
          collaboration: null,
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
    expect(screen.queryByRole("region", { name: "协同状态" })).not.toBeInTheDocument();
  });
});
