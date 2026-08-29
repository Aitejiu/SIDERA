import type { APIRequestContext, Page } from "@playwright/test";
import { randomUUID } from "node:crypto";

import {
  expect,
  IMPLEMENTER_WORKER_ITEM_ID,
  R5_WORKSPACE_ID,
  runActivationWorkerOnce,
  runPlanningWorkerOnce,
  runSchedulerOnce,
  seedR4Workspace,
  test,
  WORKSPACE_ID,
} from "./fixtures";

type JsonObject = Record<string, unknown>;

const REVISION_WORKER_TRANSITION_ID = "a6000000-0000-0000-0000-000000000001";
const OLD_SESSION_ID = "70000000-0000-0000-0000-000000000099";
const SUPERSEDED_INPUT_ID = "d0000000-0000-0000-0000-000000000099";

test.describe("R6.1.3 plan revision control room", () => {
  test.describe.configure({ mode: "serial", timeout: 90_000 });

  test.beforeEach(({ seedR5 }) => {
    seedR5("proposal");
  });

  test("surfaces the concrete revision, reviews DAG changes, confirms convergence, and closes the nested route", async ({ page, request }) => {
    const { rootTaskId } = await createInitialTask(page, request);
    const profileId = await registerRevisionProfile(request);
    const task = await getJson(request, `/api/v1/tasks/${rootTaskId}?workspace_id=${R5_WORKSPACE_ID}`);
    const graph = await getJson(
      request,
      `/api/v1/tasks/${rootTaskId}/graph?workspace_id=${R5_WORKSPACE_ID}`,
    );
    const overview = objectField(graph, "overview");
    const planHead = objectField(overview, "plan_head");
    const revision = await postCommand(request, {
      command_id: randomUUID(),
      workspace_id: R5_WORKSPACE_ID,
      command_type: "SubmitPlanRevision",
      target_type: "task",
      actor: { type: "local", id: "aitejiu" },
      target_id: rootTaskId,
      expected_version: numberField(task, "version"),
      idempotency_key: `e2e-submit-revision-${randomUUID()}`,
      correlation_id: randomUUID(),
      payload: {
        root_task_id: rootTaskId,
        expected_base_snapshot_id: stringField(planHead, "active_snapshot_id"),
        expected_base_generation: numberField(planHead, "active_generation"),
        expected_base_content_hash: stringField(planHead, "active_snapshot_hash"),
        objective: "保持已完成 WorkerItem 的上下文并增加可验证的并行分支",
        constraints: ["复用已有 WorkerItem 身份", "只影响受变更分支"],
        acceptance_criteria: ["新的执行架构可审阅", "确认后能看到计划收敛"],
        planning_profile_id: profileId,
        planning_profile_version: 1,
      },
    });
    const planningRequestId = stringField(objectField(revision, "data"), "planning_request_id");

    expect(runPlanningWorkerOnce().succeeded).toBe(1);
    await page.goto(`/workspaces/${R5_WORKSPACE_ID}/tasks/${rootTaskId}`);
    const attention = page.getByRole("region", { name: "计划调整待处理" });
    await expect(attention).toBeVisible();
    await expect(attention).toContainText("新的计划方案已就绪");
    await expect(attention).toContainText("Planner generated task");
    await expectNoHorizontalOverflow(page);

    await page.goto(`/workspaces/${R5_WORKSPACE_ID}/planning/${planningRequestId}`);
    await expect(page.getByRole("button", { name: "要求修改" })).toBeVisible();
    await page.getByRole("button", { name: "要求修改" }).click();
    await page.getByLabel("修改意见").fill("保留已完成 WorkerItem 的上下文，只调整新增分支。");
    await page.getByRole("button", { name: "提交修改意见" }).click();
    await expect(page.getByRole("button", { name: "要求修改" })).toHaveCount(0);

    expect(runPlanningWorkerOnce().succeeded).toBe(1);
    await page.goto(`/workspaces/${R5_WORKSPACE_ID}/tasks/${rootTaskId}`);
    await expect(page.getByRole("region", { name: "计划调整待处理" })).toBeVisible();
    await page.getByRole("link", { name: "查看计划调整" }).click();
    await expect(page).toHaveURL(new RegExp(`/workspaces/${R5_WORKSPACE_ID}/tasks/${rootTaskId}/revisions/${planningRequestId}$`));
    await expect(page.getByRole("heading", { name: "需求理解与风险" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "任务层级变化" })).toBeVisible();
    await page.getByRole("button", { name: "查看依赖变化" }).click();
    await expect(page.getByTestId("revision-dag-change-canvas")).toBeVisible();
    await expectNoHorizontalOverflow(page);

    await page.getByRole("button", { name: "确认并启用新计划" }).click();
    await expect(page.getByRole("status").filter({ hasText: "新计划已提交" })).toBeVisible();
    expect(runActivationWorkerOnce().status).toBe("ok");
    expect(runSchedulerOnce().status).toBe("ok");

    await page.goto(`/workspaces/${R5_WORKSPACE_ID}/tasks/${rootTaskId}`);
    await expect(page.getByRole("region", { name: "计划收敛" })).toBeVisible();
    await expect(page.getByText(/GEN \d+ → GEN \d+/).first()).toBeVisible();

    await page.goto(`/workspaces/${R5_WORKSPACE_ID}/tasks/${rootTaskId}/revisions/${planningRequestId}`);
    await expect(page.getByText("新计划已启用", { exact: true })).toBeVisible();
    const activeTab = page.getByRole("navigation", { name: "已打开的任务" }).locator(".workspace-tab.is-active");
    await activeTab.getByRole("button", { name: /^关闭/ }).click();
    await expect(page).not.toHaveURL(/\/revisions\//);
    await expect(page.getByRole("heading", { name: "规划请求" })).toBeVisible();
  });

  test("keeps unknown recovery and superseded input resend scoped to the WorkerItem", async ({ page }) => {
    seedR4Workspace();
    const commandBodies: JsonObject[] = [];
    let currentSessionId = "";

    await page.route(`**/api/v1/workspaces/${WORKSPACE_ID}/worker-items/${IMPLEMENTER_WORKER_ITEM_ID}/workspace`, async (route) => {
      const response = await route.fetch();
      const body = await response.json() as JsonObject;
      const context = objectField(body, "intervention_context");
      const runtime = objectField(body, "current_runtime");
      currentSessionId = stringField(context, "session_id");
      body.plan_sync = {
        worker_transition_id: REVISION_WORKER_TRANSITION_ID,
        plan_transition_id: "a5000000-0000-0000-0000-000000000001",
        action: "continue",
        status: "blocked_unknown",
        writer_state: "unknown",
        source_generation: 1,
        target_generation: 2,
        current_context_version: numberField(context, "context_version"),
        acknowledged_context_version: numberField(context, "context_version"),
        target_context_version: numberField(context, "context_version") + 1,
        patch_id: null,
        patch_acknowledged: false,
        source_execution_id: stringField(runtime, "execution_id"),
        source_session_id: OLD_SESSION_ID,
        target_execution_id: stringField(runtime, "execution_id"),
        target_session_id: currentSessionId,
        continuation_execution_id: null,
        continuation_session_id: null,
        queued_input_ids: [],
        superseded_input_ids: [SUPERSEDED_INPUT_ID],
        failure_code: "adapter_result_unknown",
        started_at: "2026-08-23T07:50:00Z",
        completed_at: null,
        trace_href: null,
      };
      await route.fulfill({ response, body: JSON.stringify(body) });
    });
    await page.route(`**/api/v1/workspaces/${WORKSPACE_ID}/worker-items/${IMPLEMENTER_WORKER_ITEM_ID}/activity*`, async (route) => {
      const response = await route.fetch();
      const body = await response.json() as JsonObject;
      const items = Array.isArray(body.items) ? body.items : [];
      body.items = [
        {
          activity_id: SUPERSEDED_INPUT_ID,
          source_type: "execution_input",
          source_id: SUPERSEDED_INPUT_ID,
          execution_id: "60000000-0000-0000-0000-000000000099",
          session_id: OLD_SESSION_ID,
          occurred_at: "2026-08-23T07:45:00Z",
          kind: "user_input",
          title: "用户介入",
          summary: "旧 Session 上的输入",
          correlation_id: SUPERSEDED_INPUT_ID,
          body: "保留这段旧输入并重新验证",
          delivery_status: "superseded",
          artifact_ids: [],
          trace_descriptor: null,
        },
        ...items,
      ];
      await route.fulfill({ response, body: JSON.stringify(body) });
    });
    await page.route(`**/api/v1/workspaces/${WORKSPACE_ID}/worker-transitions/${REVISION_WORKER_TRANSITION_ID}`, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ worker_transition_id: REVISION_WORKER_TRANSITION_ID, version: 3 }),
      });
    });
    await page.route("**/api/v1/commands", async (route) => {
      if (route.request().method() !== "POST") {
        await route.continue();
        return;
      }
      const command = route.request().postDataJSON() as JsonObject;
      commandBodies.push(command);
      await route.fulfill({
        status: 201,
        contentType: "application/json",
        body: JSON.stringify({
          command_id: command.command_id,
          aggregate_id: command.target_id,
          aggregate_version: 4,
          event_ids: [],
          data: {},
        }),
      });
    });

    await page.goto(`/workspaces/${WORKSPACE_ID}/worker-items/${IMPLEMENTER_WORKER_ITEM_ID}`);
    await expect(page.locator("#worker-item-title")).toHaveText("实现者");
    await expect(page.getByRole("button", { name: "恢复计划同步" })).toBeVisible();
    await expect(
      page.getByRole("region", { name: "已失效的用户介入" }).getByText("保留这段旧输入并重新验证", { exact: true }),
    ).toBeVisible();
    await expectNoHorizontalOverflow(page);

    const traceButton = page.getByRole("button", { name: "查看 Agent Trace" }).first();
    await expect(traceButton).toBeVisible();
    await traceButton.click();
    const traceDialog = page.getByRole("dialog", { name: "Agent Trace" });
    await expect(traceDialog).toBeVisible();
    await traceDialog.getByRole("button", { name: "关闭 Agent Trace" }).click();
    await expect(traceDialog).toHaveCount(0);

    await page.getByRole("button", { name: "恢复计划同步" }).click();
    const recoveryDialog = page.getByRole("dialog", { name: "恢复计划同步" });
    await recoveryDialog.getByLabel("恢复原因").fill("已核对外部执行记录，确认需要重新读取状态。");
    await recoveryDialog.getByLabel("证据引用").fill("event:recovery-1");
    await recoveryDialog.getByRole("button", { name: "提交恢复策略" }).click();
    await expect(page.getByText("恢复请求已记录", { exact: false })).toBeVisible();

    const recoveryCommand = commandBodies.find((command) => command.command_type === "RecoverWorkerTransition");
    expect(recoveryCommand).toMatchObject({
      target_type: "worker_transition",
      target_id: REVISION_WORKER_TRANSITION_ID,
      expected_version: 3,
      payload: {
        strategy: "reconcile_adapter",
        evidence_refs: ["event:recovery-1"],
      },
    });

    await page.getByRole("button", { name: "重新发送" }).click();
    await expect(page.getByText("已接受", { exact: true })).toBeVisible();
    const resendCommand = commandBodies.find((command) => command.command_type === "SendExecutionInput");
    expect(resendCommand).toMatchObject({
      target_type: "execution_session",
      target_id: currentSessionId,
      payload: { text: "保留这段旧输入并重新验证" },
    });
    expect(resendCommand?.target_id).not.toBe(OLD_SESSION_ID);
  });
});

