import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import type { Page } from "@playwright/test";

import type { InterviewDriver } from "./driver";
import { expect, test } from "./fixtures";

test.use({ demoScenario: "controls" });

test("Main Agent exposes explicit interruption and unsupported/offline boundaries", async ({
  interview,
  page,
}) => {
  test.info().annotations.push({ type: "action", description: "F02-03" });
  test.info().annotations.push({ type: "action", description: "F02-05" });

  await interview.openChat();
  const threadId = page.url().match(/\/chat\/([^/?]+)/)?.[1];
  expect(threadId).toBeDefined();

  const started = await page.request.post(
    `${interview.manifest.endpoints.main_agent}/main-agent/v1/threads/${threadId}/turns`,
    { data: { input: "为项目增加 CSV 批量导入功能" } },
  );
  expect(started.status()).toBe(202);
  const turn = await started.json() as { id?: string; status?: string };
  expect(turn.id).toBeDefined();
  expect(["queued", "in_progress", "completed"]).toContain(turn.status);

  const interrupted = await page.request.post(
    `${interview.manifest.endpoints.main_agent}/main-agent/v1/threads/${threadId}/turns/${turn.id}/interrupt`,
  );
  expect(interrupted.ok()).toBeTruthy();
  const interruptedPayload = await interrupted.json() as { status?: string };
  expect(interruptedPayload.status).toBe("interrupted");

  await page.getByRole("textbox", { name: "给 Main Agent 的消息", exact: true }).fill(
    "请帮我预测明天的股票价格",
  );
  await page.getByRole("button", { name: "发送消息", exact: true }).click();
  await expect(page.getByText("这个输入不会触发控制面命令", { exact: false })).toBeVisible({
    timeout: 30_000,
  });

  await page.route("**/main-agent/v1/health", (route) => route.abort());
  await page.reload();
  await expect(page.getByRole("status", { name: "历史只读提示" })).toBeVisible();
  await expect(page.getByText("Gateway 暂不可用；维护模式下不能发送或中断新的回合。", { exact: true })).toBeVisible();

  await page.goto(`${interview.manifest.endpoints.web}/workspaces/${interview.manifest.workspace_id}/chat`);
  await expect(page.getByRole("alert").filter({ hasText: "Main Agent 暂不可用" })).toBeVisible();
  await expect(page.getByText("任务控制面仍可继续查看", { exact: false })).toBeVisible();
});

test("planning form creates a request before execution and can cancel it", async ({
  interview,
  page,
}) => {
  test.info().annotations.push({ type: "action", description: "F03-01" });
  test.info().annotations.push({ type: "action", description: "F03-06" });

  await page.goto(`${interview.manifest.endpoints.web}/workspaces/${interview.manifest.workspace_id}/planning/new`);
  await page.getByLabel("需求目标", { exact: true }).fill("演示页面提交后可以取消的规划请求");
  await page.getByLabel("验收条件", { exact: true }).first().fill("提交后能看到规划请求状态");
  await page.getByRole("button", { name: "提交需求", exact: true }).click();
  await page.waitForURL(/\/planning\/[0-9a-f-]{36}$/i);
  await expect(page.getByRole("heading", { name: "规划请求", exact: true })).toBeVisible();

  const requestId = page.url().match(/\/planning\/([^/?]+)$/)?.[1];
  expect(requestId).toBeDefined();
  const requestBefore = await getJson(page, interview, `/api/v1/workspaces/${interview.manifest.workspace_id}/planning-requests/${requestId}`);
  expect(["submitted", "planning", "waiting_for_user", "proposal_ready"]).toContain(requestBefore.status);
  expect(await interview.getFacts()).toMatchObject({ rootTaskId: null });

  const cancel = page.getByRole("button", { name: "取消规划", exact: true }).first();
  await expect(cancel).toBeVisible({ timeout: 30_000 });
  await cancel.click();
  const dialog = page.getByRole("dialog", { name: "确认取消规划", exact: true });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "确认取消", exact: true }).click();
  await expect(page.locator(".planning-status--cancelled")).toBeVisible({ timeout: 30_000 });
  const requestAfter = await getJson(page, interview, `/api/v1/workspaces/${interview.manifest.workspace_id}/planning-requests/${requestId}`);
  expect(requestAfter.status).toBe("cancelled");
  expect(await interview.getFacts()).toMatchObject({ rootTaskId: null });
});

