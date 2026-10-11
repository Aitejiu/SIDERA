import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { server } from "@/test/server";
import { workerResultInputsFixture } from "@/test/resultInputFixtures";
import type { LocalSession } from "@/features/local-session/api";
import { resultAction as taskResultAction, resultView as taskResultView } from "../results/testFixtures";
import type { AgentBindingView, AgentExecutionView, NativeWorkerContextView } from "../assignments/types";
import type { Execution } from "../trace/types";

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
const BASE = `/api/v1/workspaces/${WORKSPACE_ID}`;
const APPOINTMENT_SESSION: LocalSession = {
  session_id: "d0000000-0000-0000-0000-000000000001",
  workspace_id: WORKSPACE_ID,
  principal_id: "principal-current",
  expires_at: "2099-01-01T00:00:00Z",
};

function legacyNativeContext(): NativeWorkerContextView {
  return {
    workspace_id: WORKSPACE_ID,
    worker_item_id: WORKER_ITEM_ID,
    execution_mode: "legacy",
    worker_item_version: workspace.worker_item.version,
    binding: null,
    execution: null,
    execution_version: null,
    context: null,
    input_enabled: false,
    read_only_reason: "execution_mode_legacy",
    run: null,
    as_of: "2026-10-07T00:00:00Z",
  };
}

const workerResultOverview = taskResultView({
  workspace_id: WORKSPACE_ID,
  root_task_id: ROOT_TASK_ID,
  task_title: "支付重构",
  available_actions: [{
    ...taskResultAction("RequestTaskRework"),
    target_id: ROOT_TASK_ID,
    allowed_worker_item_ids: [WORKER_ITEM_ID],
  }],
});

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
    active_hold_id: null,
    active_proposal: null,
    attention_state: "none",
    available_actions: [],
    enabled: true,
    governance_history: [],
    governance_mode: "automatic",
    hold_created_at: null,
    hold_reason: null,
    hold_resource_version: null,
    proposal_summary: null,
    release_state: "unreleased",
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

function traceExecution(executionId = EXECUTION_TWO_ID): Execution {
  return {
    id: executionId, workspace_id: WORKSPACE_ID, task_id: workspace.current_placement.subtask_task_id,
    worker_item_id: WORKER_ITEM_ID, lineage_id: executionId, attempt: 2, max_attempts: 3,
    purpose: "Implement callback", status: "running", capability_requirements: [],
    assigned_worker_id: workspace.current_runtime.worker_id,
    assigned_worker_summary: workspace.execution_history[0]!.assigned_worker_summary!,
    pinned_worker_id: null, pinned_worker_summary: null, binding_summary: null,
    active_lease_id: null, last_fencing_token: 8, not_before: "2026-08-23T08:00:00Z",
    failure_code: null, started_at: "2026-08-23T08:00:00Z", finished_at: null,
    version: 3, stop_requested: false, source_execution_id: null, source_session_id: null,
    created_at: "2026-08-23T08:00:00Z", updated_at: "2026-08-23T08:05:00Z",
    adapter_forbidden: [], adapter_preferences: ["opencode"], blocked_since: null,
    requires_operator: false, scheduling_status: "assigned", waiting_details_summary: null, waiting_reason: null,
  };
}

