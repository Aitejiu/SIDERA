import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { server } from "@/test/server";

import { PlanningBriefForm } from "./PlanningBriefForm";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const PROFILE_ID = "10000000-0000-0000-0000-000000000001";
const REQUEST_ID = "30000000-0000-0000-0000-000000000001";

const profile = {
  id: PROFILE_ID,
  workspace_id: WORKSPACE_ID,
  profile_key: "default-opencode",
  version: 2,
  display_name: "默认规划器",
  planner_provider: "opencode",
  planner_module_instance_id: "40000000-0000-0000-0000-000000000001",
  planner_module_config_version: 1,
  planner_interface_version: "1.0",
  status: "active",
  user_selectable: true,
  capabilities: {
    structured_output: true,
    clarification: true,
    idempotency: true,
    cancellation: true,
    usage_reporting: true,
    max_context_bytes: 200000,
    stability: "stable",
    interface_version: "1.0",
    proposal_schema_versions: [1],
  },
  created_at: "2026-08-23T08:00:00Z",
};

function renderForm() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/planning/new`]}>
        <Routes>
          <Route path="/workspaces/:workspaceId/planning/new" element={<PlanningBriefForm />} />
          <Route path="/workspaces/:workspaceId/planning/:planningRequestId" element={<div>规划请求已创建</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function installProfileHandler() {
  server.use(
    http.get(`/api/v1/workspaces/${WORKSPACE_ID}/planning-profiles`, () =>
      HttpResponse.json({ items: [profile], next_cursor: null }),
    ),
  );
}

describe("PlanningBriefForm", () => {
  beforeEach(() => {
    server.resetHandlers();
    installProfileHandler();
  });

  it("requires an objective and one acceptance criterion", async () => {
    const user = userEvent.setup();
    renderForm();

    await screen.findByRole("option", { name: "默认规划器" });
    await user.click(screen.getByRole("button", { name: "提交需求" }));

    expect(screen.getByText("请填写需求目标")).toBeVisible();
    expect(screen.getByText("至少填写一条验收条件")).toBeVisible();
  });

  it("adds and removes repeatable brief fields without exposing internal worker concepts", async () => {
    const user = userEvent.setup();
    renderForm();

    await screen.findByRole("option", { name: "默认规划器" });
    expect(screen.getAllByLabelText("约束")).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "添加约束" }));
    expect(screen.getAllByLabelText("约束")).toHaveLength(2);
    await user.click(screen.getAllByRole("button", { name: "删除约束" })[1]!);
    expect(screen.getAllByLabelText("约束")).toHaveLength(1);

    expect(screen.queryByText(/DAG|Worker ID|Planner JSON|模块实例/)).not.toBeInTheDocument();
  });

  it("prevents duplicate submits and opens the planning request from response data", async () => {
    const user = userEvent.setup();
    let release: (() => void) | undefined;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    server.use(
      http.post("/api/v1/commands", async ({ request }) => {
        await gate;
        const body = await request.json() as Record<string, unknown>;
        return HttpResponse.json({
          aggregate_id: REQUEST_ID,
          aggregate_version: 1,
          command_id: body.command_id,
          event_ids: [],
          data: { planning_request_id: REQUEST_ID },
        }, { status: 201 });
      }),
    );
    renderForm();

    await screen.findByRole("option", { name: "默认规划器" });
    await user.type(screen.getByLabelText("需求目标"), "交付任务控制台");
    await user.type(screen.getByLabelText("验收条件"), "用户可以查看规划进度");
    await user.click(screen.getByRole("button", { name: "提交需求" }));
    const submitButton = screen.getByRole("button", { name: "正在提交" });
    expect(submitButton).toBeDisabled();
    await user.click(submitButton);
    release?.();

    expect(await screen.findByText("规划请求已创建")).toBeVisible();
  });

  it("keeps every input after a command error", async () => {
    const user = userEvent.setup();
    server.use(
      http.post("/api/v1/commands", () =>
        HttpResponse.json({
          error: {
            code: "conflict",
            message: "需求已发生变化",
            details: { expected_version: 1 },
          },
        }, { status: 409 }),
      ),
    );
    renderForm();

    await screen.findByRole("option", { name: "默认规划器" });
    await user.type(screen.getByLabelText("需求目标"), "保留原始上下文");
    await user.type(screen.getByLabelText("约束"), "不能重建已有执行");
    await user.type(screen.getByLabelText("验收条件"), "失败后仍然保留输入");
    await user.clear(screen.getByLabelText("期望产物"));
    await user.type(screen.getByLabelText("期望产物"), "任务计划");
    await user.type(screen.getByLabelText("上下文引用"), "docs/spec.md");
    await user.click(screen.getByRole("button", { name: "提交需求" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("需求已发生变化");
    expect(screen.getByLabelText("需求目标")).toHaveValue("保留原始上下文");
    expect(screen.getByLabelText("约束")).toHaveValue("不能重建已有执行");
    expect(screen.getByLabelText("验收条件")).toHaveValue("失败后仍然保留输入");
    expect(screen.getByLabelText("期望产物")).toHaveValue("任务计划");
    expect(screen.getByLabelText("上下文引用")).toHaveValue("docs/spec.md");
  });

  it("treats a successful response without planning_request_id as a contract error", async () => {
    const user = userEvent.setup();
    server.use(
      http.post("/api/v1/commands", () =>
        HttpResponse.json({
          aggregate_id: REQUEST_ID,
          aggregate_version: 1,
          command_id: "50000000-0000-0000-0000-000000000001",
          event_ids: [],
          data: {},
        }, { status: 201 }),
      ),
    );
    renderForm();

    await screen.findByRole("option", { name: "默认规划器" });
    await user.type(screen.getByLabelText("需求目标"), "测试契约错误");
    await user.type(screen.getByLabelText("验收条件"), "显示错误");
    await user.click(screen.getByRole("button", { name: "提交需求" }));

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("响应缺少规划请求编号"));
    expect(screen.getByLabelText("需求目标")).toHaveValue("测试契约错误");
  });
});
