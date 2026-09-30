import { randomUUID } from "node:crypto";

import { test, expect } from "./fixtures";

test.use({ demoScenario: "handoff" });

test("X2 handoff preserves the WorkerItem and task generation", async ({ interview, page }) => {
  for (const actionId of ["F05-03", "F06-04", "F10-01", "F10-02", "F10-04"]) {
    test.info().annotations.push({ type: "action", description: actionId });
  }
  await interview.openChat();
  await interview.sendRequirement("为项目增加 CSV 批量导入功能，手机号为空的策略在执行时向用户确认");
  await interview.openProposal();
  await interview.confirmPlan();
  await expect.poll(async () => (await interview.getFacts()).parallelObserved).toBe(true);
  await interview.releaseGate("parallel");
  await interview.openAttention("validate");

  const before = await interview.getFacts();
  const workerItemId = before.workerItems.validate?.id;
  const sourceSessionId = before.workerItems.validate?.sessionId;
  expect(workerItemId).toBeDefined();
  expect(sourceSessionId).toBeDefined();

  await page.getByRole("button", { name: "调整执行 Agent", exact: true }).click();
  const chooser = page.getByRole("dialog", { name: "调整执行 Agent", exact: true });
  await expect(chooser).toBeVisible();
  await chooser.getByRole("radio", { name: "选择 演示备用执行节点", exact: true }).check();
  await chooser.getByLabel("切换原因", { exact: true }).fill("演示更换执行节点");
  await chooser.getByLabel("交接指令", { exact: true }).fill("保留手机号允许为空的规则，继续校验实现");
  await chooser.getByRole("button", { name: "查看切换方案", exact: true }).click();

  const review = page.getByRole("dialog", { name: "确认 Agent 切换", exact: true });
  await expect(review).toBeVisible();
  await review.getByRole("button", { name: "确认切换", exact: true }).click();

  await expect.poll(
    async () => (await interview.getFacts()).workerItems.validate?.sessionId,
    { timeout: 60_000 },
  ).not.toBe(sourceSessionId);
  const after = await interview.getFacts();
  expect(after.workerItems.validate?.id).toBe(workerItemId);
  expect(after.planGeneration).toBe(before.planGeneration);
  await expect(page.getByText("Agent 切换", { exact: false }).first()).toBeVisible();
});

