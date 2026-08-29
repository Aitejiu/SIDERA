import type { APIRequestContext, Page } from "@playwright/test";
import { randomUUID } from "node:crypto";

import {
  expect,
  R6_1_WORKSPACE_ID,
  runActivationWorkerOnce,
  runPlanTransitionWorkerOnce,
  runPlanningWorkerOnce,
  runSchedulerOnce,
  test,
} from "./fixtures";

type JsonObject = Record<string, unknown>;

const WORKSPACE_ID = R6_1_WORKSPACE_ID;
const ROOT_TITLE = "Running revision control room";
const SUBTASK_KEY = "subtask-b";

test.describe("R6.1.4 real request-to-confirmation control room", () => {
  test.describe.configure({ mode: "serial", timeout: 120_000 });

  test.beforeEach(({ seedR6Revision }) => {
    seedR6Revision();
  });

  test("walks Revision review, convergence, Worker recovery, and exact Trace", async ({ page, request }, testInfo) => {
    const rootTaskId = await findRootTaskId(request);
    const planningRequestId = await submitRevision(request, rootTaskId);

    expect(runPlanningWorkerOnce().succeeded).toBe(1);
    await page.goto(`/workspaces/${WORKSPACE_ID}/tasks/${rootTaskId}`);
    const attention = page.getByRole("region", { name: "计划调整待处理" });
    await expect(attention).toBeVisible();
    await expect(attention).toContainText("新的计划方案已就绪");
    await expect(attention).toContainText("Subtask B");
    await expectNoHorizontalOverflow(page);
    await page.screenshot({
      path: testInfo.outputPath(`r6-1-root-review-${testInfo.project.name}.png`),
      fullPage: true,
    });

    await attention.getByRole("link", { name: "查看计划调整" }).click();
    await expect(page).toHaveURL(
      new RegExp(`/workspaces/${WORKSPACE_ID}/tasks/${rootTaskId}/revisions/${planningRequestId}$`),
    );
    await expect(page.getByRole("heading", { name: "需求理解与风险" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "任务层级变化" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "依赖变化" })).toBeVisible();
    await expect(page.getByText("这次没有依赖变化，不需要展开 DAG。", { exact: true })).toBeVisible();
    await expectNoHorizontalOverflow(page);

    await page.getByRole("button", { name: "确认并启用新计划" }).click();
    await expect(page.getByRole("status").filter({ hasText: "新计划已提交" })).toBeVisible();

    expect(runActivationWorkerOnce().status).toBe("ok");
    expect(runSchedulerOnce().status).toBe("ok");
    expect(runPlanTransitionWorkerOnce().status).toBe("ok");

    await page.goto(`/workspaces/${WORKSPACE_ID}/tasks/${rootTaskId}`);
    const convergence = page.getByRole("region", { name: "计划收敛" });
    await expect(convergence).toBeVisible();
    await expect(convergence).toContainText("GEN 3 → GEN 4");
    await page.getByRole("tab", { name: "DAG" }).click();
    await expect(page.getByRole("region", { name: "任务 DAG" })).toBeVisible();
    await expect(page.getByRole("group", { name: "依赖关系" })).toContainText("subtask-a");
    await expectNoHorizontalOverflow(page);
    await page.getByRole("tab", { name: "看板" }).click();
    await expect(page.getByRole("button", { name: "打开子任务：Subtask B" })).toBeVisible();
    await expectNoHorizontalOverflow(page);

    await page.getByRole("button", { name: "打开子任务：Subtask B" }).click();
    await expect(page).toHaveURL(
      new RegExp(`/workspaces/${WORKSPACE_ID}/tasks/${rootTaskId}/subtasks/${SUBTASK_KEY}$`),
    );
    await expect(page.locator("#subtask-title")).toHaveText("Subtask B");
    await expect(page.getByRole("heading", { name: "Worker 看板" })).toBeVisible();
    await expect(page.getByRole("button", { name: "打开 Worker：Worker B" })).toBeVisible();

    const workerItemId = await findWorkerItemId(request, rootTaskId);
    const workerWorkspace = await getJson(
      request,
      `/api/v1/workspaces/${WORKSPACE_ID}/worker-items/${workerItemId}/workspace`,
    );
    const runtime = objectField(workerWorkspace, "current_runtime");
    const context = objectField(workerWorkspace, "intervention_context");
    const executionId = stringField(runtime, "execution_id");
    const sessionId = stringField(runtime, "session_id");
    const workerId = stringField(runtime, "worker_id");
    const contextVersion = numberField(context, "context_version");
    const workerTransitionId = "a6000000-0000-0000-0000-000000000101";
    const transitionVersion = 3;
    const injectedActivityId = randomUUID();

    await installWorkerRecoveryRoutes({
      page,
      rootTaskId,
      workerItemId,
      executionId,
      sessionId,
      workerId,
      contextVersion,
      workerTransitionId,
      injectedActivityId,
    });

    await page.getByRole("button", { name: "打开 Worker：Worker B" }).click();
    await expect(page).toHaveURL(
      new RegExp(`/workspaces/${WORKSPACE_ID}/worker-items/${workerItemId}$`),
    );
    await expect(page.locator("#worker-item-title")).toHaveText("Worker B");
    await expect(page.getByRole("button", { name: "恢复计划同步" })).toBeVisible();
    await expect(page.locator(".worker-item-page__breadcrumb")).toContainText("Subtask B");
    await expectNoHorizontalOverflow(page);

    const traceButton = page.getByRole("button", { name: "查看 Agent Trace" }).first();
    await expect(traceButton).toBeVisible();
    await traceButton.click();
    const traceDialog = page.getByRole("dialog", { name: "Agent Trace" });
    await expect(traceDialog).toBeVisible();
    await expect(traceDialog).toContainText("检查点已保存");
    await expect(traceDialog).toContainText(executionId);
    await expect(traceDialog).toContainText(sessionId);
    await traceDialog.getByRole("button", { name: "关闭 Agent Trace" }).click();
    await expect(traceDialog).toHaveCount(0);

    await page.getByRole("button", { name: "恢复计划同步" }).click();
    const recoveryDialog = page.getByRole("dialog", { name: "恢复计划同步" });
    await recoveryDialog.getByLabel("恢复原因").fill("已核对外部执行记录，确认需要重新读取状态。");
    await recoveryDialog.getByLabel("证据引用").fill("event:r6-1-recovery");
    await recoveryDialog.getByRole("button", { name: "提交恢复策略" }).click();
    await expect(page.getByText("恢复请求已记录", { exact: false })).toBeVisible();
    const recoveryCommand = commandBodies.find(
      (command) => command.command_type === "RecoverWorkerTransition",
    );
    expect(recoveryCommand).toBeDefined();
    expect(recoveryCommand).toMatchObject({
      command_type: "RecoverWorkerTransition",
      target_type: "worker_transition",
      target_id: workerTransitionId,
      expected_version: transitionVersion,
      payload: {
        strategy: "reconcile_adapter",
        evidence_refs: ["event:r6-1-recovery"],
      },
    });

    await page.screenshot({
      path: testInfo.outputPath(`r6-1-worker-trace-${testInfo.project.name}.png`),
      fullPage: true,
    });
    await expectNoHorizontalOverflow(page);
  });
});