test("notification settings expose server readiness and browser permission state", async ({
  interview,
  page,
}) => {
  test.info().annotations.push({ type: "action", description: "F17-03" });

  await page.context().grantPermissions(["notifications"], {
    origin: interview.manifest.endpoints.web,
  });
  await page.goto(`${interview.manifest.endpoints.web}/workspaces/${interview.manifest.workspace_id}/settings`);
  await expect(page.getByRole("heading", { name: "通知设置", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "渠道模块", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "运行就绪度", exact: true })).toBeVisible();
  const browserNotification = page.getByRole("region", { name: "浏览器通知", exact: true });
  await expect(browserNotification).toBeVisible();
  const toggle = browserNotification.getByRole("button", { name: "开启", exact: true });
  if (await toggle.isEnabled()) {
    await toggle.click();
    await expect(browserNotification).toContainText("浏览器通知已开启");
  } else {
    await expect(browserNotification).toContainText(/当前浏览器不支持|浏览器权限已拒绝/);
  }
});

test("task tabs keep inactive close, draft isolation, and a valid last entry", async ({
  interview,
  page,
}) => {
  test.info().annotations.push({ type: "action", description: "F01-04" });
  test.info().annotations.push({ type: "action", description: "F01-06" });

  await establishControlsMainline(interview, page);
  await interview.openWorker("validate");
  const validation = await interview.getFacts();
  const validationId = validation.workerItems.validate?.id;
  expect(validationId).toBeDefined();
  const validationInput = page.getByRole("textbox", { name: "介入这个 Worker", exact: true });
  await validationInput.fill("只保留校验 Worker 的草稿");

  await interview.openWorker("report");
  const report = await interview.getFacts();
  const reportId = report.workerItems.report?.id;
  expect(reportId).toBeDefined();
  const reportInput = page.getByRole("textbox", { name: "介入这个 Worker", exact: true });
  await reportInput.fill("只保留报告 Worker 的草稿");

  await interview.openWorker("review");
  const tabs = page.getByRole("navigation", { name: "已打开的任务", exact: true });
  const reviewId = await workerId(interview, "review-report");
  const reviewTab = tabs.locator(`a[href*="/worker-items/${reviewId}"]`).locator("..");
  await expect(reviewTab).toHaveClass(/is-active/);
  const reportTab = tabs.locator(`a[href*="/worker-items/${reportId}"]`).locator("..");
  await expect(reportTab).toBeVisible();
  await reportTab.getByRole("button", { name: /^关闭/ }).click();
  await expect(page).toHaveURL(/\/worker-items\/[0-9a-f-]{36}/i);
  await expect(page).toHaveURL(new RegExp(`/worker-items/${reviewId}$`));

  const validationTab = tabs.locator(`a[href*="/worker-items/${validationId}"]`);
  await validationTab.click();
  await expect(validationInput).toHaveValue("只保留校验 Worker 的草稿");
  await tabs.locator(".workspace-tab.is-active").getByRole("button", { name: /^关闭/ }).click();
  await expect(page).toHaveURL(new RegExp(`/tasks/${(await interview.getFacts()).rootTaskId}$`));

  await interview.openWorker("validate");
  await expect(page.getByRole("textbox", { name: "介入这个 Worker", exact: true })).toHaveValue(
    "只保留校验 Worker 的草稿",
  );

  const taskIndexPath = `/workspaces/${interview.manifest.workspace_id}/tasks`;
  for (let attempts = 0; attempts < 16 && !new URL(page.url()).pathname.endsWith(taskIndexPath); attempts += 1) {
    const activeClose = tabs.locator(".workspace-tab.is-active .workspace-tab__close");
    await expect(activeClose).toBeVisible();
    await activeClose.click();
    await page.waitForTimeout(100);
  }
  await expect(page).toHaveURL(new RegExp(`${taskIndexPath}$`));
  await expect(page.locator("#workspace-title")).toBeVisible();
});