test("old WorkerItem context is rejected after handoff and keeps the draft", async ({
  interview,
  page,
}) => {
  test.info().annotations.push({ type: "action", description: "F07-03" });

  await interview.openChat();
  await interview.sendRequirement("为项目增加 CSV 批量导入功能，手机号为空的策略在执行时向用户确认");
  await interview.openProposal();
  await interview.confirmPlan();
  await expect.poll(async () => (await interview.getFacts()).parallelObserved).toBe(true);
  await interview.releaseGate("parallel");
  await interview.openAttention("validate");

  const before = await interview.getFacts();
  const oldWorker = before.workerItems.validate;
  expect(oldWorker?.id).toBeDefined();
  expect(oldWorker?.sessionId).not.toBeNull();
  const workspacePath = new URL(
    `/api/v1/workspaces/${interview.manifest.workspace_id}/worker-items/${oldWorker?.id}/workspace`,
    interview.manifest.endpoints.control_plane,
  ).toString();
  const oldWorkspaceResponse = await page.request.get(workspacePath);
  expect(oldWorkspaceResponse.ok()).toBeTruthy();
  const oldWorkspace = await oldWorkspaceResponse.json() as Record<string, unknown>;
  const oldContext = oldWorkspace.intervention_context as Record<string, unknown>;
  const draft = page.getByRole("textbox", { name: "介入这个 Worker", exact: true });
  await draft.fill("保留这条草稿，等待新 Agent 接手后再决定是否发送");

  await page.getByRole("button", { name: "调整执行 Agent", exact: true }).click();
  const chooser = page.getByRole("dialog", { name: "调整执行 Agent", exact: true });
  await chooser.getByRole("radio", { name: "选择 演示备用执行节点", exact: true }).check();
  await chooser.getByLabel("切换原因", { exact: true }).fill("验证旧 context 不能误投");
  await chooser.getByLabel("交接指令", { exact: true }).fill("保留已有输入上下文后继续执行");
  await chooser.getByRole("button", { name: "查看切换方案", exact: true }).click();
  await page.getByRole("dialog", { name: "确认 Agent 切换", exact: true })
    .getByRole("button", { name: "确认切换", exact: true })
    .click();

  await expect.poll(
    async () => (await interview.getFacts()).workerItems.validate?.sessionId,
    { timeout: 60_000 },
  ).not.toBe(oldWorker?.sessionId);
  await expect(draft).toHaveValue("保留这条草稿，等待新 Agent 接手后再决定是否发送");

  const staleKey = `handoff-old-context:${randomUUID()}`;
  const staleResponse = await page.request.post(
    new URL("/api/v1/commands", interview.manifest.endpoints.control_plane).toString(),
    {
      data: {
        command_id: randomUUID(),
        workspace_id: interview.manifest.workspace_id,
        command_type: "SendExecutionInput",
        target_type: "execution_session",
        actor: { type: "local", id: "aitejiu" },
        target_id: oldContext.session_id,
        expected_version: oldContext.session_version,
        idempotency_key: `handoff-old-context-command:${staleKey}`,
        correlation_id: randomUUID(),
        payload: {
          context: oldContext,
          client_idempotency_key: staleKey,
          text: "这条旧 context 输入绝不能投递",
          attachment_refs: [],
        },
      },
    },
  );
  expect(staleResponse.status()).toBe(409);
  const oldInputs = await page.request.get(new URL(
    `/api/v1/gateway/inputs?workspace_id=${interview.manifest.workspace_id}&session_id=${oldContext.session_id}&limit=100`,
    interview.manifest.endpoints.control_plane,
  ).toString());
  expect(oldInputs.ok()).toBeTruthy();
  const oldInputItems = (await oldInputs.json() as { items?: Array<Record<string, unknown>> }).items ?? [];
  expect(oldInputItems.filter((item) => item.client_idempotency_key === staleKey)).toHaveLength(0);
});

test("an unfinished handoff can be cancelled without creating a target execution", async ({
  interview,
  page,
}) => {
  test.info().annotations.push({ type: "action", description: "F10-03" });

  await interview.openChat();
  await interview.sendRequirement("为项目增加 CSV 批量导入功能，手机号为空的策略在执行时向用户确认");
  await interview.openProposal();
  await interview.confirmPlan();
  await expect.poll(async () => (await interview.getFacts()).parallelObserved).toBe(true);
  await interview.releaseGate("parallel");
  await interview.openAttention("validate");

  const before = await interview.getFacts();
  const source = before.workerItems.validate;
  expect(source?.sessionId).not.toBeNull();
  await page.getByRole("button", { name: "调整执行 Agent", exact: true }).click();
  const chooser = page.getByRole("dialog", { name: "调整执行 Agent", exact: true });
  await chooser.getByRole("radio", { name: "选择 演示备用执行节点", exact: true }).check();
  await chooser.getByLabel("切换原因", { exact: true }).fill("先取消交接方案");
  await chooser.getByLabel("交接指令", { exact: true }).fill("不改变当前 WorkerItem 的执行归属");
  await chooser.getByRole("button", { name: "查看切换方案", exact: true }).click();
  const review = page.getByRole("dialog", { name: "确认 Agent 切换", exact: true });
  await expect(review).toBeVisible();
  await review.getByRole("button", { name: "取消方案", exact: true }).click();
  await expect(review).toHaveCount(0);

  await expect.poll(async () => {
    const response = await page.request.get(new URL(
      `/api/v1/workspaces/${interview.manifest.workspace_id}/worker-items/${source?.id}/workspace`,
      interview.manifest.endpoints.control_plane,
    ).toString());
    if (!response.ok()) return "unavailable";
    const workspace = await response.json() as Record<string, unknown>;
    return workspace.active_handoff ?? null;
  }).toBeNull();
  const after = await interview.getFacts();
  expect(after.workerItems.validate?.sessionId).toBe(source?.sessionId);
  expect(after.workerItems.validate?.executionId).toBe(source?.executionId);
});

