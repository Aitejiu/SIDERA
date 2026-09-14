import { randomUUID } from "node:crypto";

import type { Page } from "@playwright/test";

import {
  expect,
  runHandoffUntilUnknown,
  runHandoffWorkerOnce,
  test,
  type R6HandoffSeed,
} from "./fixtures";

test.describe("R6.2 WorkerItem handoff vertical", () => {
  test("reviews stale input, confirms a new proposal, and converges without sending the draft", async ({
    page,
    seedR6Handoff,
  }) => {
    const seed = seedR6Handoff("normal");
    const workerItemId = seed.worker_item_ids.b;
    const workerPath = `/workspaces/${seed.workspace_id}/worker-items/${workerItemId}`;

    await page.goto(workerPath);
    await expect(page.locator("#worker-item-title")).toHaveText("Worker B");
    await expect(page.locator(".worker-item-page__breadcrumb")).toContainText(
      "G4 cross-agent handoff control room / Subtask B",
    );
    const sidebar = page.getByRole("complementary", { name: "WorkerItem 事实" });
    await expect(sidebar).toContainText("Source Agent B");
    await expect(sidebar.getByRole("button", { name: "调整执行 Agent" })).toBeVisible();

    const tabs = page.getByRole("navigation", { name: "已打开的任务" });
    const workerTab = tabs.getByRole("link", {
      name: "Worker B", exact: true,
    });
    await expect(workerTab).toBeVisible();
    await tabs.getByRole("button", {
      name: "关闭Worker B", exact: true,
    }).click();
    await expect(page).toHaveURL(new RegExp(`/workspaces/${seed.workspace_id}/tasks$`));
    await expect(tabs.getByRole("link", {
      name: "Worker B", exact: true,
    })).toHaveCount(0);

    await page.goto(workerPath);
    const composerInput = page.getByRole("textbox", { name: "介入这个 Worker" });
    const retainedDraft = "保留这段草稿，切换完成后也不要自动发送";
    await composerInput.fill(retainedDraft);

    const adjustmentButton = page.getByRole("button", { name: "调整执行 Agent" });
    await adjustmentButton.focus();
    await adjustmentButton.click();
    let dialog = page.getByRole("dialog", { name: "调整执行 Agent" });
    await expect(dialog).toBeVisible();
    await dialog.getByRole("button", { name: "关闭 Agent 切换窗口" }).click();
    await expect(adjustmentButton).toBeFocused();

    await adjustmentButton.click();
    dialog = page.getByRole("dialog", { name: "调整执行 Agent" });
    await expect(dialog).toContainText("Target Agent One");
    await expect(dialog).toContainText("Target Agent Two");
    await expect(dialog).toContainText("推荐目标");
    await expect(dialog).toContainText("不可用");
    await expect(dialog.getByRole("radio", { name: "选择 Target Agent One" })).toBeChecked();
    await dialog.getByRole("textbox", { name: "切换原因" }).fill("需要切换到可用的执行 Agent");
    await dialog.getByRole("textbox", { name: "交接指令" }).fill("保留已有上下文，继续完成当前 WorkerItem");

    const workspaceResponse = await page.request.get(
      `/api/v1/workspaces/${seed.workspace_id}/worker-items/${workerItemId}/workspace`,
    );
    expect(workspaceResponse.ok()).toBeTruthy();
    const workspacePayload = await workspaceResponse.json() as {
      current_runtime?: { session_version?: number };
    };
    const sessionVersion = workspacePayload.current_runtime?.session_version;
    if (typeof sessionVersion !== "number") {
      throw new Error("G4 source Session version is missing");
    }

    let releaseProposal: (() => void) | undefined;
    let proposalRequestBlocked = false;
    const proposalGate = new Promise<void>((resolve) => {
      releaseProposal = resolve;
    });
    await page.route("**/handoff-proposals/**", async (route) => {
      if (route.request().method() === "GET" && !proposalRequestBlocked) {
        proposalRequestBlocked = true;
        await proposalGate;
      }
      await route.continue();
    });

    await dialog.getByRole("button", { name: "查看切换方案" }).click();
    await expect.poll(() => proposalRequestBlocked).toBe(true);
    await updatePendingInput(page, seed, sessionVersion);
    releaseProposal?.();

    let review = page.getByRole("dialog", { name: "确认 Agent 切换" });
    await expect(review).toContainText("方案已失效");
    await expect(review).toContainText("Task");
    await expect(review).toContainText("Subtask");
    await expect(review).toContainText("WorkerItem");
    await expect(review).toContainText("保留已有上下文，继续完成当前 WorkerItem");
    await expect(review.getByRole("button", { name: "确认切换" })).toBeDisabled();
    const staleProposalHash = await review.locator(".mono-value").first().innerText();

    await review.getByRole("button", { name: "返回选择" }).click();
    dialog = page.getByRole("dialog", { name: "调整执行 Agent" });
    await dialog.getByRole("button", { name: "查看切换方案" }).click();
    review = page.getByRole("dialog", { name: "确认 Agent 切换" });
    await expect(review).toBeVisible();
    await expect(review.getByText("方案已失效", { exact: false })).toHaveCount(0);
    await expect(review.getByRole("button", { name: "确认切换" })).toBeEnabled();
    const freshProposalHash = await review.locator(".mono-value").first().innerText();
    expect(freshProposalHash).not.toBe(staleProposalHash);

    await review.getByRole("button", { name: "确认切换" }).click();
    const progress = page.getByRole("region", { name: "Agent 切换进度" });
    await expect(progress).toBeVisible();
    await expect(progress).toContainText("Source Agent B");
    await expect(progress).toContainText("Target Agent One");
    await expect(progress.locator('[aria-label="Agent 切换阶段"]').getByText("记录源 Agent 检查点", { exact: true })).toBeVisible();
    await expect(progress.getByRole("link", { name: "查看源 Agent Trace" })).toHaveAttribute(
      "href",
      new RegExp(`/workspaces/${seed.workspace_id}/tasks/${seed.root_task_id}/executions/${seed.source_execution_id}/sessions/${seed.source_session_id}/trace`),
    );
    await expect(composerInput).toBeDisabled();
    await expect(composerInput).toHaveValue(retainedDraft);

    const runtimeSummary = runHandoffWorkerOnce(seed.workspace_id, "normal");
    expect(runtimeSummary.transition_status).toBe("converged");
    await page.reload();

    const refreshedSidebar = page.getByRole("complementary", { name: "WorkerItem 事实" });
    await expect(refreshedSidebar).toContainText("Target Agent One");
    const refreshedComposer = page.getByRole("textbox", { name: "介入这个 Worker" });
    await expect(refreshedComposer).toBeDisabled();
    await expect(page.getByText("工作 context 已变化，草稿已保留；请确认最新 context 后再发送。", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "确认当前 context" }).click();
    await expect(refreshedComposer).toBeEnabled();
    await expect(refreshedComposer).toHaveValue(retainedDraft);
    await assertNoHorizontalOverflow(page);
  });

  test("shows only authorized recovery choices for unknown target start and keeps the same WorkerItem path", async ({
    page,
    seedR6Handoff,
  }) => {
    const seed = seedR6Handoff("target-start-unknown");
    const workerItemId = seed.worker_item_ids.b;
    await page.goto(`/workspaces/${seed.workspace_id}/worker-items/${workerItemId}`);
    await prepareAndConfirm(page);

    const unknownSummary = runHandoffUntilUnknown(
      seed.workspace_id,
      "target-start-unknown",
    );
    expect(unknownSummary.transition_status).toBe("blocked_unknown");
    await page.reload();

    await expect(page.locator("#worker-item-title")).toHaveText("Worker B");
    await expect(page.locator(".worker-item-page__breadcrumb")).toContainText(
      "G4 cross-agent handoff control room / Subtask B",
    );
    const progress = page.getByRole("region", { name: "Agent 切换进度" });
    await expect(progress).toContainText("目标 Agent 启动结果未知");
    const recovery = progress.getByRole("region", { name: "恢复这次切换" });
    await expect(recovery.getByRole("radio", { name: "确认目标尚未启动" })).toBeVisible();
    await expect(recovery.getByRole("radio")).toHaveCount(4);
    await recovery.getByRole("radio", { name: "确认目标尚未启动" }).check();
    await recovery.getByRole("textbox", { name: "恢复原因" }).fill("已确认目标 Agent 尚未启动");
    await expect(recovery.getByRole("textbox", { name: "证据引用" })).toHaveValue(/handoff-recovery:\/\//);
    await recovery.getByRole("button", { name: "提交恢复策略" }).click();
    await expect(recovery).toContainText("恢复请求已记录");

    const runtimeSummary = runHandoffWorkerOnce(seed.workspace_id, "normal");
    expect(runtimeSummary.transition_status).toBe("converged");
    await page.reload();
    await expect(page.getByRole("complementary", { name: "WorkerItem 事实" })).toContainText("Target Agent One");
    await expect(page.getByRole("textbox", { name: "介入这个 Worker" })).toBeEnabled();
    await assertNoHorizontalOverflow(page);
  });
});

async function prepareAndConfirm(page: Page): Promise<void> {
  const adjustmentButton = page.getByRole("button", { name: "调整执行 Agent" });
  await expect(adjustmentButton).toBeVisible();
  await adjustmentButton.click();
  const dialog = page.getByRole("dialog", { name: "调整执行 Agent" });
  await expect(dialog.getByRole("radio", { name: "选择 Target Agent One" })).toBeChecked();
  await dialog.getByRole("textbox", { name: "切换原因" }).fill("验证未知启动后的安全恢复");
  await dialog.getByRole("textbox", { name: "交接指令" }).fill("保留已有上下文并等待恢复后继续");
  await dialog.getByRole("button", { name: "查看切换方案" }).click();
  const review = page.getByRole("dialog", { name: "确认 Agent 切换" });
  await expect(review).toBeVisible();
  await review.getByRole("button", { name: "确认切换" }).click();
  await expect(page.getByRole("dialog", { name: "Agent 切换进度" })).toBeVisible();
}

async function updatePendingInput(
  page: Page,
  seed: R6HandoffSeed,
  sessionVersion: number,
): Promise<void> {
  const response = await page.request.post("/api/v1/commands", {
    data: {
      command_id: randomUUID(),
      workspace_id: seed.workspace_id,
      command_type: "UpdatePendingExecutionInput",
      actor: { type: "local", id: "aitejiu" },
      target_type: "execution_session",
      target_id: seed.source_session_id,
      expected_version: sessionVersion,
      idempotency_key: `r6-2-browser-update:${randomUUID()}`,
      correlation_id: randomUUID(),
      payload: {
        execution_id: seed.source_execution_id,
        client_idempotency_key: "queued-source-input",
        text: "Transfer this queued instruction after the browser revised it.",
      },
    },
  });
  if (!response.ok()) {
    throw new Error(`updating the G4 queued input failed with ${response.status()}`);
  }
}

async function assertNoHorizontalOverflow(page: Page): Promise<void> {
  const dimensions = await page.evaluate(() => ({
    documentWidth: document.documentElement.scrollWidth,
    viewportWidth: document.documentElement.clientWidth,
  }));
  expect(dimensions.documentWidth).toBeLessThanOrEqual(dimensions.viewportWidth + 1);
}