test.describe("two task tab isolation", () => {
  test.use({ demoScenario: "mainline" });

  test("two task tabs keep view position and WorkerItem drafts isolated", async ({
    interview,
    page,
  }) => {
    test.info().annotations.push({ type: "action", description: "F01-02" });

  await establishControlsMainline(interview, page);
  const firstFacts = await interview.getFacts();
  const firstRootTaskId = firstFacts.rootTaskId;
  const firstWorkerItemId = firstFacts.workerItems.validate?.id;
  expect(firstRootTaskId).not.toBeNull();
  expect(firstWorkerItemId).toBeDefined();
  await page.goto(`${interview.manifest.endpoints.web}/workspaces/${interview.manifest.workspace_id}/tasks/${firstRootTaskId}?view=dag`);
  await expect(page.getByRole("region", { name: "任务 DAG", exact: true })).toBeVisible();
  await interview.openWorker("validate");
  await page.getByRole("textbox", { name: "介入这个 Worker", exact: true }).fill("第一个任务的草稿");

  await page.goto(`${interview.manifest.endpoints.web}/workspaces/${interview.manifest.workspace_id}/planning/new`);
  await page.getByLabel("需求目标", { exact: true }).fill("第二个独立任务的演示规划");
  await page.getByLabel("验收条件", { exact: true }).first().fill("第二个任务拥有独立 WorkerItem");
  await page.getByRole("button", { name: "提交需求", exact: true }).click();
  await page.waitForURL(/\/planning\/[0-9a-f-]{36}$/i);
  const clarification = page.getByRole("textbox", { name: "澄清答复", exact: true });
  if (await clarification.count() > 0) {
    await clarification.fill("optional");
    await page.getByRole("button", { name: "提交澄清答复", exact: true }).click();
  }
  await expect(page.getByRole("button", { name: "确认并创建任务", exact: true })).toBeVisible({ timeout: 30_000 });
  await page.getByRole("button", { name: "确认并创建任务", exact: true }).click();
  await page.waitForURL(/\/tasks\/[0-9a-f-]{36}$/i);
  const secondRootTaskId = page.url().match(/\/tasks\/([0-9a-f-]{36})$/i)?.[1];
  expect(secondRootTaskId).toBeDefined();
  expect(secondRootTaskId).not.toBe(firstRootTaskId);
  await interview.releaseGate("parallel");
  await expect(page.getByRole("region", { name: "子任务状态看板", exact: true })).toBeVisible();

  const secondWorkerItemId = await findWritableWorkerItem(page, interview, secondRootTaskId);
  expect(secondWorkerItemId).toBeDefined();
  await page.goto(`${interview.manifest.endpoints.web}/workspaces/${interview.manifest.workspace_id}/worker-items/${secondWorkerItemId}`);
  const secondDraft = page.getByRole("textbox", { name: "介入这个 Worker", exact: true });
  await expect(secondDraft).toBeEnabled({ timeout: 30_000 });
  await secondDraft.fill("第二个任务的草稿");

  const tabs = page.getByRole("navigation", { name: "已打开的任务", exact: true });
  await expect(tabs.locator(`a[href*="/tasks/${firstRootTaskId}"]`)).toBeVisible();
  await expect(tabs.locator(`a[href*="/tasks/${secondRootTaskId}"]`)).toBeVisible();
  await tabs.locator(`a[href*="/tasks/${firstRootTaskId}"]`).click();
  await expect(page).toHaveURL(new RegExp(`/tasks/${firstRootTaskId}\\?view=dag$`));
  await expect(page.getByRole("region", { name: "任务 DAG", exact: true })).toBeVisible();

  await tabs.locator(`a[href*="/worker-items/${firstWorkerItemId}"]`).click();
  await expect(page.getByRole("textbox", { name: "介入这个 Worker", exact: true })).toHaveValue("第一个任务的草稿");
    await tabs.locator(`a[href*="/worker-items/${secondWorkerItemId}"]`).click();
    await expect(page.getByRole("textbox", { name: "介入这个 Worker", exact: true })).toHaveValue("第二个任务的草稿");
  });
});

test("replaying one execution input is idempotent", async ({
  interview,
  page,
}) => {
  test.info().annotations.push({ type: "action", description: "F07-02" });

  await establishControlsMainline(interview, page);
  await interview.openAttention("validate");
  const { context } = await loadValidationContext(interview, page);

  const replayKey = `interview-replay:${randomUUID()}`;
  const replayCommand = commandEnvelope({
    workspaceId: interview.manifest.workspace_id,
    commandType: "SendExecutionInput",
    targetType: "execution_session",
    targetId: String(context.session_id),
    expectedVersion: Number(context.session_version),
    idempotencyKey: `interview-command:${replayKey}`,
    payload: {
      context,
      client_idempotency_key: replayKey,
      text: "手机号允许为空，有值时才校验格式",
      attachment_refs: [],
    },
  });
  const firstReplay = await postCommand(page, interview, replayCommand);
  const secondReplay = await postCommand(page, interview, replayCommand);
  expect(firstReplay.response.ok()).toBeTruthy();
  expect(secondReplay.response.ok()).toBeTruthy();
  const replayInputs = await getJson(
    page,
    interview,
    `/api/v1/gateway/inputs?workspace_id=${interview.manifest.workspace_id}&session_id=${context.session_id}&limit=100`,
  );
  const replayMatches = asArray(replayInputs.items).filter(
    (value) => asRecord(value).client_idempotency_key === replayKey,
  );
  expect(replayMatches).toHaveLength(1);
});

