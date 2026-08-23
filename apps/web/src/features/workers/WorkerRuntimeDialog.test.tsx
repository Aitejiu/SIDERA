import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { server } from "@/test/server";

import { WorkerRuntimeDialog } from "./WorkerRuntimeDialog";
import type { Worker } from "./types";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const WORKER_ID = "80000000-0000-0000-0000-000000000001";

const activeAdapter = {
  id: "a0000000-0000-0000-0000-000000000001",
  instance_name: "OpenCode Adapter",
  provider: "opencode",
  module_type: "worker_adapter",
  config_version: 4,
  definition_lifecycle: "active",
  instance_lifecycle: "active",
};
const inactiveAdapter = {
  ...activeAdapter,
  id: "a0000000-0000-0000-0000-000000000002",
  instance_name: "Retired OpenCode Adapter",
  config_version: 9,
  instance_lifecycle: "disabled",
};
const wrongAdapter = {
  ...activeAdapter,
  id: "a0000000-0000-0000-0000-000000000003",
  instance_name: "Codex Adapter",
  provider: "codex",
};
const activeWorkspace = {
  id: "b0000000-0000-0000-0000-000000000001",
  instance_name: "Git Worktree",
  provider: "git-worktree",
  module_type: "workspace_provider",
  config_version: 2,
  definition_lifecycle: "active",
  instance_lifecycle: "active",
};
const inactiveWorkspace = {
  ...activeWorkspace,
  id: "b0000000-0000-0000-0000-000000000002",
  instance_name: "Retired Worktree",
  instance_lifecycle: "draining",
};

const missingWorker: Worker = {
  id: WORKER_ID,
  workspace_id: WORKSPACE_ID,
  display_name: "OpenCode Worker A",
  adapter_type: "opencode",
  status: "idle",
  desired_state: "active",
  capabilities: ["python"],
  capacity: 4,
  current_load: 0,
  cost_per_hour_microunits: 100,
  startup_latency_ms: 300,
  last_heartbeat_at: "2026-08-23T08:05:00Z",
  version: 8,
  created_at: "2026-08-23T07:00:00Z",
  updated_at: "2026-08-23T08:05:00Z",
  runtime_configuration_status: "missing",
  runtime_configuration_reason: null,
  runtime_configuration_version: null,
  runtime_configured_at: null,
  adapter_module_summary: null,
  workspace_module_summary: null,
};

const configuredWorker: Worker = {
  ...missingWorker,
  runtime_configuration_status: "ready",
  runtime_configuration_version: 7,
  runtime_configured_at: "2026-08-23T07:30:00Z",
  adapter_module_summary: activeAdapter,
  workspace_module_summary: activeWorkspace,
};

function renderDialog(worker: Worker, onClose = vi.fn(), onSaved = vi.fn()) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <WorkerRuntimeDialog
          workspaceId={WORKSPACE_ID}
          worker={worker}
          onClose={onClose}
          onSaved={onSaved}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function moduleHandler() {
  return http.get("/api/v1/modules", ({ request }) => {
    const type = new URL(request.url).searchParams.get("module_type");
    return HttpResponse.json(
      type === "worker_adapter"
        ? [activeAdapter, inactiveAdapter, wrongAdapter]
        : [activeWorkspace, inactiveWorkspace],
    );
  });
}

describe("WorkerRuntimeDialog", () => {
  beforeEach(() => {
    server.resetHandlers();
  });

  it("offers only active same-adapter modules and explains the binding boundary", async () => {
    server.use(moduleHandler());
    renderDialog(missingWorker);

    const dialog = await screen.findByRole("dialog", { name: "调整运行配置" });
    expect(await within(dialog).findByRole("option", { name: /OpenCode Adapter/ })).toBeVisible();
    expect(within(dialog).queryByRole("option", { name: /Retired OpenCode Adapter/ })).not.toBeInTheDocument();
    expect(within(dialog).queryByRole("option", { name: /Codex Adapter/ })).not.toBeInTheDocument();
    expect(within(dialog).getByRole("option", { name: /Git Worktree/ })).toBeVisible();
    expect(within(dialog).queryByRole("option", { name: /Retired Worktree/ })).not.toBeInTheDocument();
    expect(within(dialog).getByText("该修改只影响之后分配的新执行；当前执行继续使用原绑定配置。"))
      .toBeVisible();
  });

  it("uses a null expected version for first configuration", async () => {
    const user = userEvent.setup();
    const requestBodies: Record<string, unknown>[] = [];
    const onSaved = vi.fn();
    server.use(
      moduleHandler(),
      http.post("/api/v1/commands", async ({ request }) => {
        requestBodies.push(await request.json() as Record<string, unknown>);
        return HttpResponse.json({
          command_id: "c0000000-0000-0000-0000-000000000001",
          aggregate_id: WORKER_ID,
          aggregate_version: 1,
          event_ids: [],
        }, { status: 201 });
      }),
    );
    renderDialog(missingWorker, vi.fn(), onSaved);

    const dialog = await screen.findByRole("dialog", { name: "调整运行配置" });
    await user.selectOptions(await within(dialog).findByRole("combobox", { name: "Adapter 模块" }), activeAdapter.id);
    await user.selectOptions(await within(dialog).findByRole("combobox", { name: "工作区提供器" }), activeWorkspace.id);
    await user.click(await within(dialog).findByRole("button", { name: "保存运行配置" }));

    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    const commandRequest = requestBodies[0];
    if (!commandRequest) throw new Error("configuration command was not sent");
    expect(commandRequest.expected_version).toBeNull();
    expect(commandRequest.target_id).toBe(WORKER_ID);
    expect(commandRequest.target_type).toBe("worker_runtime_configuration");
    expect(commandRequest.command_type).toBe("ConfigureWorkerRuntime");
    expect(commandRequest.payload).toEqual({
      adapter_module_instance_id: activeAdapter.id,
      adapter_module_config_version: activeAdapter.config_version,
      workspace_module_instance_id: activeWorkspace.id,
      workspace_module_config_version: activeWorkspace.config_version,
    });
  });

  it("uses the current runtime version and keeps the dialog open on OCC conflict", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    server.use(
      moduleHandler(),
      http.post(
        "/api/v1/commands",
        () => HttpResponse.json({ error: { message: "version conflict" } }, { status: 409 }),
      ),
    );
    renderDialog(configuredWorker, onClose);

    const dialog = await screen.findByRole("dialog", { name: "调整运行配置" });
    await user.click(await within(dialog).findByRole("button", { name: "保存运行配置" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "配置已变化，请刷新后重新确认",
    );
    expect(onClose).not.toHaveBeenCalled();
    expect(dialog).toBeInTheDocument();
  });
});