async function createInitialTask(
  page: Page,
  request: APIRequestContext,
): Promise<{ rootTaskId: string }> {
  await page.goto(`/workspaces/${R5_WORKSPACE_ID}/tasks`);
  await page.getByRole("region", { name: "任务" }).getByRole("link", { name: "新建任务" }).click();
  await page.getByLabel("需求目标").fill("为控制面增加可恢复的规划闭环");
  await page.getByRole("textbox", { name: "验收条件" }).first().fill("用户确认前不创建任务，确认后可看到 WorkerItem 状态");
  await page.getByRole("button", { name: "提交需求" }).click();
  await expect(page).toHaveURL(new RegExp(`/workspaces/${R5_WORKSPACE_ID}/planning/[0-9a-f-]{36}$`));
  expect((await getJson(request, `/api/v1/workspaces/${R5_WORKSPACE_ID}/task-overviews`)) as unknown[]).toEqual([]);

  expect(runPlanningWorkerOnce().succeeded).toBe(1);
  await page.reload();
  await expect(page.locator(".planning-status").filter({ hasText: "方案待确认" })).toBeVisible();
  await page.getByRole("button", { name: "确认并创建任务" }).click();
  await expect(page).toHaveURL(new RegExp(`/workspaces/${R5_WORKSPACE_ID}/tasks/[^/]+$`));
  const rootTaskId = new URL(page.url()).pathname.split("/").at(-1);
  if (rootTaskId === undefined || rootTaskId.length === 0) {
    throw new Error("initial task route did not contain a root task id");
  }
  return { rootTaskId };
}

