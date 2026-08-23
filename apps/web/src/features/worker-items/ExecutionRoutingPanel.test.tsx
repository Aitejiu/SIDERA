import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { server } from "@/test/server";

import { ExecutionRoutingPanel } from "./ExecutionRoutingPanel";
import type { WorkerExecution } from "./types";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const EXECUTION_ID = "60000000-0000-0000-0000-000000000009";
const READY_WORKER_ID = "80000000-0000-0000-0000-000000000001";
const MISSING_WORKER_ID = "80000000-0000-0000-0000-000000000002";
const OFFLINE_WORKER_ID = "80000000-0000-0000-0000-000000000003";
const FULL_WORKER_ID = "80000000-0000-0000-0000-000000000004";
const INCOMPATIBLE_WORKER_ID = "80000000-0000-0000-0000-000000000005";

function worker(overrides: Record<string, unknown> = {}) {
  return {
    id: READY_WORKER_ID,
    workspace_id: WORKSPACE_ID,
    display_name: "OpenCode Ready",
    adapter_type: "opencode",
    status: "idle",
    desired_state: "active",
    capabilities: ["python"],
    capacity: 2,
    current_load: 0,
    cost_per_hour_microunits: 100,
    startup_latency_ms: 300,
    last_heartbeat_at: "2026-08-23T08:05:00Z",
    version: 8,
    created_at: "2026-08-23T07:00:00Z",
    updated_at: "2026-08-23T08:05:00Z",
    runtime_configuration_status: "ready",
    runtime_configuration_reason: null,
    runtime_configuration_version: 7,
    runtime_configured_at: "2026-08-23T07:30:00Z",
    adapter_module_summary: null,
    workspace_module_summary: null,
    ...overrides,
  };
}

const candidates = [
  worker(),
  worker({
    id: MISSING_WORKER_ID,
    display_name: "OpenCode Needs Config",
    runtime_configuration_status: "missing",
    runtime_configuration_version: null,
  }),
  worker({
    id: OFFLINE_WORKER_ID,
    display_name: "OpenCode Offline",
    status: "offline",
  }),
  worker({
    id: FULL_WORKER_ID,
    display_name: "OpenCode Full",
    capacity: 2,
    current_load: 2,
  }),
  worker({
    id: INCOMPATIBLE_WORKER_ID,
    display_name: "Codex Missing Python",
    adapter_type: "codex",
    capabilities: ["rust"],
  }),
];

const queuedExecution: WorkerExecution = {
  execution_id: EXECUTION_ID,
  task_id: "30000000-0000-0000-0000-000000000002",
  status: "queued",
  attempt: 3,
  worker_id: null,
  session_id: null,
  failure_code: null,
  started_at: null,
  finished_at: null,
  created_at: "2026-08-23T08:00:00Z",
  updated_at: "2026-08-23T08:05:00Z",
  version: 9,
  capability_requirements: ["python"],
  adapter_preferences: ["opencode"],
  adapter_forbidden: ["unsafe"],
  scheduling_status: "ready",
  waiting_reason: null,
  waiting_details_summary: null,
  requires_operator: false,
  blocked_since: null,
  pinned_worker_id: null,
  assigned_worker_id: null,
  pinned_worker_summary: null,
  assigned_worker_summary: null,
  binding_summary: null,
};

