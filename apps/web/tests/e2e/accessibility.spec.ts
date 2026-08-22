import { randomUUID } from "node:crypto";

import { AxeBuilder } from "@axe-core/playwright";
import type { Page } from "@playwright/test";

import { test, expect, EXECUTION_ID, ROOT_TASK_ID, SESSION_ID, WORKSPACE_ID } from "./fixtures";

test.describe("accessible R4 routes", () => {
  test.beforeEach(({ seedR4 }) => {
    seedR4();
  });

  test("has no axe violations on task, plan, settings, and trace surfaces", async ({ page }) => {
    await page.goto(`/workspaces/${WORKSPACE_ID}/tasks`);
    await expect(page.getByRole("heading", { name: "任务", exact: true })).toBeVisible();
    await audit(page, "task list");

    await page.getByRole("link", { name: "打开任务 R4 control room delivery" }).click();
    await expect(page.locator("#task-detail-title")).toBeVisible();
    await page.getByRole("tab", { name: "DAG", exact: true }).click();
    await expect(page.locator(".edge-summary")).toHaveCount(2);
    await audit(page, "task detail");

    await page.getByRole("link", { name: /计划历史/ }).click();
    await expect(page.locator("#plan-generation-title")).toBeVisible();
    await audit(page, "plan history");

    await page.getByRole("link", { name: "比较当前" }).click();
    await expect(page.locator("#plan-diff-title")).toBeVisible();
    await audit(page, "plan diff");

    await page.goto(`/workspaces/${WORKSPACE_ID}/settings`);
    await expect(page.locator("#notification-settings-title")).toBeVisible();
    await audit(page, "settings");

    await page.goto(`/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}/executions/${EXECUTION_ID}/sessions/${SESSION_ID}/trace`);
    await expect(page.locator("#worker-trace-title")).toBeVisible();
    await expect(page.locator(".trace-row")).toHaveCount(3);
    await audit(page, "worker trace");
  });

  test("reaches task selection, trace details, Send, and Stop by keyboard", async ({ page, request }) => {
    await page.goto(`/workspaces/${WORKSPACE_ID}/tasks`);
    const taskLink = page.getByRole("link", { name: "打开任务 R4 control room delivery" });
    await expect(taskLink).toBeVisible();
    const wakeResponse = await request.post("/api/v1/commands", {
      data: {
        command_id: randomUUID(),
        workspace_id: WORKSPACE_ID,
        command_type: "SetTaskPriority",
        target_type: "task",
        actor: { type: "local", id: "aitejiu" },
        target_id: ROOT_TASK_ID,
        expected_version: 1,
        idempotency_key: `e2e-keyboard-wake-${randomUUID()}`,
        correlation_id: randomUUID(),
        payload: { priority: 51 },
      },
    });
    expect(wakeResponse.ok()).toBeTruthy();
    await expect(page.locator(".topbar .status-mark--live")).toBeVisible();

    await taskLink.focus();
    await page.keyboard.press("Enter");
    await expect(page.locator("#task-detail-title")).toBeVisible();

    const ledgerTab = page.getByRole("tab", { name: "任务表", exact: true });
    await ledgerTab.focus();
    await page.keyboard.press("Enter");
    await expect(page.getByRole("heading", { name: "任务表", exact: true })).toBeVisible();

    const backendRow = page.getByRole("button", { name: "在任务表中选择Backend execution" });
    await backendRow.focus();
    await page.keyboard.press("Enter");
    await expect(page.getByRole("heading", { name: "Backend execution", exact: true })).toBeVisible();

    const executionLink = page.getByRole("link", { name: /查看执行过程/ });
    await executionLink.focus();
    await page.keyboard.press("Enter");
    await expect(page.locator("#execution-page-title")).toHaveText("执行步骤");
    const traceWakeResponse = await request.post("/api/v1/commands", {
      data: {
        command_id: randomUUID(),
        workspace_id: WORKSPACE_ID,
        command_type: "SetTaskPriority",
        target_type: "task",
        actor: { type: "local", id: "aitejiu" },
        target_id: ROOT_TASK_ID,
        expected_version: 2,
        idempotency_key: `e2e-keyboard-trace-wake-${randomUUID()}`,
        correlation_id: randomUUID(),
        payload: { priority: 52 },
      },
    });
    expect(traceWakeResponse.ok()).toBeTruthy();
    await expect(page.locator(".topbar .status-mark--live")).toBeVisible();

    const traceRow = page.locator(".trace-row").first();
    await traceRow.focus();
    await page.keyboard.press("Enter");
    await expect(page.locator("#trace-details-title")).toBeVisible();

    const input = page.getByLabel("介入内容");
    await input.focus();
    await input.fill("通过键盘验证精确 Session");
    const send = page.getByRole("button", { name: "发送到当前 Session" });
    await send.focus();
    await page.keyboard.press("Enter");
    await expect(page.getByLabel("介入当前 Session").getByText("已排队", { exact: true })).toBeVisible();

    page.once("dialog", (dialog) => void dialog.accept());
    const stop = page.getByRole("button", { name: "停止 Execution" });
    await stop.focus();
    await page.keyboard.press("Enter");
    await expect(page.getByText("停止已请求", { exact: true })).toBeVisible();
  });
});

async function audit(page: Page, surface: string): Promise<void> {
  const result = await new AxeBuilder({ page }).analyze();
  expect(result.violations, `${surface} has accessibility violations`).toEqual([]);
}
