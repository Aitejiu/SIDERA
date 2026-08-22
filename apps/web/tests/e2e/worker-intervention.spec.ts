import type { Page } from "@playwright/test";

import { test, expect, IMPLEMENTER_WORKER_ITEM_ID, WORKSPACE_ID } from "./fixtures";

test.describe("exact worker intervention", () => {
  test.beforeEach(({ seedR4 }) => {
    seedR4();
  });

  test("queues input against the exact WorkerItem Session address", async ({ page }) => {
    await openWorker(page);

    await page.getByText("查看精确 context", { exact: true }).click();
    const address = page.locator("details").filter({ hasText: "查看精确 context" });
    await expect(address).toContainText("WorkerItem");
    await expect(address).toContainText("Plan generation");
    await expect(address).toContainText("Execution");
    await expect(address).toContainText("Session");
    await expect(address).toContainText("Worker");

    await page.getByRole("textbox", { name: "介入这个 Worker" }).fill("继续验证回调 checkpoint");
    await page.getByRole("button", { name: "发送到当前 Session" }).click();
    await expect(page.getByText("已接受", { exact: true })).toBeVisible();
    await expect(page.getByRole("textbox", { name: "介入这个 Worker" })).toHaveValue("");
    await expect(page.getByText("继续验证回调 checkpoint", { exact: true })).toBeVisible();
  });

  test("stops only the exact Execution after explicit confirmation", async ({ page, request }) => {
    void request;
    await openWorker(page);

    page.once("dialog", (dialog) => void dialog.accept());
    await page.getByRole("button", { name: "停止 Execution" }).click();
    await expect(page.getByText("停止已请求", { exact: true })).toBeVisible();
  });

  test("preserves the draft when the execution context changes", async ({ page, request }) => {
    void request;
    await openWorker(page);
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
    await page.getByRole("textbox", { name: "介入这个 Worker" }).fill(draft);
    await page.getByRole("button", { name: "发送到当前 Session" }).click();

    await expect(page.getByRole("alert")).toContainText("上下文已变化");
    await expect(page.getByRole("textbox", { name: "介入这个 Worker" })).toHaveValue(draft);
    await expect(page.getByText("请刷新后确认当前 Session", { exact: false })).toBeVisible();
  });
});

async function openWorker(page: Page): Promise<void> {
  await page.goto(`/workspaces/${WORKSPACE_ID}/worker-items/${IMPLEMENTER_WORKER_ITEM_ID}`);
  await expect(page.locator("#worker-item-title")).toHaveText("实现者");
  await expect(page.getByRole("textbox", { name: "介入这个 Worker" })).toBeVisible();
}
