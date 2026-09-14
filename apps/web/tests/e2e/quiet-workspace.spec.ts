import { AxeBuilder } from "@axe-core/playwright";
import type { Page } from "@playwright/test";

import { test, expect, WORKSPACE_ID, ROOT_TASK_ID, IMPLEMENTER_WORKER_ITEM_ID } from "./fixtures";

const taskPath = `/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}`;

test.describe("quiet workspace visual contract", () => {
  test.beforeEach(({ seedR4 }) => seedR4());

  test("puts the task board in the first viewport and reveals auxiliary facts on demand", async ({ page }) => {
    await page.goto(taskPath);
    const card = page.getByRole("button", { name: "打开子任务：回调测试" });
    await expect(card).toBeVisible();
    await expect(card).toBeInViewport({ ratio: 1 });
    await expect(page.getByRole("group", { name: "任务进度" })).toContainText("已完成 0 / 3");
    await expect(page.getByRole("button", { name: "确认已处理" })).toBeInViewport();
    await expect(page.getByRole("heading", { name: "里程碑", exact: true })).toBeHidden();
    await page.getByRole("button", { name: "查看任务详情" }).click();
    await expect(page.getByRole("button", { name: "查看任务详情" })).toHaveAttribute("aria-expanded", "true");
    await expect(page.getByRole("heading", { name: "里程碑", exact: true })).toBeVisible();
    await expect(page.getByRole("region", { name: "知识上下文" })).toBeVisible();
    await page.locator(".task-details > summary").press("Enter");
    await expect(page.getByRole("heading", { name: "里程碑", exact: true })).toBeHidden();
    await expect(page.getByRole("button", { name: "查看任务详情" })).toHaveAttribute("aria-expanded", "false");
    await page.getByRole("tab", { name: "DAG", exact: true }).click();
    await expect(page.locator(".task-flow-node")).toHaveCount(3);
    await noPageOverflow(page);
  });

  test("keeps long task titles readable and all five state columns horizontally aligned", async ({ page }) => {
    await page.goto(taskPath);
    const title = page.locator(".task-board-card > strong").first();
    await expect(title).toBeVisible();
    await expect(page.locator(".task-board__column--attention .status-mark")).toHaveText("待你处理");
    await title.evaluate(element => { element.textContent = "支付回调并发测试与异常恢复验证"; });
    const type = await title.evaluate(element => {
      const style = getComputedStyle(element);
      return { size: parseFloat(style.fontSize), wrap: style.whiteSpace, clipped: element.scrollWidth > element.clientWidth + 1 };
    });
    expect(type.size).toBeGreaterThanOrEqual(14);
    expect(type.wrap).not.toBe("nowrap");
    expect(type.clipped).toBe(false);
    const pageTitle = page.locator("#task-detail-title");
    await pageTitle.evaluate(element => {
      element.textContent = "PaymentCallbackIdempotencyAndRecoveryObservabilityAcceptance";
    });
    expect(await pageTitle.evaluate(element => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
    const columns = await page.locator(".task-board__column").evaluateAll(elements => elements.map(element => element.getBoundingClientRect().top));
    expect(columns).toHaveLength(5);
    expect(Math.max(...columns) - Math.min(...columns)).toBeLessThan(2);
    await noPageOverflow(page);
  });

  test("opens the same subtask and worker from graph buttons using the keyboard", async ({ page }) => {
    await page.goto(`${taskPath}?view=dag`);
    const button = page.locator("button.task-flow-node").filter({ hasText: "回调幂等" });
    await expect(button).toBeVisible();
    await button.focus();
    await page.keyboard.press("Enter");
    await expect(page.locator("#subtask-title")).toHaveText("回调幂等");
    await page.getByRole("tab", { name: "Graph", exact: true }).click();
    const worker = page.locator(".worker-flow-node").filter({ hasText: "实现者" });
    await expect(worker).toBeVisible();
    await worker.focus();
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(new RegExp(`/worker-items/${IMPLEMENTER_WORKER_ITEM_ID}$`));
  });

  test("keeps worker metadata labels intact beside a long agent name", async ({ page }) => {
    await page.goto(`${taskPath}/subtasks/callback-idempotency`);
    const metadata = page.locator(".worker-board-card__meta").first();
    await expect(metadata).toBeVisible();
    await metadata.locator("span:last-child").evaluate(element => {
      element.textContent = "OpenCode Callback Implementation Worker";
    });
    const geometry = await metadata.evaluate(element => {
      const label = element.querySelector("span:first-child")!;
      const value = element.querySelector("span:last-child")!;
      return {
        labelHeight: label.getBoundingClientRect().height,
        lineHeight: parseFloat(getComputedStyle(label).lineHeight),
        labelRight: label.getBoundingClientRect().right,
        valueLeft: value.getBoundingClientRect().left,
        fits: element.scrollWidth <= element.clientWidth + 1,
      };
    });
    expect(geometry.labelHeight).toBeLessThanOrEqual(geometry.lineHeight + 1);
    expect(geometry.labelRight).toBeLessThanOrEqual(geometry.valueLeft);
    expect(geometry.fits).toBe(true);
  });

  test("keeps the active workspace tab visible and returns correctly after closing it", async ({ page }) => {
    await page.goto(taskPath);
    await page.getByRole("button", { name: "打开子任务：回调幂等" }).click();
    await page.getByRole("button", { name: "打开 Worker：实现者" }).click();
    await expect(page.locator("#worker-item-title")).toHaveText("实现者");
    await expect.poll(() => page.locator(".workspace-tab.is-active").evaluate(tab => {
      const bounds = tab.getBoundingClientRect();
      const strip = tab.closest(".workspace-tabs")!.getBoundingClientRect();
      return bounds.left >= strip.left - 1 && bounds.right <= strip.right + 1;
    })).toBe(true);
    await page.locator(".workspace-tab.is-active .workspace-tab__close").click();
    await expect(page.locator("#subtask-title")).toHaveText("回调幂等");
    await page.locator(".workspace-tab.is-active .workspace-tab__close").click();
    await expect(page.locator("#task-detail-title")).toHaveText("支付重构");
    await noPageOverflow(page);
  });

  test("preserves contrast and geometry across board, graph and conversation surfaces", async ({ page }, testInfo) => {
    const capture = async (name: string) => {
      await page.evaluate(() => window.scrollTo(0, 0));
      await noPageOverflow(page);
      const result = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
      expect.soft(result.violations, name).toEqual([]);
      if (name === "worker-workspace") {
        const timeline = await page.locator(".worker-timeline").boundingBox();
        const composer = await page.locator(".worker-composer").boundingBox();
        expect.soft(composer!.y, "composer must not cover the working record").toBeGreaterThanOrEqual(timeline!.y + timeline!.height);
      }
      await page.screenshot({ path: testInfo.outputPath(`${name}.png`), fullPage: true, animations: "disabled" });
    };
    await page.goto(taskPath);
    await expect(page.locator(".task-board-card")).toHaveCount(3);
    await capture("task-board");
    await page.getByRole("button", { name: "查看任务详情" }).click();
    await expect(page.getByRole("heading", { name: "里程碑", exact: true })).toBeVisible();
    await capture("task-details");
    await page.getByText("任务详情", { exact: true }).click();
    await page.getByRole("tab", { name: "DAG", exact: true }).click();
    await expect(page.locator(".task-flow-node")).toHaveCount(3);
    await capture("task-dag");
    await page.goto(`${taskPath}/subtasks/callback-idempotency`);
    await expect(page.locator(".worker-board-card")).toHaveCount(3);
    await capture("worker-board");
    await page.getByRole("tab", { name: "Graph", exact: true }).click();
    await expect(page.locator(".worker-flow-node")).toHaveCount(3);
    await capture("worker-graph");
    await page.goto(`/workspaces/${WORKSPACE_ID}/worker-items/${IMPLEMENTER_WORKER_ITEM_ID}`);
    await expect(page.locator("#worker-item-title")).toHaveText("实现者");
    await expect(page.getByRole("textbox", { name: "介入这个 Worker" })).toBeVisible();
    await capture("worker-workspace");
  });

  test("keeps multiline activity entries from covering the next step", async ({ page }) => {
    await page.goto(`/workspaces/${WORKSPACE_ID}/worker-items/${IMPLEMENTER_WORKER_ITEM_ID}`);
    const rows = page.locator(".worker-timeline-item");
    await expect(rows.first()).toBeVisible();
    await rows.first().locator("strong").first().evaluate(element => {
      element.textContent = "等待支付回调权限确认，需要保留当前测试证据，并检查重复通知、超时恢复与退款失败的处理结果。".repeat(4);
    });
    await expect.poll(() => rows.evaluateAll(elements => {
      const bounds = elements.map(element => element.getBoundingClientRect()).sort((a, b) => a.top - b.top);
      return bounds.every((box, index) => index === 0 || box.top >= bounds[index - 1]!.bottom - 1);
    })).toBe(true);
  });
});

async function noPageOverflow(page: Page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
}