const commandBodies: JsonObject[] = [];

async function submitRevision(
  request: APIRequestContext,
  rootTaskId: string,
): Promise<string> {
  const task = await getJson(request, `/api/v1/tasks/${rootTaskId}?workspace_id=${WORKSPACE_ID}`);
  const graph = await getJson(request, `/api/v1/tasks/${rootTaskId}/graph?workspace_id=${WORKSPACE_ID}`);
  const overview = objectField(graph, "overview");
  const planHead = objectField(overview, "plan_head");
  const profiles = await getJson(request, `/api/v1/workspaces/${WORKSPACE_ID}/planning-profiles`);
  const profile = objectArrayField(profiles, "items")[0];
  if (profile === undefined) throw new Error("R6.1 seed did not create a planning profile");

  const result = await postCommand(request, {
    command_id: randomUUID(),
    workspace_id: WORKSPACE_ID,
    command_type: "SubmitPlanRevision",
    target_type: "task",
    actor: { type: "local", id: "aitejiu" },
    target_id: rootTaskId,
    expected_version: numberField(task, "version"),
    idempotency_key: `e2e-r6-1-submit-revision-${randomUUID()}`,
    correlation_id: randomUUID(),
    payload: {
      root_task_id: rootTaskId,
      expected_base_snapshot_id: stringField(planHead, "active_snapshot_id"),
      expected_base_generation: numberField(planHead, "active_generation"),
      expected_base_content_hash: stringField(planHead, "active_snapshot_hash"),
      objective: "Keep completed context and adjust only the blocked branch.",
      constraints: ["Reuse existing WorkerItem identities where safe."],
      acceptance_criteria: ["The new execution graph is reviewable before confirmation."],
      planning_profile_id: stringField(profile, "id"),
      planning_profile_version: numberField(profile, "version"),
    },
  });
  return stringField(objectField(result, "data"), "planning_request_id");
}

async function findRootTaskId(request: APIRequestContext): Promise<string> {
  const overviews = await getJson(request, `/api/v1/workspaces/${WORKSPACE_ID}/task-overviews`);
  if (!Array.isArray(overviews)) throw new Error("task-overviews did not return an array");
  const root = overviews.find((item) => isObject(item) && item.title === ROOT_TITLE);
  if (!isObject(root)) throw new Error(`could not find seeded root task ${ROOT_TITLE}`);
  return stringField(root, "root_task_id");
}

async function findWorkerItemId(
  request: APIRequestContext,
  rootTaskId: string,
): Promise<string> {
  const workspace = await getJson(
    request,
    `/api/v1/tasks/${rootTaskId}/subtasks/${SUBTASK_KEY}?workspace_id=${WORKSPACE_ID}`,
  );
  const workers = objectArrayField(workspace, "worker_items");
  const worker = workers.find((item) => item.title === "Worker B");
  if (worker === undefined) throw new Error("R6.1 seed did not expose Worker B");
  return stringField(worker, "worker_item_id");
}

