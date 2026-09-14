import { test, expect, WORKSPACE_ID, ROOT_TASK_ID, SECOND_ROOT_TASK_ID, IMPLEMENTER_WORKER_ITEM_ID, TESTER_WORKER_ITEM_ID } from "./fixtures";

const rootPath = `/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}`;
const workerPath = `/workspaces/${WORKSPACE_ID}/worker-items/${IMPLEMENTER_WORKER_ITEM_ID}`;

test.describe("workspace tab continuity", () => {
  test.beforeEach(({ seedR4 }) => seedR4());

  test("labels each workspace with its own name and full ownership path", async ({ page }) => {
    await page.goto(rootPath);
    await page.getByRole("button", { name: "打开子任务：回调幂等" }).click();
    await expect(page.locator("#subtask-title")).toHaveText("回调幂等");
    const tabs = page.getByRole("navigation", { name: "已打开的任务" });
    await expect(tabs.getByRole("link", { name: "回调幂等", exact: true })).toHaveAttribute("title", "子任务 · 支付重构 / 回调幂等");
    await page.getByRole("button", { name: "打开 Worker：实现者" }).click();
    await expect(page.locator("#worker-item-title")).toHaveText("实现者");
    await expect(tabs.getByRole("link", { name: "实现者", exact: true })).toHaveAttribute("title", "WorkerItem · 支付重构 / 回调幂等 / 实现者");
    await expect(tabs.getByRole("link", { name: "支付重构", exact: true })).toHaveCount(1);
    await page.reload();
    await expect(tabs.getByRole("link", { name: "实现者", exact: true })).toHaveAttribute("aria-current", "page");
  });

  test("restores the root view, details and reading position independently of another task", async ({ page }) => {
    await page.goto(`${rootPath}?view=dag`);
    await expect(page.locator(".task-flow-node")).toHaveCount(3);
    await page.getByRole("button", { name: "查看任务详情" }).click();
    await expect(page.locator(".task-details")).toHaveAttribute("open", "");
    await page.evaluate(() => window.scrollTo(0, 400));
    await expect.poll(() => page.evaluate(() => scrollY)).toBe(400);
    const tabs = page.getByRole("navigation", { name: "已打开的任务" });
    await tabs.getByRole("link", { name: "任务总览" }).click();
    await page.getByRole("link", { name: "打开任务 发布监控升级" }).click();
    await expect(page.locator("#task-detail-title")).toHaveText("发布监控升级");
    await expect.poll(() => page.evaluate(() => scrollY)).toBe(0);
    await expect(page.locator(".task-details")).not.toHaveAttribute("open", "");
    await tabs.getByRole("link", { name: "支付重构", exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`${ROOT_TASK_ID}\\?view=dag$`));
    await expect(page.locator(".task-details")).toHaveAttribute("open", "");
    await expect.poll(() => page.evaluate(() => scrollY)).toBe(400);
    await expect(page.getByRole("tab", { name: "DAG", exact: true })).toHaveAttribute("aria-selected", "true");
  });

  test("restores the worker reading position without changing the subtask view", async ({ page }) => {
    await page.goto(`${rootPath}/subtasks/callback-idempotency?view=graph`);
    await page.locator("button.worker-flow-node").filter({ hasText: "实现者" }).click();
    await expect(page).toHaveURL(new RegExp(`${IMPLEMENTER_WORKER_ITEM_ID}$`));
    const timeline = page.getByRole("region", { name: "WorkerItem 活动时间线" });
    await expect(timeline).toBeVisible();
    await timeline.evaluate(element => { element.scrollTop = 80; });
    await expect.poll(() => timeline.evaluate(element => element.scrollTop)).toBe(80);
    const tabs = page.getByRole("navigation", { name: "已打开的任务" });
    await tabs.locator(`a[href="${rootPath}/subtasks/callback-idempotency?view=graph"]`).click();
    await expect(page.getByRole("tab", { name: "Graph", exact: true })).toHaveAttribute("aria-selected", "true");
    await tabs.locator(`a[href="${workerPath}"]`).click();
    await expect.poll(() => timeline.evaluate(element => element.scrollTop)).toBe(80);
    await page.getByRole("button", { name: "系统记录" }).click();
    await expect(page.getByRole("button", { name: "系统记录" })).toHaveAttribute("aria-pressed", "true");
    await timeline.evaluate(element => { element.scrollTop = 120; });
    await expect.poll(() => timeline.evaluate(element => element.scrollTop)).toBe(120);
    await tabs.locator(`a[href="${rootPath}/subtasks/callback-idempotency?view=graph"]`).click();
    await tabs.locator(`a[href="${workerPath}"]`).click();
    await expect(page.getByRole("button", { name: "系统记录" })).toHaveAttribute("aria-pressed", "true");
    await expect.poll(() => timeline.evaluate(element => element.scrollTop)).toBe(120);
  });

  test("discards a closed tasks view state without disrupting the active task", async ({ page }) => {
    await page.goto(rootPath);
    await page.getByRole("button", { name: "查看任务详情" }).click();
    const tabs = page.getByRole("navigation", { name: "已打开的任务" });
    await tabs.getByRole("link", { name: "任务总览" }).click();
    await page.getByRole("link", { name: "打开任务 发布监控升级" }).click();
    await expect(page.locator("#task-detail-title")).toHaveText("发布监控升级");
    await tabs.getByRole("button", { name: "关闭支付重构", exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`${SECOND_ROOT_TASK_ID}$`));
    await tabs.getByRole("link", { name: "任务总览" }).click();
    await page.getByRole("link", { name: "打开任务 支付重构" }).click();
    await expect(page.locator(".task-details")).not.toHaveAttribute("open", "");
  });

  test("keeps cached worker tabs drafts and record filters independent", async ({ page }) => {
    const subtaskPath = `${rootPath}/subtasks/callback-idempotency`;
    await page.goto(subtaskPath);
    await page.getByRole("button", { name: "打开 Worker：实现者" }).click();
    const input = page.getByRole("textbox", { name: "介入这个 Worker" });
    await input.fill("实现者的本地草稿");
    await page.getByRole("button", { name: "系统记录" }).click();
    const tabs = page.getByRole("navigation", { name: "已打开的任务" });
    await tabs.locator(`a[href="${subtaskPath}"]`).click();
    await page.getByRole("button", { name: "打开 Worker：测试者" }).click();
    await expect(page).toHaveURL(new RegExp(`${TESTER_WORKER_ITEM_ID}$`));
    await expect(input).toHaveValue("");
    await expect(page.getByRole("button", { name: "系统记录" })).toHaveAttribute("aria-pressed", "false");
    await tabs.locator(`a[href="${workerPath}"]`).click();
    await expect(input).toHaveValue("实现者的本地草稿");
    await expect(page.getByRole("button", { name: "系统记录" })).toHaveAttribute("aria-pressed", "true");
    await tabs.locator(`a[href="/workspaces/${WORKSPACE_ID}/worker-items/${TESTER_WORKER_ITEM_ID}"]`).click();
    await expect(input).toHaveValue("");
    await expect(page.getByRole("button", { name: "系统记录" })).toHaveAttribute("aria-pressed", "false");
  });

  test("keeps tabs clickable beneath navigation after scrolling", async ({ page }, testInfo) => {
    await page.goto(rootPath);
    await page.getByRole("button", { name: "查看任务详情" }).click();
    await page.evaluate(() => window.scrollTo(0, 400));
    const current = page.locator(".workspace-tab.is-active .workspace-tab__link");
    await expect.poll(() => current.evaluate(element => {
      const bounds = element.getBoundingClientRect();
      return element.contains(document.elementFromPoint(bounds.left + bounds.width / 2, bounds.top + bounds.height / 2));
    })).toBe(true);
    const frame = await page.locator(".workspace-tabs").boundingBox();
    expect(frame!.height).toBeLessThanOrEqual(50);
    await page.screenshot({ path: testInfo.outputPath("tabs-scrolled.png"), animations: "disabled" });
    await expect(page.getByRole("button", { name: "查看任务详情" })).toHaveAttribute("aria-expanded", "true");
  });
});
