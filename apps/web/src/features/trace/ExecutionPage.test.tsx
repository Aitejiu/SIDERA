import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { server } from "@/test/server";

import { ExecutionPage } from "./ExecutionPage";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const ROOT_TASK_ID = "30000000-0000-0000-0000-000000000001";
const EXECUTION_ID = "60000000-0000-0000-0000-000000000001";
const SESSION_ID = "70000000-0000-0000-0000-000000000001";
const WORKER_ID = "80000000-0000-0000-0000-000000000001";
const WORKER_ITEM_ID = "40000000-0000-0000-0000-000000000001";
const CONTEXT_PACKAGE_ID = "90000000-0000-0000-0000-000000000001";

const trace = {
  schema_version: 1,
  execution_id: EXECUTION_ID,
  session_id: SESSION_ID,
  worker_id: WORKER_ID,
  status: "running",
  current_action: "Running tests",
  plan_generation: 2,
  checkpoint_sequence: 3,
  freshness_at: "2026-08-21T08:04:00Z",
  last_change_cursor: 42,
  timing: { input_ms: 10, model_ms: 20, tools_ms: 30, total_ms: 60 },
  events: [
    {
      schema_version: 1,
      workspace_id: WORKSPACE_ID,
      root_task_id: ROOT_TASK_ID,
      work_item_key: "backend",
      plan_snapshot_id: "10000000-0000-0000-0000-000000000002",
      execution_id: EXECUTION_ID,
      session_id: SESSION_ID,
      sequence: 1,
      occurred_at: "2026-08-21T08:01:00Z",
      category: "assistant_summary",
      status: "running",
      title: "Inspect repository",
      summary: "Repository inspected",
      duration_ms: 10,
      usage: null,
      artifact_refs: [],
      provider_payload_ref: null,
      provider_extensions: {},
    },
  ],
  previous_before_sequence: null,
  context_branch: {
    root_task_id: ROOT_TASK_ID,
    plan_snapshot_id: "10000000-0000-0000-0000-000000000002",
    work_item_key: "backend",
    worker_slot_key: "backend",
    execution_lineage_id: EXECUTION_ID,
    current_execution_id: EXECUTION_ID,
    current_session_id: SESSION_ID,
    head_checkpoint_sequence: 3,
    artifact_refs: [],
    replay_capability: "portable_replay",
  },
};

const session = {
  id: SESSION_ID,
  workspace_id: WORKSPACE_ID,
  execution_id: EXECUTION_ID,
  worker_id: WORKER_ID,
  adapter_session_id: "adapter-session",
  status: "active",
  last_sequence: 1,
  last_activity_at: "2026-08-21T08:04:00Z",
  input_mode: "queued",
  version: 3,
};

const execution = {
  id: EXECUTION_ID,
  workspace_id: WORKSPACE_ID,
  task_id: "40000000-0000-0000-0000-000000000001",
  lineage_id: EXECUTION_ID,
  attempt: 1,
  max_attempts: 3,
  purpose: "Implement backend",
  status: "running",
  capability_requirements: ["python"],
  pinned_worker_id: WORKER_ID,
  assigned_worker_id: WORKER_ID,
  active_lease_id: null,
  last_fencing_token: 1,
  not_before: "2026-08-21T08:00:00Z",
  failure_code: null,
  started_at: "2026-08-21T08:00:00Z",
  finished_at: null,
  version: 4,
  stop_requested: false,
  source_execution_id: null,
  source_session_id: null,
  worker_item_id: null,
};

const executionContextPackage = {
  package_id: CONTEXT_PACKAGE_ID,
  workspace_id: WORKSPACE_ID,
  purpose: "execution",
  consumer_type: "worker",
  consumer_id: EXECUTION_ID,
  worker_item_id: WORKER_ITEM_ID,
  work_context_revision_id: "91000000-0000-0000-0000-000000000001",
  work_context_revision_number: 5,
  work_context_content_hash: "a".repeat(64),
  context_version: 2,
  control_plane_fact_snapshot: {},
  knowledge_refs: [],
  artifact_refs: [],
  evidence_refs: [],
  budget: {
    profile_id: "92000000-0000-0000-0000-000000000001",
    profile_version: 1,
    max_sections: 1,
    max_chars: 100,
    max_input_tokens: 30,
    actual_sections: 0,
    actual_chars: 0,
    actual_input_tokens: 0,
  },
  expires_at: "2026-08-27T08:00:00Z",
  idempotency_key: "execution-context",
  package_hash: "b".repeat(64),
  status: "active",
  created_at: "2026-08-21T08:00:00Z",
  materialized_at: null,
  revoked_at: null,
  revocation_reason: null,
  revocation_scope: null,
  assembly: {
    request_id: "93000000-0000-0000-0000-000000000001",
    status: "ready",
    attempt: 1,
    next_attempt_at: null,
    error_code: null,
    request_fingerprint: "execution-context",
    version: 1,
    created_at: "2026-08-21T08:00:00Z",
    updated_at: "2026-08-21T08:00:00Z",
  },
  acknowledgements: [],
  materialization_audits: [],
};

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}/executions/${EXECUTION_ID}/sessions/${SESSION_ID}`]}>
        <Routes>
          <Route path="/workspaces/:workspaceId/tasks/:rootTaskId/executions/:executionId/sessions/:sessionId" element={<ExecutionPage />} />
          <Route path="/workspaces/:workspaceId/worker-items/:workerItemId" element={<div>worker-item-target</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("ExecutionPage", () => {
  beforeEach(() => {
    server.resetHandlers();
    server.use(
      http.get(`/api/v1/tasks/${ROOT_TASK_ID}/executions/${EXECUTION_ID}/sessions/${SESSION_ID}/trace`, () => HttpResponse.json(trace)),
      http.get("/api/v1/gateway/sessions", () => HttpResponse.json({ items: [session], next_cursor: null })),
      http.get(`/api/v1/executions/${EXECUTION_ID}`, () => HttpResponse.json(execution)),
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/executions/${EXECUTION_ID}/context-package`, () => HttpResponse.json(executionContextPackage)),
    );
  });

  it("shows execution steps and keeps trace diagnostic behind an explicit entry", async () => {
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByRole("heading", { name: "执行步骤" })).toBeVisible();
    expect(await screen.findByText("旧 Execution 只读")).toBeVisible();
    expect(screen.queryByRole("heading", { name: "介入当前 Session" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "用户介入记录" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "查看 Agent Trace" })).toBeVisible();
    expect(screen.getByRole("link", { name: "查看本次运行上下文" })).toHaveAttribute(
      "href",
      `/workspaces/${WORKSPACE_ID}/context-packages/${CONTEXT_PACKAGE_ID}`,
    );
    expect(screen.queryByRole("heading", { name: "Worker 轨迹" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "查看 Agent Trace" }));
    const dialog = await screen.findByRole("dialog", { name: "Agent Trace" });
    expect(dialog).toBeVisible();
    expect(within(dialog).getAllByRole("heading", { name: "Worker 轨迹" }).length).toBeGreaterThan(0);
    expect(within(dialog).getByRole("button", { name: "关闭 Agent Trace" })).toBeVisible();
  });

  it("redirects a mapped legacy execution to its stable WorkerItem workspace", async () => {
    server.use(
      http.get(`/api/v1/executions/${EXECUTION_ID}`, () => HttpResponse.json({
        ...execution,
        worker_item_id: WORKER_ITEM_ID,
      })),
    );
    renderPage();

    expect(await screen.findByText("worker-item-target")).toBeVisible();
    expect(screen.queryByRole("heading", { name: "执行步骤" })).not.toBeInTheDocument();
  });
});
