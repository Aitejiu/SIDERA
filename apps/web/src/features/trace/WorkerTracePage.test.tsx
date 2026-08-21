import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { server } from "@/test/server";

import { WorkerTracePage } from "./WorkerTracePage";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const ROOT_TASK_ID = "30000000-0000-0000-0000-000000000001";
const EXECUTION_ID = "60000000-0000-0000-0000-000000000001";
const SESSION_ID = "70000000-0000-0000-0000-000000000001";
const WORKER_ID = "80000000-0000-0000-0000-000000000001";
const PLAN_ID = "10000000-0000-0000-0000-000000000002";
const ARTIFACT_ID = "90000000-0000-0000-0000-000000000001";

function makeEvent(sequence: number) {
  const escalation = sequence === 200;
  return {
    schema_version: 1,
    workspace_id: WORKSPACE_ID,
    root_task_id: ROOT_TASK_ID,
    work_item_key: "backend",
    plan_snapshot_id: PLAN_ID,
    execution_id: EXECUTION_ID,
    session_id: SESSION_ID,
    sequence,
    occurred_at: `2026-08-21T08:${String(Math.floor(sequence / 60)).padStart(2, "0")}:${String(sequence % 60).padStart(2, "0")}Z`,
    category: sequence % 5 === 0 ? "tool" : "assistant_summary",
    status: "running",
    title: escalation ? "scope_escalation_requested" : `event-${sequence}`,
    summary: escalation ? "scope_escalation_requested" : `Normalized trace event ${sequence}`,
    duration_ms: sequence % 5 === 0 ? 24 : null,
    usage: null,
    artifact_refs: sequence === 180 ? [ARTIFACT_ID] : [],
    provider_payload_ref: null,
    provider_extensions: sequence === 180 ? { "vendor:unknown": { safe: true } } : {},
  };
}

const session = {
  id: SESSION_ID,
  workspace_id: WORKSPACE_ID,
  execution_id: EXECUTION_ID,
  worker_id: WORKER_ID,
  adapter_session_id: "opaque-adapter-session",
  status: "active",
  last_sequence: 240,
  last_activity_at: "2026-08-21T08:04:00Z",
  input_mode: "queued",
  version: 3,
  adapter_module_id: null,
  adapter_config_version: null,
  capability_snapshot: null,
  created_at: "2026-08-21T08:00:00Z",
  updated_at: "2026-08-21T08:04:00Z",
  workspace_config_version: null,
  workspace_module_id: null,
  workspace_ref: null,
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
  created_at: "2026-08-21T08:00:00Z",
  updated_at: "2026-08-21T08:04:00Z",
};

function traceResponse(events: ReturnType<typeof makeEvent>[], previous: number | null) {
  return {
    schema_version: 1,
    execution_id: EXECUTION_ID,
    session_id: SESSION_ID,
    worker_id: WORKER_ID,
    status: "running",
    current_action: "Running test suite",
    plan_generation: 2,
    checkpoint_sequence: 180,
    freshness_at: "2026-08-21T08:04:00Z",
    last_change_cursor: 42,
    timing: { input_ms: 10, model_ms: 180, tools_ms: 240, total_ms: 430 },
    events,
    previous_before_sequence: previous,
    context_branch: {
      root_task_id: ROOT_TASK_ID,
      plan_snapshot_id: PLAN_ID,
      work_item_key: "backend",
      worker_slot_key: "backend",
      execution_lineage_id: EXECUTION_ID,
      current_execution_id: EXECUTION_ID,
      current_session_id: SESSION_ID,
      head_checkpoint_sequence: 180,
      artifact_refs: [ARTIFACT_ID],
      replay_capability: "portable_replay",
    },
  };
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}/executions/${EXECUTION_ID}/sessions/${SESSION_ID}/trace`]}>
        <Routes>
          <Route path="/workspaces/:workspaceId/tasks/:rootTaskId/executions/:executionId/sessions/:sessionId/trace" element={<WorkerTracePage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("WorkerTracePage", () => {
  beforeEach(() => {
    server.resetHandlers();
    server.use(
      http.get(`/api/v1/tasks/${ROOT_TASK_ID}/executions/${EXECUTION_ID}/sessions/${SESSION_ID}/trace`, ({ request }) => {
        const before = new URL(request.url).searchParams.get("before_sequence");
        if (before !== null) {
          return HttpResponse.json(traceResponse([1, 2, 3].map(makeEvent), null));
        }
        return HttpResponse.json(traceResponse(Array.from({ length: 100 }, (_, index) => makeEvent(index + 141)), 141));
      }),
      http.get("/api/v1/gateway/sessions", () => HttpResponse.json({ items: [session], next_cursor: null })),
      http.get(`/api/v1/executions/${EXECUTION_ID}`, () => HttpResponse.json(execution)),
      http.get("/api/v1/gateway/inputs", () => HttpResponse.json({ items: [], next_cursor: null })),
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/artifacts/${ARTIFACT_ID}/descriptor`, () => HttpResponse.json({
        artifact_id: ARTIFACT_ID,
        workspace_id: WORKSPACE_ID,
        media_type: "text/plain",
        byte_size: 24,
        content_hash: "a".repeat(64),
        download_name: "report.txt",
        content_available: true,
        unavailable_reason: null,
      })),
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/artifacts/${ARTIFACT_ID}/content`, () => new HttpResponse("artifact content", { headers: { "Content-Type": "text/plain" } })),
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/capabilities`, () => HttpResponse.json({ main_agent: { available: true, entry_uri: "http://main-agent.local" } })),
    );
  });

  it("shows a bounded trace ledger, loads older events, and opens artifact content by id", async () => {
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByText("Worker 轨迹")).toBeVisible();
    expect(await screen.findByRole("tab", { name: "Input" })).toBeVisible();
    expect(await screen.findByText("Model", { selector: ".timing-metric span" })).toBeVisible();
    expect(await screen.findByText("event-141")).toBeVisible();
    expect(screen.queryByText("vendor:unknown")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /event-180/ }));
    expect(await screen.findByText("vendor:unknown")).toBeVisible();
    await user.click(screen.getByRole("tab", { name: /Artifacts/ }));
    await user.click(screen.getByRole("button", { name: ARTIFACT_ID }));
    expect(await screen.findByText("artifact content")).toBeVisible();

    await user.click(screen.getByRole("button", { name: "更早事件" }));
    expect(await screen.findByText("event-1")).toBeVisible();

    await user.click(screen.getByRole("button", { name: /scope_escalation_requested/ }));
    expect(await screen.findByRole("link", { name: /与 Main Agent 对话/ })).toHaveAttribute("href", "http://main-agent.local");
  });
});