test("queued execution inputs can be edited and cancelled before delivery", async ({
  interview,
  page,
}) => {
  test.info().annotations.push({ type: "action", description: "F07-04" });
  test.info().annotations.push({ type: "action", description: "F07-05" });

  await establishControlsMainline(interview, page);
  await interview.openAttention("validate");
  const { context } = await loadValidationContext(interview, page);
  await stopDemoComponent(interview, "gateway-worker");

  const updateKey = `interview-update:${randomUUID()}`;
  await postCommand(page, interview, commandEnvelope({
    workspaceId: interview.manifest.workspace_id,
    commandType: "SendExecutionInput",
    targetType: "execution_session",
    targetId: String(context.session_id),
    expectedVersion: Number(context.session_version),
    idempotencyKey: `interview-command:${updateKey}`,
    payload: {
      context,
      client_idempotency_key: updateKey,
      text: "待编辑的手机号规则",
      attachment_refs: [],
    },
  }));
  const updateSession = await getSession(page, interview, String(context.session_id));
  const updateInput = await findInput(page, interview, String(context.session_id), updateKey);
  expect(["accepted", "queued"]).toContain(updateInput.status);
  const updated = await postCommand(page, interview, commandEnvelope({
    workspaceId: interview.manifest.workspace_id,
    commandType: "UpdatePendingExecutionInput",
    targetType: "execution_session",
    targetId: String(context.session_id),
    expectedVersion: updateSession.version,
    idempotencyKey: `interview-command:update:${updateKey}`,
    payload: {
      execution_id: updateInput.id,
      client_idempotency_key: updateKey,
      text: "编辑后的手机号规则已确认",
    },
  }));
  expect(updated.response.ok(), JSON.stringify(updated.body)).toBeTruthy();
  const updatedInput = await findInput(page, interview, String(context.session_id), updateKey);
  expect(updatedInput.textLength).toBe("编辑后的手机号规则已确认".length);
  expect(updatedInput.textLength).not.toBe("待编辑的手机号规则".length);

  const cancelled = await postCommand(page, interview, commandEnvelope({
    workspaceId: interview.manifest.workspace_id,
    commandType: "CancelExecutionInput",
    targetType: "execution_session",
    targetId: String(context.session_id),
    expectedVersion: (await getSession(page, interview, String(context.session_id))).version,
    idempotencyKey: `interview-command:cancel:${updateKey}`,
    payload: {
      execution_id: updateInput.id,
      client_idempotency_key: updateKey,
    },
  }));
  expect(cancelled.response.ok()).toBeTruthy();
  expect((await findInput(page, interview, String(context.session_id), updateKey)).status).toBe("cancelled");
});

test("blocked execution input can be retried without creating a second input", async ({
  interview,
  page,
}) => {
  test.info().annotations.push({ type: "action", description: "F07-06" });

  await establishControlsMainline(interview, page);
  await interview.openAttention("validate");
  const { context } = await loadValidationContext(interview, page);
  const clientKey = `interview-blocked:${randomUUID()}`;
  await postCommand(page, interview, commandEnvelope({
    workspaceId: interview.manifest.workspace_id,
    commandType: "SendExecutionInput",
    targetType: "execution_session",
    targetId: String(context.session_id),
    expectedVersion: Number(context.session_version),
    idempotencyKey: `interview-command:${clientKey}`,
    payload: {
      context,
      client_idempotency_key: clientKey,
      text: "请处理一个当前演示不支持的输入",
      attachment_refs: [],
    },
  }));
  await expect.poll(
    async () => (await findInput(page, interview, String(context.session_id), clientKey)).status,
    { timeout: 15_000 },
  ).toBe("blocked");

  const blockedInput = await findInput(page, interview, String(context.session_id), clientKey);
  let retried: Awaited<ReturnType<typeof postCommand>> | null = null;
  for (let attempt = 0; attempt < 4; attempt += 1) {
    retried = await postCommand(page, interview, commandEnvelope({
      workspaceId: interview.manifest.workspace_id,
      commandType: "RetryBlockedExecutionInput",
      targetType: "execution_session",
      targetId: String(context.session_id),
      expectedVersion: (await getSession(page, interview, String(context.session_id))).version,
      idempotencyKey: `interview-command:retry:${clientKey}:${attempt}`,
      payload: {
        execution_id: blockedInput.id,
        client_idempotency_key: clientKey,
      },
    }));
    if (retried.response.ok()) break;
    expect(asRecord(retried.body.error).code).toBe("version_conflict");
    await page.waitForTimeout(100);
  }
  expect(retried).not.toBeNull();
  expect(retried?.response.ok(), JSON.stringify(retried?.body)).toBeTruthy();
  await expect.poll(
    async () => (await findInput(page, interview, String(context.session_id), clientKey)).status,
    { timeout: 15_000 },
  ).toBe("blocked");
  const inputs = await getJson(
    page,
    interview,
    `/api/v1/gateway/inputs?workspace_id=${interview.manifest.workspace_id}&session_id=${context.session_id}&limit=100`,
  );
  expect(asArray(inputs.items).filter((value) => asRecord(value).client_idempotency_key === clientKey)).toHaveLength(1);
});

