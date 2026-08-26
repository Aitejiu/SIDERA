import type { Page } from "@playwright/test";

import {
  CONTEXT_EXECUTION_PACKAGE_ID,
  CONTEXT_PLANNING_PACKAGE_ID,
  CONTEXT_PLANNING_REQUEST_ID,
  expect,
  IMPLEMENTER_WORKER_ITEM_ID,
  ROOT_TASK_ID,
  test,
  WORKSPACE_ID,
} from "./fixtures";

test.describe("Context Package audit workflow", () => {
  test.beforeEach(({ seedR4, seedKnowledge }) => {
    seedR4("context-knowledge");
    seedKnowledge();
  });

  test("opens the execution package from WorkerItem, materializes explicitly, and keeps trace independent", async ({ page }, testInfo) => {
    await page.goto(`/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}`);
    await expect(page.locator("#task-detail-title")).toHaveText("支付重构");
    await expect(page.getByRole("heading", { name: "知识上下文" })).toBeVisible();

    await page.getByRole("button", { name: "打开子任务：回调幂等" }).click();
    await page.getByRole("button", { name: "打开 Worker：实现者" }).click();
    await expect(page).toHaveURL(new RegExp(`/worker-items/${IMPLEMENTER_WORKER_ITEM_ID}$`));
    await expect(page.locator("#worker-item-title")).toHaveText("实现者");
    await expect(page.getByRole("region", { name: "WorkerItem 上下文" })).toBeVisible();

    await page.getByRole("link", { name: "查看本次运行上下文：执行尝试 #1" }).click();
    await expect(page).toHaveURL(new RegExp(`/context-packages/${CONTEXT_EXECUTION_PACKAGE_ID}$`));
    await expect(page.locator("#context-package-title")).toHaveText("上下文包");
    await expect(page.getByText("BODY NOT LOADED", { exact: true })).toBeVisible();
    await expect(page.getByText("Package active", { exact: true })).toBeVisible();
    await expect(page.getByText("The worker receives an exact immutable context package and keeps the selected revision hash.", { exact: true })).toHaveCount(0);

    const materialize = page.getByRole("button", { name: "查看物化内容" });
    await materialize.focus();
    await expect(materialize).toBeFocused();
    await materialize.click();
    await expect(page.getByText("The worker receives an exact immutable context package and keeps the selected revision hash.", { exact: true })).toBeVisible();
    await expect(page.getByText("context-package.md", { exact: true })).toBeVisible();
    await expect(page.getByText("已确认", { exact: true }).first()).toBeVisible();

    await assertResponsive(page);
    await page.screenshot({ path: testInfo.outputPath("context-package-materialized.png"), fullPage: true });

    await page.getByRole("button", { name: "关闭上下文包" }).click();
    await expect(page).toHaveURL(new RegExp(`/worker-items/${IMPLEMENTER_WORKER_ITEM_ID}$`));
    await expect(page.locator("#worker-item-title")).toHaveText("实现者");

    const trace = page.getByRole("button", { name: "查看 Agent Trace" }).first();
    await trace.focus();
    await expect(trace).toBeFocused();
    await trace.click();
    await expect(page.getByRole("dialog", { name: "Agent Trace" })).toBeVisible();
    await page.getByRole("button", { name: "关闭 Agent Trace" }).click();
    await expect(page.getByRole("dialog", { name: "Agent Trace" })).toHaveCount(0);
  });

  test("opens the planning-run package and returns to the planning tab", async ({ page }, testInfo) => {
    await page.goto(`/workspaces/${WORKSPACE_ID}/planning/${CONTEXT_PLANNING_REQUEST_ID}`);
    await expect(page.locator("#planning-request-title")).toHaveText("规划请求");

    const runContext = page.getByRole("link", { name: "查看本次运行上下文" });
    await expect(runContext).toBeVisible();
    await runContext.click();
    await expect(page).toHaveURL(new RegExp(`/context-packages/${CONTEXT_PLANNING_PACKAGE_ID}$`));
    await expect(page.getByText("Package active", { exact: true })).toBeVisible();
    await expect(page.getByText(/planner b0000000/)).toBeVisible();
    await expect(page.getByText("BODY NOT LOADED", { exact: true })).toBeVisible();

    const tabs = page.getByRole("navigation", { name: "已打开的任务" });
    await expect(tabs.getByRole("link", { name: "上下文包" })).toBeVisible();
    await expect(tabs.getByRole("link", { name: new RegExp(`规划 ${CONTEXT_PLANNING_REQUEST_ID.slice(0, 8)}`) })).toBeVisible();

    await assertResponsive(page);
    await page.screenshot({ path: testInfo.outputPath("planning-context-package.png"), fullPage: true });

    await page.getByRole("button", { name: "关闭上下文包" }).click();
    await expect(page).toHaveURL(new RegExp(`/planning/${CONTEXT_PLANNING_REQUEST_ID}$`));
    await expect(page.locator("#planning-request-title")).toHaveText("规划请求");
  });
});

async function assertResponsive(page: Page): Promise<void> {
  const dimensions = await page.evaluate(() => ({
    documentWidth: document.documentElement.scrollWidth,
    viewportWidth: window.innerWidth,
  }));
  expect(dimensions.documentWidth).toBeLessThanOrEqual(dimensions.viewportWidth + 1);
}
