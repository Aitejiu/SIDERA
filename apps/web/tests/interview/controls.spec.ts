import { randomUUID } from "node:crypto";
import type { Page } from "@playwright/test";

import type { InterviewDriver } from "./driver";
import { expect, test } from "./fixtures";

test.use({ demoScenario: "controls" });

test("F03 planning controls keep clarification, revision and rejection explicit", async ({
  interview,
  page,
}) => {
  test.info().annotations.push({ type: "action", description: "F03-03" });
  test.info().annotations.push({ type: "action", description: "F03-04" });
  test.info().annotations.push({ type: "action", description: "F03-05" });

  await interview.openChat();
  await interview.sendRequirement("为项目增加 CSV 批量导入功能，手机号策略需要确认");
  await interview.openProposal();
  await expect(page.getByRole("heading", { name: "需要你的回答", exact: true })).toBeVisible();
  await page.getByRole("textbox", { name: "澄清答复", exact: true }).fill("optional");
  await page.getByRole("button", { name: "提交澄清答复", exact: true }).click();
  await expect(page.getByRole("button", { name: "确认并创建任务", exact: true })).toBeVisible({
    timeout: 30_000,
  });

  await page.getByRole("button", { name: "要求修改", exact: true }).click();
  await page.getByRole("textbox", { name: "修改意见", exact: true }).fill(
    "保留现有 Worker 上下文，只调整方案说明",
  );
  await page.getByRole("button", { name: "提交修改意见", exact: true }).click();
  await expect(page.getByText(/方案 R2|方案 R1/, { exact: false })).toBeVisible({ timeout: 30_000 });

  await page.getByRole("button", { name: "拒绝方案", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "确认拒绝方案", exact: true });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "确认拒绝", exact: true }).click();
  await expect(page.getByText("已拒绝", { exact: true })).toBeVisible({ timeout: 15_000 });
});

test("stale planning confirmation and cancellation remain explicit conflicts", async ({ interview, page }) => {
  test.info().annotations.push({ type: "action", description: "F03-08" });
  test.info().annotations.push({ type: "action", description: "F09-05" });

  await interview.openChat();
  await interview.sendRequirement("为项目增加 CSV 批量导入功能，手机号策略需要确认");
  await interview.openProposal();
  await answerClarificationIfNeeded(page);

  const requestId = page.url().match(/\/planning\/([^/?]+)$/)?.[1];
  expect(requestId).toBeDefined();
  const requestResponse = await page.request.get(new URL(
    `/api/v1/workspaces/${interview.manifest.workspace_id}/planning-requests/${requestId}`,
    interview.manifest.endpoints.control_plane,
  ).toString());
  expect(requestResponse.ok()).toBeTruthy();
  const request = await requestResponse.json() as {
    version?: number;
    current_proposal_revision?: number | null;
    current_proposal_content_hash?: string | null;
    planning_profile_id?: string;
    planning_profile_version?: number;
  };
  const proposalResponse = await page.request.get(new URL(
    `/api/v1/workspaces/${interview.manifest.workspace_id}/planning-requests/${requestId}/proposals/${request.current_proposal_revision}`,
    interview.manifest.endpoints.control_plane,
  ).toString());
  expect(proposalResponse.ok()).toBeTruthy();
  const proposal = await proposalResponse.json() as { revision?: number; content_hash?: string };
  expect(request.version).toBeGreaterThan(1);
  expect(request.current_proposal_revision).toBeDefined();
  expect(request.current_proposal_content_hash).toMatch(/^[0-9a-f]{64}$/i);
  expect(request.planning_profile_id).toMatch(/^[0-9a-f-]{36}$/i);
  expect(request.planning_profile_version).toBeGreaterThan(0);
  expect(proposal.content_hash).toMatch(/^[0-9a-f]{64}$/i);

  const stale = await page.request.post(
    new URL("/api/v1/commands", interview.manifest.endpoints.control_plane).toString(),
    {
      data: {
        command_id: randomUUID(),
        workspace_id: interview.manifest.workspace_id,
        command_type: "ConfirmPlanningProposal",
        target_type: "planning_request",
        actor: { type: "local", id: "aitejiu" },
        target_id: requestId,
        expected_version: Number(request.version) - 1,
        idempotency_key: `controls-stale-confirm:${randomUUID()}`,
        correlation_id: randomUUID(),
        payload: {
          proposal_revision: proposal.revision,
          proposal_content_hash: proposal.content_hash,
          profile_id: request.planning_profile_id,
          profile_version: request.planning_profile_version,
        },
      },
    },
  );
  expect(stale.status()).toBe(409);
  expect((await stale.json()).error.code).toBe("planning_conflict");

  const staleCancel = await page.request.post(
    new URL("/api/v1/commands", interview.manifest.endpoints.control_plane).toString(),
    {
      data: {
        command_id: randomUUID(),
        workspace_id: interview.manifest.workspace_id,
        command_type: "CancelPlanningRequest",
        target_type: "planning_request",
        actor: { type: "local", id: "aitejiu" },
        target_id: requestId,
        expected_version: Number(request.version) - 1,
        idempotency_key: `controls-stale-cancel:${randomUUID()}`,
        correlation_id: randomUUID(),
        payload: {},
      },
    },
  );
  expect(staleCancel.status()).toBe(409);
  expect((await staleCancel.json()).error.code).toBe("planning_conflict");
  expect((await interview.getFacts()).rootTaskId).toBeNull();
});

