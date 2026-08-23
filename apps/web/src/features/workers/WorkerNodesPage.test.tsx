import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { server } from "@/test/server";

import { WorkerNodePage } from "./WorkerNodePage";
import { WorkerNodesPage } from "./WorkerNodesPage";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const READY_WORKER_ID = "80000000-0000-0000-0000-000000000001";
const MISSING_WORKER_ID = "80000000-0000-0000-0000-000000000002";

const adapterModule = {
  id: "a0000000-0000-0000-0000-000000000001",
  instance_name: "OpenCode Adapter",
  provider: "opencode",
  module_type: "worker_adapter",
  config_version: 4,
  definition_lifecycle: "active",
  instance_lifecycle: "active",
};

const workspaceModule = {
  id: "b0000000-0000-0000-0000-000000000001",
  instance_name: "Git Worktree",
  provider: "git-worktree",
  module_type: "workspace_provider",
  config_version: 2,
  definition_lifecycle: "active",
  instance_lifecycle: "active",
};

const readyWorker = {
  id: READY_WORKER_ID,
  workspace_id: WORKSPACE_ID,
  display_name: "OpenCode Worker A",
  adapter_type: "opencode",
  status: "idle",
  desired_state: "active",
  capabilities: ["python", "typescript"],
  capacity: 4,
  current_load: 2,
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
  adapter_module_summary: adapterModule,
  workspace_module_summary: workspaceModule,
};

const missingWorker = {
  ...readyWorker,
  id: MISSING_WORKER_ID,
  display_name: "OpenCode Worker B",
  current_load: 0,
  runtime_configuration_status: "missing",
  runtime_configuration_reason: "runtime_configuration_missing",
  runtime_configuration_version: null,
  runtime_configured_at: null,
  adapter_module_summary: null,
  workspace_module_summary: null,
};

function renderPage(initialEntry = `/workspaces/${WORKSPACE_ID}/workers`) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route path="/workspaces/:workspaceId/workers" element={<WorkerNodesPage />} />
          <Route path="/workspaces/:workspaceId/workers/:workerId" element={<WorkerNodePage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("WorkerNodesPage", () => {
  beforeEach(() => {
    server.resetHandlers();
  });

  it("shows a loading state before the execution node list arrives", async () => {
    server.use(
      http.get("/api/v1/workers", async () => {
        await delay(100);
        return HttpResponse.json([readyWorker]);
      }),
    );

    renderPage();

    expect(screen.getByTestId("worker-nodes-loading")).toBeVisible();
    expect(await screen.findByText("OpenCode Worker A")).toBeVisible();
  });

  it("explains an unavailable list and an empty workspace", async () => {
    server.use(
      http.get(
        "/api/v1/workers",
        () => HttpResponse.json({ error: { message: "upstream unavailable" } }, { status: 503 }),
      ),
    );
    renderPage();
    expect(await screen.findByRole("alert")).toHaveTextContent("无法读取执行节点");

    server.resetHandlers();
    server.use(http.get("/api/v1/workers", () => HttpResponse.json([])));
    renderPage();
    expect(await screen.findByText("当前工作区还没有执行节点")).toBeVisible();
  });

  it("lets a developer scan node health, capacity, and runtime configuration", async () => {
    server.use(http.get("/api/v1/workers", () => HttpResponse.json([readyWorker, missingWorker])));

    renderPage();

    expect(await screen.findByRole("heading", { name: "执行节点" })).toBeVisible();
    expect(await screen.findByText("OpenCode Worker A")).toBeVisible();
    expect(screen.getByText("2 / 4")).toBeVisible();
    expect(screen.getByText("配置可用")).toBeVisible();
    expect(screen.getByText("未配置")).toBeVisible();
    expect(screen.getByRole("link", { name: "OpenCode Worker A" })).toHaveAttribute(
      "href",
      `/workspaces/${WORKSPACE_ID}/workers/${READY_WORKER_ID}`,
    );
  });

  it("opens a worker detail with the keyboard and returns to the node list", async () => {
    const user = userEvent.setup();
    server.use(
      http.get(`/api/v1/workers/${READY_WORKER_ID}`, () => HttpResponse.json(readyWorker)),
      http.get("/api/v1/modules", ({ request }) => {
        const type = new URL(request.url).searchParams.get("module_type");
        return HttpResponse.json(type === "worker_adapter" ? [adapterModule] : [workspaceModule]);
      }),
    );

    renderPage(`/workspaces/${WORKSPACE_ID}/workers/${READY_WORKER_ID}`);

    expect(await screen.findByRole("heading", { name: "OpenCode Worker A" })).toBeVisible();
    const configureButton = screen.getByRole("button", { name: "调整运行配置" });
    configureButton.focus();
    await user.keyboard("{Enter}");
    expect(await screen.findByRole("dialog", { name: "调整运行配置" })).toBeVisible();

    await user.click(screen.getByRole("button", { name: "关闭运行配置" }));
    const backLink = screen.getByRole("link", { name: "执行节点" });
    await user.click(backLink);
    await waitFor(() => expect(screen.getByRole("heading", { name: "执行节点" })).toBeVisible());
    expect(screen.queryByRole("button", { name: "调整运行配置" })).not.toBeInTheDocument();
  });

  it("keeps the detail columns readable on a compact viewport", async () => {
    server.use(http.get(`/api/v1/workers/${READY_WORKER_ID}`, () => HttpResponse.json(readyWorker)));

    renderPage(`/workspaces/${WORKSPACE_ID}/workers/${READY_WORKER_ID}`);

    const facts = await screen.findByRole("region", { name: "执行节点事实" });
    expect(within(facts).getByText("容量")).toBeVisible();
    expect(within(facts).getByText("2 / 4")).toBeVisible();
  });
});