test("stopping a WorkerItem execution stays scoped to that execution", async ({
  interview,
  page,
}) => {
  test.info().annotations.push({ type: "action", description: "F07-07" });

  await establishControlsMainline(interview, page);
  await interview.openAttention("validate");
  const { executionId, workerItemId } = await loadValidationContext(interview, page);

  page.on("dialog", (dialog) => void dialog.accept());
  await page.getByRole("button", { name: "停止 Execution", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "停止已请求" })).toBeVisible({ timeout: 30_000 });
  const stoppedExecution = await getJson(
    page,
    interview,
    `/api/v1/executions/${executionId}?workspace_id=${interview.manifest.workspace_id}`,
  );
  expect(stoppedExecution.stop_requested).toBe(true);
  const stoppedWorkspace = await getJson(
    page,
    interview,
    `/api/v1/workspaces/${interview.manifest.workspace_id}/worker-items/${workerItemId}/workspace`,
  );
  expect(asRecord(stoppedWorkspace.current_runtime).execution_id).toBe(executionId);
  const afterStop = await interview.getFacts();
  expect(afterStop.workerItems.validate?.executionId).toBe(executionId);
  expect(afterStop.workerItems.report?.executionId).not.toBeNull();
});

test("an interrupted result check retries the same candidate", async ({
  interview,
  page,
}) => {
  test.info().annotations.push({ type: "action", description: "F11-03" });
  test.info().annotations.push({ type: "action", description: "F08-05" });

  await establishControlsMainline(interview, page);
  await interview.openAttention("validate");
  await interview.sendInput("手机号允许为空，有值时才校验格式");
  await interview.releaseGate("before-artifact");
  await interview.approvePendingRelease("review-report");
  await interview.approvePendingRelease("integrate");

  const rootTaskId = (await interview.getFacts()).rootTaskId;
  expect(rootTaskId).not.toBeNull();
  const resultPath = `/api/v1/workspaces/${interview.manifest.workspace_id}/tasks/${rootTaskId}/results`;
  await expect.poll(
    async () => asString(asRecord(asArray((await getJson(page, interview, resultPath)).criteria)[0]).verification_status),
    { timeout: 30_000 },
  ).toBe("interrupted");
  const interrupted = await getJson(page, interview, resultPath);
  const candidateId = asString(asRecord(interrupted.current_candidate).candidate_id);
  expect(candidateId).not.toBeNull();
  expect(asArray(interrupted.available_actions).some(
    (action) => asRecord(action).command_type === "CompleteTask",
  )).toBe(false);
  expect(asArray(interrupted.blocking_reasons).length).toBeGreaterThan(0);

  await interview.openWorker("integrate");
  const operations = page.getByRole("region", { name: "结果恢复与局部返工", exact: true });
  await expect(operations.getByRole("button", { name: "重试检查", exact: true })).toBeVisible({
    timeout: 30_000,
  });
  await operations.getByRole("button", { name: "重试检查", exact: true }).click();
  const form = operations.getByRole("form", { name: "重试检查", exact: true });
  await form.getByLabel("操作原因", { exact: true }).fill("检查进程被演示环境中断，重新核对同一候选");
  await form.getByRole("button", { name: "提交重试检查", exact: true }).click();

  await expect.poll(
    async () => asString(asRecord(asArray((await getJson(page, interview, resultPath)).criteria)[0]).verification_status),
    { timeout: 30_000 },
  ).toBe("passed");
  const recovered = await getJson(page, interview, resultPath);
  expect(asString(asRecord(recovered.current_candidate).candidate_id)).toBe(candidateId);
  await expect(operations.getByRole("button", { name: "重试检查", exact: true })).toHaveCount(0);
});