async function installWorkerRecoveryRoutes(input: {
  page: Page;
  rootTaskId: string;
  workerItemId: string;
  executionId: string;
  sessionId: string;
  workerId: string;
  contextVersion: number;
  workerTransitionId: string;
  injectedActivityId: string;
}): Promise<void> {
  const {
    page,
    rootTaskId,
    workerItemId,
    executionId,
    sessionId,
    workerId,
    contextVersion,
    workerTransitionId,
    injectedActivityId,
  } = input;
  commandBodies.length = 0;

  await page.route(
    `**/api/v1/workspaces/${WORKSPACE_ID}/worker-items/${workerItemId}/workspace`,
    async (route) => {
      const response = await route.fetch();
      const body = await response.json() as JsonObject;
      body.plan_sync = {
        worker_transition_id: workerTransitionId,
        plan_transition_id: "a5000000-0000-0000-0000-000000000101",
        action: "continue",
        status: "blocked_unknown",
        writer_state: "unknown",
        source_generation: 3,
        target_generation: 4,
        current_context_version: contextVersion,
        acknowledged_context_version: contextVersion,
        target_context_version: contextVersion + 1,
        patch_id: null,
        patch_acknowledged: false,
        source_execution_id: executionId,
        source_session_id: "70000000-0000-0000-0000-000000000101",
        target_execution_id: executionId,
        target_session_id: sessionId,
        continuation_execution_id: null,
        continuation_session_id: null,
        queued_input_ids: [],
        superseded_input_ids: [],
        failure_code: "adapter_result_unknown",
        started_at: "2026-08-29T12:00:00Z",
        completed_at: null,
        trace_href: null,
      };
      await route.fulfill({ response, body: JSON.stringify(body) });
    },
  );
  await page.route(
    `**/api/v1/workspaces/${WORKSPACE_ID}/worker-items/${workerItemId}/activity*`,
    async (route) => {
      const response = await route.fetch();
      const body = await response.json() as JsonObject;
      const items = Array.isArray(body.items) ? body.items : [];
      body.items = [
        {
          activity_id: injectedActivityId,
          source_type: "execution_event",
          source_id: injectedActivityId,
          execution_id: executionId,
          session_id: sessionId,
          occurred_at: "2026-08-29T12:01:00Z",
          kind: "checkpoint",
          title: "检查点已保存",
          summary: "Agent 已保存可复原的执行检查点",
          correlation_id: injectedActivityId,
          body: "当前执行上下文已记录，可从 Trace 复原。",
          delivery_status: null,
          artifact_ids: [],
          trace_descriptor: {
            root_task_id: rootTaskId,
            execution_id: executionId,
            session_id: sessionId,
            route: `/api/v1/tasks/${rootTaskId}/executions/${executionId}/sessions/${sessionId}/trace`,
            raw_ref: injectedActivityId,
          },
        },
        ...items,
      ];
      await route.fulfill({ response, body: JSON.stringify(body) });
    },
  );
  await page.route(
    `**/api/v1/workspaces/${WORKSPACE_ID}/worker-transitions/${workerTransitionId}`,
    async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ worker_transition_id: workerTransitionId, version: 3 }),
      });
    },
  );
  await page.route(
    `**/api/v1/tasks/${rootTaskId}/executions/${executionId}/sessions/${sessionId}/trace*`,
    async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          schema_version: 1,
          execution_id: executionId,
          session_id: sessionId,
          worker_id: workerId,
          status: "running",
          current_action: "保存执行检查点",
          plan_generation: 3,
          checkpoint_sequence: 2,
          freshness_at: "2026-08-29T12:01:00Z",
          last_change_cursor: 72,
          timing: { input_ms: 12, model_ms: 480, tools_ms: 160, total_ms: 652 },
          events: [
            {
              schema_version: 1,
              sequence: 1,
              workspace_id: WORKSPACE_ID,
              root_task_id: rootTaskId,
              execution_id: executionId,
              session_id: sessionId,
              plan_snapshot_id: null,
              work_item_key: "worker-b",
              category: "checkpoint",
              status: "completed",
              title: "检查点已保存",
              summary: "Agent 已保存可复原的执行检查点",
              occurred_at: "2026-08-29T12:01:00Z",
              duration_ms: 652,
              provider_extensions: {},
              provider_payload_ref: null,
              artifact_refs: [],
              usage: null,
            },
          ],
          previous_before_sequence: null,
          context_branch: null,
        }),
      });
    },
  );
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

async function expectNoHorizontalOverflow(page: Page): Promise<void> {
  const dimensions = await page.evaluate(() => ({
    clientWidth: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
  }));
  expect(dimensions.scrollWidth).toBeLessThanOrEqual(dimensions.clientWidth + 1);
}

function objectField(value: unknown, key: string): JsonObject {
  if (isObject(value) && isObject(value[key])) return value[key];
  throw new Error(`expected object field ${key}`);
}

function objectArrayField(value: unknown, key: string): JsonObject[] {
  if (isObject(value) && Array.isArray(value[key])) {
    return value[key].filter(isObject);
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