function renderPanel(execution: WorkerExecution, onRefresh = vi.fn(async () => undefined)) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <ExecutionRoutingPanel
          workspaceId={WORKSPACE_ID}
          execution={execution}
          onRefresh={onRefresh}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("ExecutionRoutingPanel", () => {
  beforeEach(() => {
    server.resetHandlers();
    server.use(
      http.get("/api/v1/workers", () => HttpResponse.json(candidates)),
    );
  });

  it("shows queued attempt mode and all candidate reasons without hiding hard incompatibility", async () => {
    const user = userEvent.setup();
    renderPanel(queuedExecution);

    expect(screen.getByText("执行尝试 #3")).toBeVisible();
    expect(screen.getByText("自动选择")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "调整执行节点" }));

    const chooser = await screen.findByRole("region", { name: "执行节点选择器" });
    expect(within(chooser).getByText("OpenCode Ready")).toBeVisible();
    expect(within(chooser).getByText("运行配置未配置")).toBeVisible();
    expect(within(chooser).getByText("节点离线")).toBeVisible();
    expect(within(chooser).getByText("容量已满")).toBeVisible();
    const incompatible = within(chooser).getByRole("button", { name: /Codex Missing Python/ });
    expect(incompatible).toBeDisabled();
  });

  it("pins a queued attempt with the latest execution version and explains missing runtime", async () => {
    const user = userEvent.setup();
    const onRefresh = vi.fn(async () => undefined);
    const requestBodies: Record<string, unknown>[] = [];
    server.use(
      http.post("/api/v1/commands", async ({ request }) => {
        requestBodies.push(await request.json() as Record<string, unknown>);
        return HttpResponse.json({ command_id: "c1", aggregate_id: EXECUTION_ID, aggregate_version: 10, event_ids: [] });
      }),
    );
    renderPanel(queuedExecution, onRefresh);

    await user.click(screen.getByRole("button", { name: "调整执行节点" }));
    const chooser = await screen.findByRole("region", { name: "执行节点选择器" });
    await user.click(within(chooser).getByRole("button", { name: /OpenCode Needs Config/ }));
    expect(await within(chooser).findByText("该执行会等待节点完成运行配置")).toBeVisible();
    await user.click(within(chooser).getByRole("button", { name: "固定到此节点" }));

    await screen.findByText("已固定到 OpenCode Needs Config");
    await vi.waitFor(() => expect(onRefresh).toHaveBeenCalled());
    const requestBody = requestBodies.at(-1);
    expect(requestBody).toBeDefined();
    if (requestBody === undefined) throw new Error("command request was not captured");
    expect(requestBody.expected_version).toBe(9);
    expect(requestBody.command_type).toBe("PinExecutionWorker");
    expect(requestBody.target_id).toBe(EXECUTION_ID);
    expect(requestBody.payload).toEqual({ worker_id: MISSING_WORKER_ID });
  });

  it("keeps running bindings immutable and supports clearing a queued pin", async () => {
    const user = userEvent.setup();
    const running: WorkerExecution = {
      ...queuedExecution,
      status: "running",
      version: 12,
      worker_id: READY_WORKER_ID,
      assigned_worker_id: READY_WORKER_ID,
      scheduling_status: "assigned",
      assigned_worker_summary: {
        id: READY_WORKER_ID,
        display_name: "OpenCode Ready",
        adapter_type: "opencode",
        status: "busy",
        desired_state: "active",
        current_load: 1,
        capacity: 2,
      },
      binding_summary: {
        id: "b0000000-0000-0000-0000-000000000001",
        worker_id: READY_WORKER_ID,
        adapter_module_id: "a0000000-0000-0000-0000-000000000001",
        workspace_module_id: "a0000000-0000-0000-0000-000000000002",
        adapter_config_version: 4,
        workspace_config_version: 2,
        fencing_token: 8,
        version: 1,
      },
    };
    renderPanel(running);
    expect(await screen.findByText("当前执行已绑定；更换节点需要停止并创建新的执行尝试")).toBeVisible();
    expect(screen.queryByRole("button", { name: "调整执行节点" })).not.toBeInTheDocument();

    server.resetHandlers();
    server.use(
      http.post("/api/v1/commands", async ({ request }) => {
        const body = await request.json() as Record<string, unknown>;
        expect(body.command_type).toBe("ClearExecutionWorkerPin");
        return HttpResponse.json({ command_id: "c2", aggregate_id: EXECUTION_ID, aggregate_version: 13, event_ids: [] });
      }),
      http.get("/api/v1/workers", () => HttpResponse.json(candidates)),
    );
    const pinned: WorkerExecution = {
      ...queuedExecution,
      pinned_worker_id: READY_WORKER_ID,
      pinned_worker_summary: running.assigned_worker_summary,
    };
    renderPanel(pinned);
    await user.click(screen.getByRole("button", { name: "恢复自动选择" }));
    expect(await screen.findByText("已恢复自动选择")).toBeVisible();
  });

  it("keeps the chooser open after an execution version conflict", async () => {
    const user = userEvent.setup();
    server.use(
      http.post(
        "/api/v1/commands",
        () => HttpResponse.json({ error: { message: "version conflict" } }, { status: 409 }),
      ),
    );
    renderPanel(queuedExecution);
    await user.click(screen.getByRole("button", { name: "调整执行节点" }));
    const chooser = await screen.findByRole("region", { name: "执行节点选择器" });
    await user.click(within(chooser).getByRole("button", { name: /OpenCode Ready/ }));
    await user.click(within(chooser).getByRole("button", { name: "固定到此节点" }));
    expect(await within(chooser).findByRole("alert")).toHaveTextContent(
      "执行状态已变化，请刷新后重新确认",
    );
    expect(screen.getByRole("region", { name: "执行节点选择器" })).toBeInTheDocument();
  });
});
