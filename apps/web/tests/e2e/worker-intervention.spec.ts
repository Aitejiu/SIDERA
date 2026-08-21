import { randomUUID } from "node:crypto";

import type { APIRequestContext, Page } from "@playwright/test";

import { test, expect, EXECUTION_ID, ROOT_TASK_ID, SESSION_ID, WORKSPACE_ID } from "./fixtures";

test.describe("exact worker intervention", () => {
  test.beforeEach(({ seedR4 }) => {
    seedR4();
  });

  test("queues input against the exact Session address", async ({ page, request }) => {
    await openTrace(page, request);

    const address = page.getByRole("group", { name: "精确执行地址" });
    await expect(address).toBeVisible();
    await expect(address.getByText("Plan", { exact: true })).toBeVisible();
    await expect(address.getByText("WorkItem", { exact: true })).toBeVisible();
    await expect(address.getByText("Session", { exact: true })).toBeVisible();

    await page.getByLabel("介入内容").fill("继续验证 backend checkpoint");
    await page.getByRole("button", { name: "发送到当前 Session" }).click();
    await expect(page.getByText("已排队", { exact: true })).toBeVisible();
    await expect(page.getByLabel("介入内容")).toHaveValue("");
  });

  test("stops only the exact Execution after explicit confirmation", async ({ page, request }) => {
    await openTrace(page, request);

    page.once("dialog", (dialog) => void dialog.accept());
    await page.getByRole("button", { name: "停止 Execution" }).click();
    await expect(page.getByText("停止已请求", { exact: true })).toBeVisible();
  });

  test("preserves the draft when the execution context changes", async ({ page, request }) => {
    await openTrace(page, request);
    await page.route("**/api/v1/commands", async (route) => {
      if (route.request().method() !== "POST") {
        await route.continue();
        return;
      }
      await route.fulfill({
        status: 409,
        contentType: "application/json",
        headers: { "X-Correlation-Id": "e2e-context-change" },
        body: JSON.stringify({
          error: {
            code: "execution_context_changed",
            message: "Execution context changed",
            details: { changed_fields: ["session_id"] },
          },
        }),
      });
    });

    const draft = "保留这段输入，不要自动切换到后继 Session";
    await page.getByLabel("介入内容").fill(draft);
    await page.getByRole("button", { name: "发送到当前 Session" }).click();

    await expect(page.getByRole("alert")).toContainText("执行地址已变化");
    await expect(page.getByLabel("介入内容")).toHaveValue(draft);
    await expect(page.getByText("没有自动切换目标", { exact: false })).toBeVisible();
  });
});

async function openTrace(page: Page, request: APIRequestContext): Promise<void> {
  await page.goto(`/workspaces/${WORKSPACE_ID}/tasks`);
  await expect(page.getByRole("link", { name: "打开任务 R4 control room delivery" })).toBeVisible();
  const wakeResponse = await request.post("/api/v1/commands", {
    data: {
      command_id: randomUUID(),
      workspace_id: WORKSPACE_ID,
      command_type: "SetTaskPriority",
      target_type: "task",
      actor: { type: "local", id: "aitejiu" },
      target_id: ROOT_TASK_ID,
      expected_version: 1,
      idempotency_key: `e2e-wake-${randomUUID()}`,
      correlation_id: randomUUID(),
      payload: { priority: 51 },
    },
  });
  if (!wakeResponse.ok()) {
    throw new Error(`failed to wake projection stream: ${wakeResponse.status()}`);
  }
  await expect(page.locator(".topbar .status-mark--live")).toBeVisible();
  await page.getByRole("link", { name: "打开任务 R4 control room delivery" }).click();
  await page.getByRole("tab", { name: "任务表", exact: true }).click();
  await page.getByRole("link", { name: /Execution 60000000/ }).click();
  await expect(page.locator("#worker-trace-title")).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`${EXECUTION_ID}/sessions/${SESSION_ID}/trace$`));
  await expect(page.locator(".trace-row")).toHaveCount(3);
  await expect(page.locator("textarea[aria-label='介入内容']")).toBeVisible();
}
