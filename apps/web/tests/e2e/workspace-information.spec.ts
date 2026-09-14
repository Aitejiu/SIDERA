import { AxeBuilder } from "@axe-core/playwright";

import { test, expect, WORKSPACE_ID, ROOT_TASK_ID, IMPLEMENTER_WORKER_ITEM_ID } from "./fixtures";

const rootPath = `/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}`;
const subtaskPath = `${rootPath}/subtasks/callback-idempotency`;
const workerPath = `/workspaces/${WORKSPACE_ID}/worker-items/${IMPLEMENTER_WORKER_ITEM_ID}`;

test.describe("workspace information hierarchy", () => {
  test.beforeEach(({ seedR4 }) => seedR4());

  test("opens attention content in the existing worker tab without confirming it", async ({ page }) => {
    let acknowledgementCount = 0;
    page.on("request", request => {
      if (request.method() === "POST" && request.url().endsWith("/api/v1/commands")
        && request.postDataJSON()?.command_type === "AcknowledgeAttentionItem") acknowledgementCount += 1;
    });
    await page.route(`**/api/v1/tasks/${ROOT_TASK_ID}/graph?*`, async route => {
      const response = await route.fetch();
      const graph = await response.json();
      graph.overview.attention_items = [{ ...graph.overview.attention_items[0],
        worker_item_id: IMPLEMENTER_WORKER_ITEM_ID, deep_link: null,
        subtask_key: "callback-idempotency", subtask_title: "回调幂等", reason: "请确认回调测试范围" }];
      await route.fulfill({ response, json: graph });
    });
    await page.goto(rootPath);
    const attention = page.getByRole("region", { name: "需要我", exact: true });
    await attention.getByText("请确认回调测试范围", { exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`${IMPLEMENTER_WORKER_ITEM_ID}$`));
    await expect(page.locator("#worker-item-title")).toHaveText("实现者");
    await page.getByRole("textbox", { name: "介入这个 Worker" }).fill("保留未发送的草稿");
    const tabs = page.getByRole("navigation", { name: "已打开的任务" });
    await tabs.getByRole("link", { name: "支付重构", exact: true }).click();
    await expect(attention.getByRole("button", { name: "确认已处理" })).toBeVisible();
    await attention.getByRole("link", { name: /支付重构 \/ 回调幂等/ }).press("Enter");
    await expect(page.getByRole("textbox", { name: "介入这个 Worker" })).toHaveValue("保留未发送的草稿");
    await expect(tabs.locator(`a[href="${workerPath}"]`)).toHaveCount(1);
    expect(acknowledgementCount).toBe(0);
  });

  test("puts the worker board before auxiliary facts and preserves details across tabs", async ({ page }, testInfo) => {
    await page.goto(subtaskPath);
    const card = page.getByRole("button", { name: "打开 Worker：实现者" });
    await expect(card).toBeVisible();
    if (testInfo.project.name === "mobile-chromium") {
      const bounds = await card.boundingBox();
      expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(page.viewportSize()!.height);
      await expect(page.locator(".worker-board__column").first()).toBeInViewport();
    } else {
      await expect(card).toBeInViewport({ ratio: 0.9 });
    }
    await expect(page.getByRole("complementary", { name: "子任务摘要" })).toBeHidden();
    await expect(page.getByRole("navigation", { name: "子任务依赖关系" })).toBeHidden();
    await expect(page.getByRole("region", { name: "知识上下文" })).toBeHidden();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    expect((await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze()).violations).toEqual([]);
    await page.screenshot({ path: testInfo.outputPath("subtask-first-viewport.png"), animations: "disabled" });
    await page.getByRole("button", { name: "查看子任务详情" }).click();
    await expect(page.getByRole("navigation", { name: "子任务依赖关系" })).toBeVisible();
    await expect(page.getByRole("region", { name: "知识上下文" })).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath("subtask-details.png"), fullPage: true, animations: "disabled" });
    await card.click();
    await expect(page.locator("#worker-item-title")).toHaveText("实现者");
    const tabs = page.getByRole("navigation", { name: "已打开的任务" });
    await tabs.locator(`a[href="${subtaskPath}"]`).click();
    await expect(page.getByRole("button", { name: "查看子任务详情" })).toHaveAttribute("aria-expanded", "true");
    await page.locator(".subtask-details > summary").click();
    await page.getByRole("tab", { name: "Graph", exact: true }).click();
    await expect(page.locator(".worker-flow-node")).toHaveCount(3);
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({ path: testInfo.outputPath("subtask-graph.png"), animations: "disabled" });
  });
});