test("F16 runtime settings persist without replacing a live session", async ({
  interview,
  page,
}) => {
  test.info().annotations.push({ type: "action", description: "F16-01" });
  test.info().annotations.push({ type: "action", description: "F16-02" });
  test.info().annotations.push({ type: "action", description: "F16-04" });

  await establishControlsMainline(interview, page);
  await expect.poll(
    async () => (await interview.getFacts()).workerItems.validate?.sessionId,
    { timeout: 30_000 },
  ).not.toBeNull();

  const before = await interview.getFacts();
  const validationWorkerId = interview.manifest.worker_ids.validation;
  await page.goto(
    `${interview.manifest.endpoints.web}/workspaces/${interview.manifest.workspace_id}/workers/${validationWorkerId}`,
  );
  await page.getByRole("button", { name: "调整运行配置", exact: true }).click();
  await page.getByLabel("Adapter 模块", { exact: true }).selectOption(
    interview.manifest.module_ids.worker_secondary,
  );
  await page.getByRole("button", { name: "保存运行配置", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "调整运行配置", exact: true })).toHaveCount(0);
  await expect(page.getByText("V2", { exact: true })).toBeVisible({ timeout: 15_000 });

  await page.reload();
  await page.getByRole("button", { name: "调整运行配置", exact: true }).click();
  await expect(page.getByLabel("Adapter 模块", { exact: true })).toHaveValue(
    interview.manifest.module_ids.worker_secondary,
  );
  const after = await interview.getFacts();
  expect(after.workerItems.validate?.sessionId).toBe(before.workerItems.validate?.sessionId);

  const invalidRuntime = await page.request.post(
    `${interview.manifest.endpoints.control_plane}/api/v1/commands`,
    {
      data: {
        command_id: randomUUID(),
        workspace_id: interview.manifest.workspace_id,
        command_type: "ConfigureWorkerRuntime",
        target_type: "worker_runtime_configuration",
        actor: { type: "local", id: "aitejiu" },
        target_id: validationWorkerId,
        expected_version: 2,
        idempotency_key: `controls-invalid-runtime:${randomUUID()}`,
        correlation_id: randomUUID(),
        payload: {
          adapter_module_instance_id: randomUUID(),
          adapter_module_config_version: 1,
          workspace_module_instance_id: interview.manifest.module_ids.workspace,
          workspace_module_config_version: 1,
        },
      },
    },
  );
  expect(invalidRuntime.status()).toBe(422);
  expect((await invalidRuntime.json()).error.code).toBe("demo_runtime_binding_forbidden");
});

async function establishControlsMainline(
  interview: InterviewDriver,
  page: Page,
): Promise<void> {
  await interview.openChat();
  await interview.sendRequirement("为项目增加 CSV 批量导入功能，手机号为空的策略在执行时向用户确认");
  await interview.openProposal();
  await answerClarificationIfNeeded(page);
  await interview.confirmPlan();
  await interview.releaseGate("parallel");
}

async function answerClarificationIfNeeded(page: Page): Promise<void> {
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
}