test("handoff confirmation rejects a stale proposal identity", async ({ interview, page }) => {
  test.info().annotations.push({ type: "action", description: "F10-06" });

  await interview.openChat();
  await interview.sendRequirement("为项目增加 CSV 批量导入功能，手机号为空的策略在执行时向用户确认");
  await interview.openProposal();
  await interview.confirmPlan();
  await expect.poll(async () => (await interview.getFacts()).parallelObserved).toBe(true);
  await interview.releaseGate("parallel");
  await interview.openAttention("validate");
  const before = await interview.getFacts();
  const source = before.workerItems.validate;
  expect(source?.id).toBeDefined();
  const sourceWorkspaceResponse = await page.request.get(new URL(
    `/api/v1/workspaces/${interview.manifest.workspace_id}/worker-items/${source?.id}/workspace`,
    interview.manifest.endpoints.control_plane,
  ).toString());
  expect(sourceWorkspaceResponse.ok()).toBeTruthy();
  const sourceWorkspace = await sourceWorkspaceResponse.json() as Record<string, unknown>;
  const sourceWorkerItem = sourceWorkspace.worker_item as Record<string, unknown>;
  const sourceWorkerItemVersion = Number(sourceWorkerItem.version);
  expect(Number.isInteger(sourceWorkerItemVersion)).toBeTruthy();

  const prepareResponsePromise = page.waitForResponse((response) => {
    if (!response.url().endsWith("/api/v1/commands") || response.request().method() !== "POST") return false;
    try {
      return (response.request().postDataJSON() as { command_type?: string }).command_type === "PrepareWorkerHandoff";
    } catch {
      return false;
    }
  });
  await page.getByRole("button", { name: "调整执行 Agent", exact: true }).click();
  const chooser = page.getByRole("dialog", { name: "调整执行 Agent", exact: true });
  await chooser.getByRole("radio", { name: "选择 演示备用执行节点", exact: true }).check();
  await chooser.getByLabel("切换原因", { exact: true }).fill("验证陈旧提案不会改变执行");
  await chooser.getByLabel("交接指令", { exact: true }).fill("仅用于陈旧身份校验");
  await chooser.getByRole("button", { name: "查看切换方案", exact: true }).click();
  const prepareResponse = await prepareResponsePromise;
  const prepareBody = await prepareResponse.json() as { aggregate_id?: string };
  expect(prepareBody.aggregate_id).toMatch(/^[0-9a-f-]{36}$/i);
  const proposalResponse = await page.request.get(new URL(
    `/api/v1/workspaces/${interview.manifest.workspace_id}/handoff-proposals/${prepareBody.aggregate_id}`,
    interview.manifest.endpoints.control_plane,
  ).toString());
  expect(proposalResponse.ok()).toBeTruthy();
  const proposal = await proposalResponse.json() as Record<string, unknown>;
  const stale = await page.request.post(
    new URL("/api/v1/commands", interview.manifest.endpoints.control_plane).toString(),
    {
      data: {
        command_id: randomUUID(),
        workspace_id: interview.manifest.workspace_id,
        command_type: "ConfirmWorkerHandoff",
        target_type: "handoff_proposal",
        actor: { type: "local", id: "aitejiu" },
        target_id: prepareBody.aggregate_id,
        expected_version: proposal.version,
        idempotency_key: `handoff-stale-proposal:${randomUUID()}`,
        correlation_id: randomUUID(),
        payload: {
          proposal_id: prepareBody.aggregate_id,
          expected_proposal_version: proposal.version,
          expected_proposal_hash: "0".repeat(64),
          expected_worker_item_version: sourceWorkerItemVersion,
        },
      },
    },
  );
  expect(stale.status()).toBe(409);
  expect((await interview.getFacts()).workerItems.validate?.sessionId).toBe(source?.sessionId);
  await page.getByRole("dialog", { name: "确认 Agent 切换", exact: true })
    .getByRole("button", { name: "取消方案", exact: true })
    .click();
});