test("queued execution worker pin can be cleared without changing the execution", async ({
  interview,
  page,
}) => {
  test.info().annotations.push({ type: "action", description: "F16-03" });
  test.info().annotations.push({ type: "action", description: "F16-05" });

  await stopDemoComponent(interview, "gateway-worker");
  await stopDemoComponent(interview, "scheduler-worker");
  await interview.openChat();
  await interview.sendRequirement("为项目增加 CSV 批量导入功能，手机号为空的策略在执行时向用户确认");
  await interview.openProposal();
  const clarification = page.getByRole("textbox", { name: "澄清答复", exact: true });
  const confirm = page.getByRole("button", { name: "确认并创建任务", exact: true });
  await expect.poll(
    async () => await clarification.isVisible().catch(() => false) || await confirm.isVisible().catch(() => false),
    { timeout: 30_000 },
  ).toBe(true);
  if (await clarification.isVisible().catch(() => false)) {
    await clarification.fill("optional");
    await page.getByRole("button", { name: "提交澄清答复", exact: true }).click();
  }
  await expect(confirm).toBeVisible({ timeout: 30_000 });
  await interview.confirmPlan();
  await expect.poll(async () => Object.keys((await interview.getFacts()).workerItems).length).toBeGreaterThan(0);

  await interview.releaseGate("parallel");
  await interview.openWorker("validate");

  const routing = page.getByRole("region", { name: "执行尝试", exact: true });
  await expect(routing).toBeVisible({ timeout: 30_000 });
  await expect(routing).toContainText("排队中");
  await routing.getByRole("button", { name: "调整执行节点", exact: true }).click();
  const chooser = routing.getByRole("region", { name: "执行节点选择器", exact: true });
  await expect(chooser).toBeVisible();
  const alternate = chooser.getByRole("button", { name: /演示备用执行节点/ });
  await expect(alternate).toBeEnabled();
  await alternate.click();
  await chooser.getByRole("button", { name: "固定到此节点", exact: true }).click();
  await expect(routing).toContainText("已固定到 演示备用执行节点");

  const pinned = await getJson(page, interview, `/api/v1/executions/${(await interview.getFacts()).workerItems.validate?.executionId}?workspace_id=${interview.manifest.workspace_id}`);
  expect(asRecord(pinned.pinned_worker_summary).display_name).toBe("演示备用执行节点");

  await routing.getByRole("button", { name: "恢复自动选择", exact: true }).click();
  await expect(routing).toContainText("已恢复自动选择");
  const cleared = await getJson(page, interview, `/api/v1/executions/${(await interview.getFacts()).workerItems.validate?.executionId}?workspace_id=${interview.manifest.workspace_id}`);
  expect(cleared.pinned_worker_id).toBeNull();
});

test("additional rework budget stays scoped to the current result", async ({
  interview,
  page,
}) => {
  test.info().annotations.push({ type: "action", description: "F11-04" });

  await establishControlsMainline(interview, page);
  await interview.openAttention("validate");
  await interview.sendInput("手机号允许为空，有值时才校验格式");
  await interview.releaseGate("before-artifact");
  await interview.approvePendingRelease("review-report");
  await interview.approvePendingRelease("integrate");

  const rootTaskId = (await interview.getFacts()).rootTaskId;
  expect(rootTaskId).not.toBeNull();
  const resultPath = `/api/v1/workspaces/${interview.manifest.workspace_id}/tasks/${rootTaskId}/results`;
  await expect.poll(
    async () => asString(asRecord(asArray((await getJson(page, interview, resultPath)).criteria)[0]).verification_status),
    { timeout: 30_000 },
  ).toBe("interrupted");
  const before = await getJson(page, interview, resultPath);
  const beforeBudget = asRecord(before.rework_budget);
  const beforeLimit = Number(beforeBudget.auto_rounds_limit);
  expect(beforeLimit).toBeGreaterThanOrEqual(0);

  await interview.openWorker("integrate");
  const operations = page.getByRole("region", { name: "结果恢复与局部返工", exact: true });
  await operations.getByRole("button", { name: "增加返工额度", exact: true }).click();
  const form = operations.getByRole("form", { name: "增加返工额度", exact: true });
  await form.getByLabel("追加轮数", { exact: true }).fill("2");
  await form.getByLabel("追加秒数", { exact: true }).fill("600");
  await form.getByLabel("额度调整原因", { exact: true }).fill("当前结果需要额外两轮局部返工");
  await form.getByRole("button", { name: "提交额度调整", exact: true }).click();

  await expect.poll(
    async () => Number(asRecord((await getJson(page, interview, resultPath)).rework_budget).auto_rounds_limit),
    { timeout: 30_000 },
  ).toBe(beforeLimit + 2);
  const after = await getJson(page, interview, resultPath);
  expect(Number(asRecord(after.rework_budget).auto_seconds_limit)).toBe(
    Number(beforeBudget.auto_seconds_limit) + 600,
  );
});