function savedTraceExecution(executionId = EXECUTION_TWO_ID): AgentExecutionView {
  return { workspace_id: WORKSPACE_ID, execution_id: executionId, root_task_id: ROOT_TASK_ID,
    subtask_id: workspace.current_placement.subtask_task_id, worker_item_id: WORKER_ITEM_ID,
    status: "running", updated_at: "2026-08-23T08:05:00Z", profile_ref: null,
    binding_version: null, run_id: null, link_status: null, event_cursor: null };
}

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
      http.get("/main-agent/v1/health", () => HttpResponse.json({ status: "ok", service: "sidera-main-agent-gateway" })),
      http.get("/product/v1/session", () => HttpResponse.json(APPOINTMENT_SESSION)),
      http.get(`${BASE}/assignments/worker_item/${WORKER_ITEM_ID}`, () => HttpResponse.json(null)),
      http.get(`${BASE}/worker-items/${WORKER_ITEM_ID}/native-context`, () => HttpResponse.json(legacyNativeContext())),
      http.get(
        `/api/v1/tasks/${ROOT_TASK_ID}/graph`,
        () => HttpResponse.json({
          overview: { title: "支付重构" },
          nodes: [{ subtask_key: "callback-idempotency", title: "回调幂等" }],
        }),
      ),
      http.get(
        `/api/v1/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}/results`,
        () => HttpResponse.json(workerResultOverview),
      ),
      http.get(
        `/api/v1/workspaces/${WORKSPACE_ID}/worker-items/${WORKER_ITEM_ID}/workspace`,
        () => HttpResponse.json(workspace),
      ),
      http.get(
        `/api/v1/workspaces/${WORKSPACE_ID}/worker-items/${WORKER_ITEM_ID}/result-inputs`,
        () => HttpResponse.json(workerResultInputsFixture()),
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
      http.get(`/api/v1/executions/${EXECUTION_TWO_ID}`, () => HttpResponse.json(traceExecution())),
      http.get(`${BASE}/agent-executions/${EXECUTION_TWO_ID}`, () => HttpResponse.json(savedTraceExecution())),
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/executions/${EXECUTION_TWO_ID}/context-package`, () => HttpResponse.json(makeExecutionContextPackage(CONTEXT_PACKAGE_ONE_ID, EXECUTION_TWO_ID, 4))),
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/executions/${EXECUTION_ONE_ID}/context-package`, () => HttpResponse.json(makeExecutionContextPackage(CONTEXT_PACKAGE_TWO_ID, EXECUTION_ONE_ID, 3))),
    );
  });

  it("keeps healthy workspace details out of the main reading path until requested", async () => {
    const user = userEvent.setup();
    server.use(http.get(
      `/api/v1/workspaces/${WORKSPACE_ID}/worker-items/${WORKER_ITEM_ID}/workspace`,
      () => HttpResponse.json({ ...workspace, plan_sync: null }),
    ));
    const inputs = workerResultInputsFixture();
    server.use(http.get(
      `/api/v1/workspaces/${WORKSPACE_ID}/worker-items/${WORKER_ITEM_ID}/result-inputs`,
      () => HttpResponse.json({
        ...inputs,
        request: { ...inputs.request, status: "materialized", gate_reason: null },
        governance: { ...inputs.governance, attention_state: "none", waiting_summary: null, state: "materialized" },
        delivery: { ...inputs.delivery, reason_code: null, acknowledgement_status: "acknowledged" },
        execution: { ...inputs.execution, status: "running", gate_reason: null },
        available_actions: [],
      }),
    ));
    renderPage();
    const sidebar = await screen.findByRole("complementary", { name: "WorkerItem 事实" });
    expect(within(sidebar).getByText("Research reports")).not.toBeVisible();
    expect(within(sidebar).queryByText("运行回调测试")).not.toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "当前进展" })).getByText("运行回调测试")).toBeVisible();
    await user.click(within(sidebar).getByText("更多任务信息", { exact: true }));
    expect(within(sidebar).getByText("Research reports")).toBeVisible();
  });

  it("renders a stable WorkerItem workspace across sessions", async () => {
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByRole("heading", { name: "实现回调幂等" })).toBeVisible();
    expect((await screen.findAllByText("支付重构 / 回调幂等")).length).toBeGreaterThan(0);
    await user.click(screen.getByRole("button", { name: "系统记录" }));
    expect(screen.getByText("Session 1 已关闭")).toBeVisible();
    expect(screen.getByText("Session 2 · 当前")).toBeVisible();
    expect(screen.getByText("只修改这个回调重试规则")).toBeVisible();
    expect(screen.getAllByText("OpenCode Worker").length).toBeGreaterThan(0);
    expect(screen.getByText("执行尝试 #2")).not.toBeVisible();
    await user.click(screen.getByText("执行配置", { exact: true }));
    expect(screen.getByText("执行尝试 #2")).toBeVisible();
    expect(screen.getByText("当前执行已绑定；更换节点需要停止并创建新的执行尝试")).toBeVisible();
    expect(screen.getByText("运行回调测试")).toBeVisible();
    expect(await screen.findByRole("region", { name: "结果输入" })).toBeVisible();
    expect(await screen.findByRole("region", { name: "结果恢复与局部返工" })).toBeVisible();
    expect(screen.getByRole("button", { name: "局部返工" })).toBeVisible();
    expect(screen.getByText("Research reports")).toBeVisible();
    expect(screen.queryByText(/raw_payload/)).not.toBeInTheDocument();
  });

  it.each([
    { reason: "a pending decision", attention: "decision_required", mode: "automatic", failure: null },
    { reason: "paused collaboration", attention: "none", mode: "paused", failure: null },
    { reason: "a materialization failure", attention: "none", mode: "automatic", failure: "adapter_unavailable" },
  ])("reveals $reason even without a waiting summary", async ({ attention, mode, failure }) => {
    server.use(http.get(
      `/api/v1/workspaces/${WORKSPACE_ID}/worker-items/${WORKER_ITEM_ID}/workspace`,
      () => HttpResponse.json({ ...workspace, plan_sync: null }),
    ));
    const inputs = workerResultInputsFixture();
    server.use(http.get(
      `/api/v1/workspaces/${WORKSPACE_ID}/worker-items/${WORKER_ITEM_ID}/result-inputs`,
      () => HttpResponse.json({
        ...inputs,
        request: null,
        governance: { ...inputs.governance, attention_state: attention, governance_mode: mode, waiting_summary: null, state: "materialized" },
        delivery: { ...inputs.delivery, reason_code: null, materialization_failure_code: failure },
        available_actions: [],
      }),
    ));
    renderPage();
    const sidebar = await screen.findByRole("complementary", { name: "WorkerItem 事实" });
    await waitFor(() => expect(within(sidebar).getByText("Research reports")).toBeVisible());
  });

  it("keeps result input loading separate from the central workspace", async () => {
    let resolveResultInputs: ((response: Response) => void) | undefined;
    server.use(
      http.get(
        `/api/v1/workspaces/${WORKSPACE_ID}/worker-items/${WORKER_ITEM_ID}/result-inputs`,
        () => new Promise((resolve) => {
          resolveResultInputs = resolve;
        }),
      ),
    );
    renderPage();

    expect(await screen.findByRole("heading", { name: "实现回调幂等" })).toBeVisible();
    expect(screen.getByRole("status", { name: "结果输入" })).toHaveTextContent("读取结果输入事实");
    expect(screen.getByRole("textbox", { name: "介入这个 Worker" })).toBeVisible();

    resolveResultInputs?.(HttpResponse.json(workerResultInputsFixture()));
    expect(await screen.findByRole("region", { name: "结果输入" })).toBeVisible();
  });

  it("allows the result input section to collapse without changing the WorkerItem route", async () => {
    const user = userEvent.setup();
    renderPage();

    await screen.findByRole("region", { name: "结果输入" });
    const details = screen.getByRole("region", { name: "结果输入" }).closest("details");
    if (!(details instanceof HTMLDetailsElement)) throw new Error("expected a collapsible result input section");
    expect(details.open).toBe(true);
    const summary = details.querySelector("summary");
    if (!(summary instanceof HTMLElement)) throw new Error("expected a result input summary");
    await user.click(summary);
    expect(details.open).toBe(false);
    expect(window.location.pathname).toBe(`/workspaces/${WORKSPACE_ID}/worker-items/${WORKER_ITEM_ID}`);
  });

  it("keeps the central WorkerItem workspace usable when result inputs fail", async () => {
    server.use(
      http.get(
        `/api/v1/workspaces/${WORKSPACE_ID}/worker-items/${WORKER_ITEM_ID}/result-inputs`,
        () => HttpResponse.json({ detail: "暂时不可用" }, { status: 503 }),
      ),
    );
    renderPage();

    expect(await screen.findByRole("heading", { name: "实现回调幂等" })).toBeVisible();
    expect(await screen.findByRole("alert", { name: "结果输入" })).toHaveTextContent("结果输入暂不可用");
    expect(screen.getByRole("textbox", { name: "介入这个 Worker" })).toBeVisible();
  });

  it("reveals execution configuration when a running attempt needs operator attention", async () => {
    server.use(http.get(
      `/api/v1/workspaces/${WORKSPACE_ID}/worker-items/${WORKER_ITEM_ID}/workspace`,
      () => HttpResponse.json({
        ...workspace,
        execution_history: [{
          ...workspace.execution_history[0],
          scheduling_status: "waiting",
          waiting_reason: "operator_required",
          waiting_details_summary: "请检查执行节点连接",
          requires_operator: true,
        }],
      }),
    ));
    renderPage();
    const progress = await screen.findByRole("region", { name: "当前进展" });
    expect(within(progress).getByText("请检查执行节点连接")).toBeVisible();
    expect(screen.getByText("需要处理", { exact: true })).toBeVisible();
  });

  it("opens fixed source and artifact routes from the WorkerItem sidebar", async () => {
    const user = userEvent.setup();
    renderPage();

    const panel = await screen.findByRole("region", { name: "结果输入" });
    await user.click(within(panel).getByRole("button", { name: "查看来源 WorkerItem：Research A" }));
    expect(window.location.pathname).toBe(`/workspaces/${WORKSPACE_ID}/worker-items/61000000-0000-0000-0000-000000000011`);
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
    expect(within(screen.getByRole("region", { name: "协同状态" })).getByText("等待协同放行")).toBeVisible();
    expect(screen.getByRole("textbox", { name: "介入这个 Worker" })).toBeVisible();
    expect(screen.getByRole("textbox", { name: "介入这个 Worker" })).toBeDisabled();
    expect(screen.getByRole("link", { name: "查看上游：支付实现" })).toHaveAttribute(
      "href",
      `/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}/subtasks/payment-implementation`,
    );
    expect(screen.queryByRole("button", { name: /强制放行/ })).not.toBeInTheDocument();
  });

  it("shows server-driven release governance controls inside the WorkerItem sidebar", async () => {
    server.use(
      http.get(
        `/api/v1/workspaces/${WORKSPACE_ID}/worker-items/${WORKER_ITEM_ID}/workspace`,
        () => HttpResponse.json({
          ...workspace,
          collaboration: {
            ...workspace.collaboration,
            active_hold_id: "50000000-0000-0000-0000-000000000002",
            active_proposal: {
              created_at: "2026-09-04T08:00:00Z",
              proposal_id: "50000000-0000-0000-0000-000000000001",
              reason_code: "upstream_evidence_ready",
              resolved_at: null,
              resource_version: 8,
              status: "pending",
              summary: "上游证据已齐，请确认是否放行回调实现",
              target_request_version: 3,
            },
            attention_state: "decision_required",
            available_actions: ["approve", "reject", "resume_automatic"],
            governance_mode: "paused",
            hold_created_at: "2026-09-04T07:55:00Z",
            hold_reason: "等待人工审查",
            hold_resource_version: 4,
            proposal_summary: "上游证据已齐，请确认是否放行回调实现",
            release_state: "proposed",
          },
        }),
      ),
    );
    renderPage();

    const sidebar = await screen.findByRole("complementary", { name: "WorkerItem 事实" });
    const governance = within(sidebar).getByRole("region", { name: "协同放行控制" });
    expect(within(governance).getByText("已暂停")).toBeVisible();
    expect(within(governance).getByText("上游证据已齐，请确认是否放行回调实现")).toBeVisible();
    expect(within(governance).getByRole("button", { name: "批准 Proposal" })).toBeVisible();
    await waitFor(() => expect(within(governance).getByRole("button", { name: "恢复自动放行" })).toBeVisible());
  });

  it("shows plan sync state and keeps transition facts separate from Agent Trace", async () => {
    renderPage();

    const sync = await screen.findByRole("region", { name: "计划同步" });
    expect(within(sync).getByText("GEN 1 → GEN 2")).toBeVisible();
    expect(within(sync).getByText("未知结果")).toBeVisible();
    expect(within(sync).getByRole("button", { name: "恢复计划同步" })).toBeVisible();
    expect(screen.getByText("计划调整", { exact: true })).toBeVisible();
    expect(screen.getByText("上下文更新", { exact: true })).toBeVisible();
    expect(screen.getByText("工作续接", { exact: true })).toBeVisible();
    expect(screen.getByText("异常恢复", { exact: true })).toBeVisible();
    expect(screen.getByText("阶段结果", { exact: true })).toBeVisible();
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

  it("keeps genuine gateway recovery on the existing local command path", async () => {
    const posts: Record<string, unknown>[] = [];
    server.use(
      http.get(`${BASE}/worker-transitions/${WORKER_TRANSITION_ID}`, () => HttpResponse.json({ worker_transition_id: WORKER_TRANSITION_ID, version: 17 })),
      http.post("/api/v1/commands", async ({ request }) => {
        const command = await request.json() as Record<string, unknown>; posts.push(command);
        return HttpResponse.json({ command_id: command.command_id, aggregate_id: WORKER_TRANSITION_ID,
          aggregate_version: 17, event_ids: [], data: {} });
      }),
    );
    const user = userEvent.setup(); renderPage();
    await user.click(await screen.findByRole("button", { name: "恢复计划同步" }));
    const dialog = screen.getByRole("dialog", { name: "恢复计划同步" });
    expect(within(dialog).queryByText("Transition V17")).not.toBeInTheDocument();
    await user.type(within(dialog).getByRole("textbox", { name: "恢复原因" }), "legacy review");
    await user.type(within(dialog).getByRole("textbox", { name: "证据引用" }), "event:legacy");
    await user.click(within(dialog).getByRole("button", { name: "提交恢复策略" }));
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toMatchObject({ command_type: "RecoverWorkerTransition", target_type: "worker_transition",
      target_id: WORKER_TRANSITION_ID, expected_version: 17, actor: { type: "local", id: "aitejiu" },
      payload: { strategy: "reconcile_adapter", reason: "legacy review", evidence_refs: ["event:legacy"] } });
    expect(screen.queryByRole("region", { name: "计划同步恢复记录" })).not.toBeInTheDocument();
  });

  it("loads older activity without duplicating an existing item", async () => {
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByRole("article", { name: "只修改这个回调重试规则" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "系统记录" }));
    await user.click(screen.getByRole("button", { name: "加载更多活动" }));

    expect(await screen.findByText("更早的历史活动")).toBeVisible();
    expect(screen.getAllByText("只修改这个回调重试规则")).toHaveLength(1);
  });

  it("shows placement, usage, delivery facts, and a clear read-only state", async () => {
    const user = userEvent.setup();
    renderPage();

    const sidebar = await screen.findByRole("complementary", { name: "WorkerItem 事实" });
    expect(within(sidebar).getByText("第 2 代 · callback-idempotency")).not.toBeVisible();
    await user.click(within(sidebar).getByText("分配历史", { exact: true }));
    expect(within(sidebar).getByText("第 2 代 · callback-idempotency")).toBeVisible();
    expect(within(sidebar).getByText("context V4 · 已同步")).toBeVisible();
    await user.click(within(sidebar).getByText("用量", { exact: true }));
    expect(within(sidebar).getByText("1,200")).toBeVisible();
    await user.click(within(sidebar).getByText("交付物", { exact: true }));
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

    await user.click(await screen.findByRole("button", { name: "系统记录" }));
    const traceButton = await screen.findByRole("button", { name: "查看 Agent Trace" });
    await user.click(traceButton);

    const dialog = await screen.findByRole("dialog", { name: "Agent Trace" });
    expect((await within(dialog).findAllByText("运行回调测试")).length).toBeGreaterThan(0);
    await user.click(within(dialog).getByRole("button", { name: "关闭 Agent Trace" }));

    expect(screen.queryByRole("dialog", { name: "Agent Trace" })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "实现回调幂等" })).toBeVisible();
    expect(traceButton).toHaveFocus();
  });

  it("opens the current Agent Trace without exposing routine system records", async () => {
    const user = userEvent.setup();
    renderPage();
    const traceButton = await screen.findByRole("button", { name: "查看当前 Agent Trace" });
    expect(screen.queryByText("Session 1 已关闭")).not.toBeInTheDocument();
    await user.click(traceButton);
    const dialog = await screen.findByRole("dialog", { name: "Agent Trace" });
    expect((await within(dialog).findAllByText("运行回调测试")).length).toBeGreaterThan(0);
    await user.click(within(dialog).getByRole("button", { name: "关闭 Agent Trace" }));
    expect(traceButton).toHaveFocus();
  });

  it.each([false, true])("opens current saved native Trace without a legacy descriptor or Session, failure=%s", async (failed) => {
    const profileRef = { workspace_id: WORKSPACE_ID, profile_id: "e0000000-0000-0000-0000-000000000001", revision: 1 };
    const runId = "f0000000-0000-0000-0000-000000000001";
    const original: AgentExecutionView = { ...savedTraceExecution(), profile_ref: profileRef,
      binding_version: 2, run_id: runId, link_status: "accepted", event_cursor: 3 };
    const currentBinding: AgentBindingView = { workspace_id: WORKSPACE_ID, target_type: "worker_item", target_id: WORKER_ITEM_ID,
      assignee: { kind: "profile", id: profileRef.profile_id }, profile_ref: { ...profileRef, revision: 2 },
      version: 3, runnable: true, not_runnable_reasons: [], created_at: "2026-10-07T00:00:00Z", changed_at: "2026-10-07T00:00:00Z" };
    const nativeContext: NativeWorkerContextView = { ...legacyNativeContext(), execution_mode: "native_profile",
      binding: currentBinding, execution: original, execution_version: 3, context: null,
      input_enabled: false, read_only_reason: "native_context_unavailable" };
    let legacyReads = 0; let writes = 0; const selected: string[] = [];
    server.use(
      http.get(`${BASE}/assignments/worker_item/${WORKER_ITEM_ID}`, () => HttpResponse.json(currentBinding)),
      http.get(`${BASE}/worker-items/${WORKER_ITEM_ID}/native-context`, () => HttpResponse.json(nativeContext)),
      http.get(`${BASE}/worker-items/${WORKER_ITEM_ID}/workspace`, () => HttpResponse.json({ ...workspace,
        trace_descriptor: null, current_runtime: { ...workspace.current_runtime, adapter_type: "agent-run", session_id: null },
        can_intervene: false, read_only_reason: "no_active_session", intervention_context: null, plan_sync: null })),
      http.get(`/api/v1/executions/${EXECUTION_TWO_ID}`, () => HttpResponse.json({ ...traceExecution(),
        assigned_worker_summary: { ...traceExecution().assigned_worker_summary!, adapter_type: "agent-run" } })),
      http.get(`${BASE}/agent-executions/${EXECUTION_TWO_ID}`, () => HttpResponse.json(original)),
      http.get(`${BASE}/worker-items/${WORKER_ITEM_ID}/executions/:ex/native-trace`, ({ params }) => {
        selected.push(String(params.ex));
        return failed ? HttpResponse.json({ error: { code: "service_unavailable", message: "private-secret", details: {} } }, { status: 503 })
          : HttpResponse.json({ workspace_id: WORKSPACE_ID, worker_item_id: WORKER_ITEM_ID, execution_id: EXECUTION_TWO_ID,
            run_id: runId, profile_ref: profileRef, next_sequence: 3, has_more: false,
            events: [{ run_id: runId, sequence: 3, kind: "native.original", occurred_at: "2026-10-07T00:00:00Z",
              operation_id: null, response_id: null, data: { text: "Raw trace only in dialog" } }] });
      }),
      http.get("/api/v1/tasks/:root/executions/:ex/sessions/:session/trace", () => { legacyReads += 1; return HttpResponse.json({}); }),
      http.post("*", () => { writes += 1; return HttpResponse.json({}); }),
    );
    const user = userEvent.setup(); renderPage();
    expect(await screen.findByRole("heading", { name: "实现回调幂等" })).toBeVisible();
    expect(screen.queryByText("Raw trace only in dialog")).not.toBeInTheDocument();
    const entry = await screen.findByRole("button", { name: "查看当前 Agent Trace" });
    expect(screen.getByRole("region", { name: "当前进展" })).toHaveTextContent("运行回调测试");
    expect(screen.getByText("只修改这个回调重试规则")).toBeVisible();
    await user.click(entry);
    const dialog = await screen.findByRole("dialog", { name: "Agent Trace" });
    if (failed) expect(await within(dialog).findByRole("alert")).toHaveTextContent("Agent Trace 暂不可用");
    else {
      await within(dialog).findByRole("heading", { name: "native.original" });
      expect(within(dialog).getByText("Profile R1")).toBeVisible();
    }
    await user.click(within(dialog).getByRole("button", { name: "关闭 Agent Trace" }));
    expect(entry).toHaveFocus(); expect(selected).toEqual([EXECUTION_TWO_ID]);
    expect(screen.getByRole("textbox", { name: "介入这个 Worker" })).toBeVisible();
    expect(screen.getByText("只修改这个回调重试规则")).toBeVisible();
    expect(screen.queryByText("Raw trace only in dialog")).not.toBeInTheDocument();
    expect(legacyReads).toBe(0); expect(writes).toBe(0);
  });

  it("keeps the historical activity's original Execution and R1 pins while the current native owner is R2", async () => {
    const profileRef = { workspace_id: WORKSPACE_ID, profile_id: "e0000000-0000-0000-0000-000000000001", revision: 1 };
    const runId = "f0000000-0000-0000-0000-000000000001";
    const original: AgentExecutionView = { ...savedTraceExecution(EXECUTION_ONE_ID), profile_ref: profileRef,
      status: "completed", binding_version: 2, run_id: runId, link_status: "accepted", event_cursor: 8 };
    const current: AgentExecutionView = { ...savedTraceExecution(), profile_ref: { ...profileRef, revision: 2 },
      binding_version: 3, run_id: "f0000000-0000-0000-0000-000000000002", link_status: "accepted", event_cursor: 12 };
    const currentBinding: AgentBindingView = { workspace_id: WORKSPACE_ID, target_type: "worker_item", target_id: WORKER_ITEM_ID,
      assignee: { kind: "profile", id: profileRef.profile_id }, profile_ref: current.profile_ref,
      version: 3, runnable: true, not_runnable_reasons: [], created_at: "2026-10-07T00:00:00Z", changed_at: "2026-10-07T00:00:00Z" };
    const nativeContext: NativeWorkerContextView = { ...legacyNativeContext(), execution_mode: "native_profile",
      binding: currentBinding, execution: current, execution_version: 3, context: null,
      input_enabled: false, read_only_reason: "native_context_unavailable" };
    const selected: string[] = []; let legacyReads = 0; let writes = 0;
    server.use(
      http.get(`${BASE}/assignments/worker_item/${WORKER_ITEM_ID}`, () => HttpResponse.json(currentBinding)),
      http.get(`${BASE}/worker-items/${WORKER_ITEM_ID}/native-context`, () => HttpResponse.json(nativeContext)),
      http.get(`${BASE}/worker-items/${WORKER_ITEM_ID}/workspace`, () => HttpResponse.json({ ...workspace,
        trace_descriptor: null, current_runtime: { ...workspace.current_runtime, adapter_type: "agent-run", session_id: null },
        can_intervene: false, read_only_reason: "no_active_session", intervention_context: null, plan_sync: null })),
      http.get(`${BASE}/worker-items/${WORKER_ITEM_ID}/activity`, () => HttpResponse.json({ ...firstActivityPage,
        items: firstActivityPage.items.map((item) => ({ ...item, trace_descriptor: item.execution_id === EXECUTION_ONE_ID && item.kind === "session.closed"
          ? { root_task_id: ROOT_TASK_ID, execution_id: EXECUTION_ONE_ID, session_id: "unknown", route: "ignored-historical-route", raw_ref: null } : null })) })),
      http.get("/api/v1/executions/:ex", ({ params }) => HttpResponse.json({ ...traceExecution(String(params.ex)),
        status: params.ex === EXECUTION_ONE_ID ? "completed" : "running",
        assigned_worker_summary: { ...traceExecution().assigned_worker_summary!, adapter_type: "agent-run" } })),
      http.get(`${BASE}/agent-executions/:ex`, ({ params }) => HttpResponse.json(params.ex === EXECUTION_ONE_ID ? original : current)),
      http.get(`${BASE}/worker-items/${WORKER_ITEM_ID}/executions/:ex/native-trace`, ({ params }) => {
        selected.push(String(params.ex));
        return HttpResponse.json({ workspace_id: WORKSPACE_ID, worker_item_id: WORKER_ITEM_ID, execution_id: EXECUTION_ONE_ID,
          run_id: runId, profile_ref: profileRef, next_sequence: 8, has_more: false,
          events: [{ run_id: runId, sequence: 8, kind: "historical.original", occurred_at: "2026-10-07T00:00:00Z",
            operation_id: null, response_id: null, data: { text: "Original historical Run, not the current owner" } }] });
      }),
      http.get("/api/v1/tasks/:root/executions/:ex/sessions/:session/trace", () => { legacyReads += 1; return HttpResponse.json({}); }),
      http.post("*", () => { writes += 1; return HttpResponse.json({}); }),
    );
    const user = userEvent.setup(); renderPage();
    await screen.findByRole("heading", { name: "实现回调幂等" });
    expect(screen.queryByText("Original historical Run, not the current owner")).not.toBeInTheDocument();
    expect(selected).toEqual([]);
    await user.click(await screen.findByRole("button", { name: "系统记录" }));
    const entry = await screen.findByRole("button", { name: "查看 Agent Trace" });
    await user.click(entry);
    const dialog = await screen.findByRole("dialog", { name: "Agent Trace" });
    await within(dialog).findByRole("heading", { name: "historical.original" });
    expect(within(dialog).getByText("Profile R1")).toBeVisible();
    expect(within(dialog).queryByText("Profile R2")).not.toBeInTheDocument();
    await user.click(within(dialog).getByText("原始执行", { selector: "summary" }));
    expect(within(dialog).getByText(EXECUTION_ONE_ID)).toBeVisible();
    expect(within(dialog).getByText(runId)).toBeVisible();
    expect(selected).toEqual([EXECUTION_ONE_ID]);
    await user.click(within(dialog).getByRole("button", { name: "关闭 Agent Trace" }));
    expect(entry).toHaveFocus();
    expect(screen.getByRole("region", { name: "当前进展" })).toHaveTextContent("运行回调测试");
    expect(screen.getByText("只修改这个回调重试规则")).toBeVisible();
    expect(screen.getByRole("textbox", { name: "介入这个 Worker" })).toBeVisible();
    expect(screen.queryByText("Original historical Run, not the current owner")).not.toBeInTheDocument();
    expect(legacyReads).toBe(0); expect(writes).toBe(0);
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

    const readOnly = await screen.findByText("只读", { selector: '[role="status"] strong' });
    expect(readOnly).toBeVisible();
    expect(screen.getAllByText("这个 WorkerItem 已退役，只能查看历史上下文").length).toBeGreaterThan(0);
    expect(screen.queryByRole("region", { name: "协同状态" })).not.toBeInTheDocument();
  });
});
