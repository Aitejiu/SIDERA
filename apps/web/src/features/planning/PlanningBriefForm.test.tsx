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
    proposal_schema_versions: [2],
  },
  created_at: "2026-08-23T08:00:00Z",
};

const nativeProfile = {
  ...profile,
  id: "10000000-0000-0000-0000-000000000005",
  profile_key: "native-planner",
  display_name: "原生规划配置",
  execution_mode: "native_profile",
  planner_agent_ref: {
    workspace_id: WORKSPACE_ID,
    profile_id: "60000000-0000-0000-0000-000000000001",
    revision: 1,
  },
  planner_provider: null,
  planner_module_instance_id: null,
  planner_module_config_version: null,
  capabilities: null,
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

  it("retains the legacy base selection when a native policy is listed first", async () => {
    const user = userEvent.setup();
    const revisionProfile = {
      ...profile,
      id: "10000000-0000-0000-0000-000000000002",
      display_name: "修订规划器",
      capabilities: { ...profile.capabilities, proposal_schema_versions: [2, 3] },
    };
    let submitted: { command_type?: string; payload?: { profile_id?: string } } | undefined;
    server.use(
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/planning-profiles`, () =>
        HttpResponse.json({ items: [nativeProfile, revisionProfile, profile], next_cursor: null }),
      ),
      http.post("/api/v1/commands", async ({ request }) => {
        submitted = await request.json() as typeof submitted;
        return HttpResponse.json({
          aggregate_id: REQUEST_ID,
          aggregate_version: 1,
          command_id: "50000000-0000-0000-0000-000000000005",
          event_ids: [],
          data: { planning_request_id: REQUEST_ID },
        }, { status: 201 });
      }),
    );
    renderForm();

    await screen.findByRole("option", { name: "默认规划器" });
    expect(screen.getByRole("combobox", { name: "规划配置" })).toHaveValue(PROFILE_ID);
    expect(screen.queryByRole("option", { name: "原生规划配置" })).not.toBeInTheDocument();
    await user.type(screen.getByLabelText("需求目标"), "保持基础规划配置");
    await user.type(screen.getByLabelText("验收条件"), "只提交可用的旧规划配置");
    await user.click(screen.getByRole("button", { name: "提交需求" }));

    await screen.findByText("规划请求已创建");
    expect(submitted?.command_type).toBe("SubmitPlanningRequest");
    expect(submitted?.payload?.profile_id).toBe(PROFILE_ID);
  });

  it("uses the unavailable state without legacy submission for native-only policies", async () => {
    const user = userEvent.setup();
    let submitted = 0;
    server.use(
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/planning-profiles`, () =>
        HttpResponse.json({ items: [nativeProfile], next_cursor: null }),
      ),
      http.post("/api/v1/commands", () => {
        submitted += 1;
        return HttpResponse.json({}, { status: 201 });
      }),
    );
    renderForm();

    const select = await screen.findByRole("combobox", { name: "规划配置" });
    expect(select).toBeDisabled();
    expect(screen.queryByRole("option", { name: "原生规划配置" })).not.toBeInTheDocument();
    await user.type(screen.getByLabelText("需求目标"), "保留原生配置");
    await user.type(screen.getByLabelText("验收条件"), "不得使用旧提交命令");
    await user.click(screen.getByRole("button", { name: "提交需求" }));

    expect(screen.getByText("当前工作区没有可用的规划配置")).toBeVisible();
    expect(submitted).toBe(0);
  });

  it("defaults new tasks to the base planning profile when a revision profile is listed first", async () => {
    const user = userEvent.setup();
    const revisionProfile = {
      ...profile,
      id: "10000000-0000-0000-0000-000000000002",
      profile_key: "revision-opencode",
      display_name: "修订规划器",
      capabilities: {
        ...profile.capabilities,
        proposal_schema_versions: [2, 3],
      },
    };
    let submittedProfileId: string | undefined;
    server.use(
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/planning-profiles`, () =>
        HttpResponse.json({ items: [revisionProfile, profile], next_cursor: null }),
      ),
      http.post("/api/v1/commands", async ({ request }) => {
        const body = await request.json() as { payload?: { profile_id?: string } };
        submittedProfileId = body.payload?.profile_id;
        return HttpResponse.json({
          aggregate_id: REQUEST_ID,
          aggregate_version: 1,
          command_id: "50000000-0000-0000-0000-000000000002",
          event_ids: [],
          data: { planning_request_id: REQUEST_ID },
        }, { status: 201 });
      }),
    );
    renderForm();

    await screen.findByRole("option", { name: "修订规划器" });
    await user.type(screen.getByLabelText("需求目标"), "使用基础规划配置");
    await user.type(screen.getByLabelText("验收条件"), "创建普通规划请求");
    await user.click(screen.getByRole("button", { name: "提交需求" }));

    await screen.findByText("规划请求已创建");
    expect(submittedProfileId).toBe(PROFILE_ID);
  });

  it("defaults result-enabled new tasks to the single-schema base profile", async () => {
    const user = userEvent.setup();
    const resultProfile = {
      ...profile,
      id: "10000000-0000-0000-0000-000000000003",
      profile_key: "result-enabled-opencode",
      display_name: "结果集成规划器",
      capabilities: {
        ...profile.capabilities,
        proposal_schema_versions: [6],
      },
    };
    const revisionProfile = {
      ...profile,
      id: "10000000-0000-0000-0000-000000000004",
      profile_key: "revision-opencode",
      display_name: "修订规划器",
      capabilities: {
        ...profile.capabilities,
        proposal_schema_versions: [2, 3],
      },
    };
    let submittedProfileId: string | undefined;
    server.use(
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/planning-profiles`, () =>
        HttpResponse.json({ items: [revisionProfile, resultProfile], next_cursor: null }),
      ),
      http.post("/api/v1/commands", async ({ request }) => {
        const body = await request.json() as { payload?: { profile_id?: string } };
        submittedProfileId = body.payload?.profile_id;
        return HttpResponse.json({
          aggregate_id: REQUEST_ID,
          aggregate_version: 1,
          command_id: "50000000-0000-0000-0000-000000000004",
          event_ids: [],
          data: { planning_request_id: REQUEST_ID },
        }, { status: 201 });
      }),
    );
    renderForm();

    await screen.findByRole("option", { name: "修订规划器" });
    await user.type(screen.getByLabelText("需求目标"), "使用结果集成规划配置");
    await user.type(screen.getByLabelText("验收条件"), "创建结果集成规划请求");
    await user.click(screen.getByRole("button", { name: "提交需求" }));

    await screen.findByText("规划请求已创建");
    expect(submittedProfileId).toBe(resultProfile.id);
  });

  it("does not submit while planning profiles are still loading", async () => {
    const user = userEvent.setup();
    let releaseProfiles: (() => void) | undefined;
    const profilesReady = new Promise<void>((resolve) => {
      releaseProfiles = resolve;
    });
    let submitted = false;
    server.use(
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/planning-profiles`, async () => {
        await profilesReady;
        return HttpResponse.json({ items: [profile], next_cursor: null });
      }),
      http.post("/api/v1/commands", () => {
        submitted = true;
        return HttpResponse.json({
          aggregate_id: REQUEST_ID,
          aggregate_version: 1,
          command_id: "50000000-0000-0000-0000-000000000003",
          event_ids: [],
          data: { planning_request_id: REQUEST_ID },
        }, { status: 201 });
      }),
    );
    renderForm();

    await user.type(screen.getByLabelText("需求目标"), "避免加载竞态");
    await user.type(screen.getByLabelText("验收条件"), "配置加载后才能提交");
    const submitButton = screen.getByRole("button", { name: "正在读取配置" });
    expect(submitButton).toBeDisabled();
    await user.click(submitButton);
    expect(submitted).toBe(false);

    releaseProfiles?.();
    await screen.findByRole("option", { name: "默认规划器" });
    await user.click(screen.getByRole("button", { name: "提交需求" }));
    await screen.findByText("规划请求已创建");
    expect(submitted).toBe(true);
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