test("a blocked result context assembly retries through the knowledge service", async ({
  interview,
  page,
}) => {
  test.info().annotations.push({ type: "action", description: "F13-05" });

  await interview.openChat();
  await interview.sendRequirement(
    "为项目增加 CSV 批量导入功能，手机号为空的策略在执行时向用户确认",
  );
  await interview.openProposal();
  const clarification = page.getByRole("textbox", { name: "澄清答复", exact: true });
  const confirm = page.getByRole("button", { name: "确认并创建任务", exact: true });
  await expect.poll(
    async () => await clarification.isVisible().catch(() => false) || await confirm.isVisible().catch(() => false),
    { timeout: 30_000 },
  ).toBe(true);
  if (await clarification.isVisible().catch(() => false)) {
    await clarification.fill("optional");
    await expect(page.getByRole("button", { name: "提交澄清答复", exact: true })).toBeEnabled();
    await page.getByRole("button", { name: "提交澄清答复", exact: true }).click();
  }
  await expect(confirm).toBeVisible({ timeout: 30_000 });
  await interview.confirmPlan();
  await expect.poll(async () => Object.keys((await interview.getFacts()).workerItems).length).toBeGreaterThan(0);

  await stopDemoComponent(interview, "knowledge-service");
  await interview.releaseGate("parallel");

  const facts = await interview.getFacts();
  const reportWorkerItemId = facts.workerItems.report?.id;
  expect(reportWorkerItemId).toBeDefined();
  const resultInputsPath = `/api/v1/workspaces/${interview.manifest.workspace_id}/worker-items/${reportWorkerItemId}/result-inputs`;
  await expect.poll(async () => {
    const response = await page.request.get(new URL(
      resultInputsPath,
      interview.manifest.endpoints.control_plane,
    ).toString());
    if (!response.ok()) return "unavailable";
    const payload = await response.json() as Record<string, unknown>;
    const delivery = asRecord(payload.delivery);
    return typeof delivery.assembly_status === "string" ? delivery.assembly_status : "none";
  }, { timeout: 45_000 }).toMatch(/blocked|retry_wait/);

  // Hold the automatic retry after observing retry_wait so this case exercises
  // the user-facing RetryContextAssembly action deterministically.
  await stopDemoComponent(interview, "context-package-worker");
  await interview.restart("knowledge-service");
  await interview.openWorker("report");
  await expect(page.getByRole("button", { name: "重试上下文组装", exact: true })).toBeVisible({ timeout: 15_000 });
  await page.getByRole("button", { name: "重试上下文组装", exact: true }).click();
  await interview.restart("context-package-worker");
  await expect.poll(async () => {
    const response = await page.request.get(new URL(
      resultInputsPath,
      interview.manifest.endpoints.control_plane,
    ).toString());
    if (!response.ok()) return "unavailable";
    const payload = await response.json() as Record<string, unknown>;
    const delivery = asRecord(payload.delivery);
    return `${delivery.assembly_status ?? "none"}:${delivery.acknowledgement_status ?? "none"}`;
  }, { timeout: 45_000 }).toMatch(/succeeded:acknowledged|ready:acknowledged/);
});

async function getJson(
  page: Page,
  interview: InterviewDriver,
  path: string,
): Promise<Record<string, unknown>> {
  const response = await page.request.get(
    new URL(path, interview.manifest.endpoints.control_plane).toString(),
  );
  expect(response.ok()).toBeTruthy();
  return await response.json() as Record<string, unknown>;
}

async function establishControlsMainline(
  interview: InterviewDriver,
  page: Page,
): Promise<void> {
  await interview.openChat();
  await interview.sendRequirement("为项目增加 CSV 批量导入功能，手机号为空的策略在执行时向用户确认");
  await interview.openProposal();
  const clarification = page.getByRole("textbox", { name: "澄清答复", exact: true });
  const confirm = page.getByRole("button", { name: "确认并创建任务", exact: true });
  await expect.poll(
    async () => await clarification.isVisible().catch(() => false) || await confirm.isVisible().catch(() => false),
    { timeout: 30_000 },
  ).toBe(true);
  if (await clarification.isVisible().catch(() => false)) {
    await clarification.fill("optional");
    await page.getByRole("button", { name: "提交澄清答复", exact: true }).click();
  }
  await expect(confirm).toBeVisible({
    timeout: 30_000,
  });
  await interview.confirmPlan();
  await expect.poll(async () => (await interview.getFacts()).parallelObserved).toBe(true);
  await interview.releaseGate("parallel");
}

async function workerId(interview: InterviewDriver, key: string): Promise<string> {
  const item = (await interview.getFacts()).workerItems[key];
  if (item === undefined) throw new Error(`missing WorkerItem ${key}`);
  return item.id;
}

async function getSession(
  page: Page,
  interview: InterviewDriver,
  sessionId: string,
): Promise<{ id: string; version: number }> {
  const response = await getJson(
    page,
    interview,
    `/api/v1/gateway/sessions?workspace_id=${interview.manifest.workspace_id}&session_id=${sessionId}&limit=1`,
  );
  const item = asRecord(asArray(response.items)[0]);
  return { id: String(item.id), version: Number(item.version) };
}