test.describe("handoff target-start recovery", () => {
  test.use({ demoScenario: "controls" });

  test("unknown target start stays blocked until an evidence-backed recovery", async ({ interview, page }) => {
    test.setTimeout(120_000);
    test.info().annotations.push({ type: "action", description: "F10-05" });

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
    await expect.poll(async () => (await interview.getFacts()).parallelObserved).toBe(true);
    await interview.releaseGate("parallel");
    await interview.openAttention("validate");

    const before = await interview.getFacts();
    const source = before.workerItems.validate;
    expect(source?.id).toBeDefined();

    await page.getByRole("button", { name: "调整执行 Agent", exact: true }).click();
    const chooser = page.getByRole("dialog", { name: "调整执行 Agent", exact: true });
    await chooser.getByRole("radio", { name: "选择 演示备用执行节点", exact: true }).check();
    await chooser.getByLabel("切换原因", { exact: true }).fill("验证目标启动未知时不重复创建写入者");
    await chooser.getByLabel("交接指令", { exact: true }).fill("保留已有上下文，仅在证据确认后恢复");
    await chooser.getByRole("button", { name: "查看切换方案", exact: true }).click();
    const confirmResponsePromise = page.waitForResponse((response) => {
      if (!response.url().endsWith("/api/v1/commands") || response.request().method() !== "POST") return false;
      try {
        return (response.request().postDataJSON() as { command_type?: string }).command_type === "ConfirmWorkerHandoff";
      } catch {
        return false;
      }
    });
    await page.getByRole("dialog", { name: "确认 Agent 切换", exact: true })
      .getByRole("button", { name: "确认切换", exact: true })
      .click();
    const confirmResponse = await confirmResponsePromise;
    expect(confirmResponse.ok(), await confirmResponse.text()).toBeTruthy();
    const confirmPayload = await confirmResponse.json() as { aggregate_id?: string };
    const transitionId = confirmPayload.aggregate_id;
    expect(transitionId).toMatch(/^[0-9a-f-]{36}$/i);
    if (transitionId === undefined) throw new Error("handoff transition id is missing");

    const transitionPath = new URL(
      `/api/v1/workspaces/${interview.manifest.workspace_id}/handoff-transitions/${transitionId}`,
      interview.manifest.endpoints.control_plane,
    ).toString();
    await expect.poll(async () => {
      const response = await page.request.get(transitionPath);
      if (!response.ok()) return "unavailable";
    const transition = await response.json() as Record<string, unknown>;
      return transition.status ?? "unknown";
    }, { timeout: 90_000 }).toBe("blocked_unknown");

    await page.reload();
    const progress = page.getByRole("region", { name: "Agent 切换进度", exact: true });
    await expect(progress.getByText("目标 Agent 启动结果未知", { exact: true })).toBeVisible({ timeout: 30_000 });
    await expect(progress.getByText("写入者状态未知", { exact: true })).toBeVisible();

    const blocked = await page.request.get(transitionPath);
    expect(blocked.ok()).toBeTruthy();
    const blockedTransition = await blocked.json() as Record<string, unknown>;
    expect(blockedTransition.status).toBe("blocked_unknown");
    const blockedTargetExecution = ((blockedTransition.target as Record<string, unknown>).execution as Record<string, unknown> | null)?.execution_id;
    expect(blockedTargetExecution).toBeDefined();
    expect(blockedTransition.available_recovery_strategies).toEqual([]);
    expect(((blockedTransition.target as Record<string, unknown>).execution as Record<string, unknown> | null)?.session_id)
      .toBeNull();
    await expect(progress.getByRole("heading", { name: "恢复这次切换", exact: true })).toHaveCount(0);
    await expect.poll(async () => {
      const response = await page.request.get(transitionPath);
      if (!response.ok()) return "unavailable";
      const transition = await response.json() as Record<string, unknown>;
      return `${transition.status}:${((transition.target as Record<string, unknown>).execution as Record<string, unknown> | null)?.execution_id ?? "none"}`;
    }, { timeout: 10_000 }).toBe(`blocked_unknown:${blockedTargetExecution}`);
  });
});