async function expectNoHorizontalOverflow(page: Page): Promise<void> {
  const dimensions = await page.evaluate(() => ({
    clientWidth: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
  }));
  expect(dimensions.scrollWidth).toBeLessThanOrEqual(dimensions.clientWidth + 1);
}

async function registerRevisionProfile(request: APIRequestContext): Promise<string> {
  const profiles = await getJson(request, `/api/v1/workspaces/${R5_WORKSPACE_ID}/planning-profiles`);
  const baseProfile = objectArrayField(profiles, "items")[0];
  if (baseProfile === undefined) throw new Error("seed did not create a planning profile");
  const result = await postCommand(request, {
    command_id: randomUUID(),
    workspace_id: R5_WORKSPACE_ID,
    command_type: "RegisterPlanningProfile",
    target_type: "workspace",
    actor: { type: "local", id: "aitejiu" },
    target_id: null,
    expected_version: null,
    idempotency_key: `e2e-register-revision-profile-${randomUUID()}`,
    correlation_id: randomUUID(),
    payload: {
      profile_key: `r6-e2e-revision-${randomUUID().slice(0, 8)}`,
      version: 1,
      display_name: "R6.1.3 Revision E2E",
      planner_module_instance_id: stringField(baseProfile, "planner_module_instance_id"),
      planner_module_config_version: numberField(baseProfile, "planner_module_config_version"),
      planner_interface_version: stringField(baseProfile, "planner_interface_version"),
      prompt_policy_version: "r6.1.3-e2e",
      system_policy: {},
      proposal_schema_version: 3,
      allowed_capabilities: ["code"],
      adapter_preferences: ["fake"],
      adapter_forbidden: [],
      budget: {
        max_context_bytes: 64_000,
        max_tokens: 100_000,
        max_cost_microunits: 1_000_000_000,
        max_runtime_seconds: 3_600,
        max_revisions: 20,
        max_nodes: 500,
        max_concurrency: 4,
      },
      retry_policy: {
        max_retries: 1,
        backoff_seconds: [1],
        unknown_outcome_policy: "block",
        invalid_output_policy: "retry",
      },
      default_priority: 50,
      execution_max_attempts: 2,
      execution_budget_level: "standard",
      user_selectable: true,
      planner_secret_refs: [],
    },
  });
  return stringField(result, "aggregate_id");
}