async function loadValidationContext(
  interview: InterviewDriver,
  page: Page,
): Promise<{ workerItemId: string; executionId: string; context: Record<string, unknown> }> {
  const facts = await interview.getFacts();
  const validationItem = facts.workerItems.validate;
  expect(validationItem?.sessionId).not.toBeNull();
  expect(validationItem?.executionId).not.toBeNull();
  expect(validationItem?.id).toBeDefined();
  const workspace = await getJson(
    page,
    interview,
    `/api/v1/workspaces/${interview.manifest.workspace_id}/worker-items/${validationItem?.id}/workspace`,
  );
  const context = asRecord(workspace.intervention_context);
  expect(context.worker_item_id).toBe(validationItem?.id);
  expect(context.execution_id).toBe(validationItem?.executionId);
  return {
    workerItemId: String(validationItem?.id),
    executionId: String(validationItem?.executionId),
    context,
  };
}

async function stopDemoComponent(interview: InterviewDriver, component: string): Promise<void> {
  const identityPath = resolve(interview.manifest.run_root, "processes", `${component}.json`);
  const identity = JSON.parse(readFileSync(identityPath, "utf8")) as { pid?: number };
  if (typeof identity.pid !== "number") throw new Error(`missing process identity ${component}`);
  try {
    process.kill(identity.pid, "SIGTERM");
  } catch (error) {
    if (!(error instanceof Error) || !error.message.includes("ESRCH")) throw error;
  }
  await new Promise<void>((resolvePromise) => {
    const deadline = Date.now() + 5_000;
    const check = () => {
      try {
        process.kill(identity.pid!, 0);
      } catch {
        resolvePromise();
        return;
      }
      if (Date.now() >= deadline) {
        resolvePromise();
        return;
      }
      setTimeout(check, 50);
    };
    check();
  });
}

async function findInput(
  page: Page,
  interview: InterviewDriver,
  sessionId: string,
  clientKey: string,
): Promise<{ id: string; status: string; textLength: number }> {
  const response = await getJson(
    page,
    interview,
    `/api/v1/gateway/inputs?workspace_id=${interview.manifest.workspace_id}&session_id=${sessionId}&limit=100`,
  );
  const input = asArray(response.items)
    .map(asRecord)
    .find((value) => value.client_idempotency_key === clientKey);
  if (input === undefined) throw new Error(`missing execution input ${clientKey}`);
  return { id: String(input.execution_id), status: String(input.status), textLength: Number(input.text_length) };
}

function commandEnvelope(input: {
  workspaceId: string;
  commandType: string;
  targetType: string;
  targetId: string;
  expectedVersion: number;
  idempotencyKey: string;
  payload: Record<string, unknown>;
}): Record<string, unknown> {
  return {
    command_id: randomUUID(),
    workspace_id: input.workspaceId,
    command_type: input.commandType,
    target_type: input.targetType,
    actor: { type: "local", id: "aitejiu" },
    target_id: input.targetId,
    expected_version: input.expectedVersion,
    idempotency_key: input.idempotencyKey,
    correlation_id: randomUUID(),
    payload: input.payload,
  };
}

async function postCommand(
  page: Page,
  interview: InterviewDriver,
  command: Record<string, unknown>,
): Promise<{ response: Awaited<ReturnType<Page["request"]["post"]>>; body: Record<string, unknown> }> {
  const response = await page.request.post(
    new URL("/api/v1/commands", interview.manifest.endpoints.control_plane).toString(),
    { data: command },
  );
  return { response, body: await response.json() as Record<string, unknown> };
}

function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function asString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

async function findWritableWorkerItem(
  page: Page,
  interview: InterviewDriver,
  rootTaskId: string | undefined,
): Promise<string | null> {
  if (rootTaskId === undefined) return null;
  let found: string | null = null;
  await expect.poll(async () => {
    const graph = await getJson(
      page,
      interview,
      `/api/v1/tasks/${rootTaskId}/graph?workspace_id=${interview.manifest.workspace_id}`,
    );
    for (const node of asArray(graph.nodes).map(asRecord)) {
      const subtaskKey = typeof node.subtask_key === "string" ? node.subtask_key : null;
      if (subtaskKey === null) continue;
      const subtask = await getJson(
        page,
        interview,
        `/api/v1/tasks/${rootTaskId}/subtasks/${encodeURIComponent(subtaskKey)}?workspace_id=${interview.manifest.workspace_id}`,
      );
      for (const item of asArray(subtask.worker_items).map(asRecord)) {
        const workerItemId = typeof item.worker_item_id === "string" ? item.worker_item_id : null;
        if (workerItemId === null) continue;
        const workspace = await getJson(
          page,
          interview,
          `/api/v1/workspaces/${interview.manifest.workspace_id}/worker-items/${workerItemId}/workspace`,
        );
        const runtime = asRecord(workspace.current_runtime);
        if (typeof runtime.session_id === "string" && workspace.can_intervene !== false) {
          found = workerItemId;
          return found;
        }
      }
    }
    return found;
  }, { timeout: 30_000 }).not.toBeNull();
  return found;
}