async function postCommand(request: APIRequestContext, command: JsonObject): Promise<JsonObject> {
  const response = await request.post("/api/v1/commands", { data: command });
  expect(response.ok(), await response.text()).toBeTruthy();
  return await response.json() as JsonObject;
}

async function getJson(request: APIRequestContext, path: string): Promise<unknown> {
  const response = await request.get(path);
  expect(response.ok(), await response.text()).toBeTruthy();
  return response.json();
}

function objectField(value: unknown, key: string): JsonObject {
  if (typeof value === "object" && value !== null && !Array.isArray(value)) {
    const field = (value as JsonObject)[key];
    if (typeof field === "object" && field !== null && !Array.isArray(field)) return field as JsonObject;
  }
  throw new Error(`expected object field ${key}`);
}

function objectArrayField(value: unknown, key: string): JsonObject[] {
  if (typeof value === "object" && value !== null && !Array.isArray(value)) {
    const field = (value as JsonObject)[key];
    if (Array.isArray(field)) return field.filter(isObject);
  }
  throw new Error(`expected object array field ${key}`);
}

function isObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function stringField(value: JsonObject, key: string): string {
  const field = value[key];
  if (typeof field === "string" && field.length > 0) return field;
  throw new Error(`expected string field ${key}`);
}

function numberField(value: JsonObject, key: string): number {
  const field = value[key];
  if (typeof field === "number") return field;
  throw new Error(`expected number field ${key}`);
}
