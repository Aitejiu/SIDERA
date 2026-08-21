import { test, expect, EXECUTION_ID, ROOT_TASK_ID, SESSION_ID, WORKSPACE_ID } from "./fixtures";

test.describe("Task First control room", () => {
  test.beforeEach(({ seedR4 }) => {
    seedR4();
  });

  test("moves from task overview to graph, ledger, trace, and attention", async ({ page }) => {
    await page.goto(`/workspaces/${WORKSPACE_ID}/tasks`);
    await expect(page.getByRole("heading", { name: "任务", exact: true })).toBeVisible();

    const taskLink = page.getByRole("link", { name: "打开任务 R4 control room delivery" });
    await expect(taskLink).toBeVisible();
    await expect(taskLink).toContainText("GEN 2");
    await expect(taskLink).toContainText("运行中 1");
    await expect(taskLink).toContainText("需要关注 1");
    await taskLink.click();

    await expect(page.locator("#task-detail-title")).toHaveText("R4 control room delivery");
    await expect(page.getByRole("heading", { name: "需要关注", exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "确认已处理" })).toBeVisible();

    await page.getByRole("tab", { name: "DAG", exact: true }).click();
    await expect(page.getByRole("region", { name: "任务 DAG" })).toBeVisible();
    await expect(page.locator(".edge-summary")).toHaveCount(2);
    await expect(page.getByText("依赖：backend → integration", { exact: true })).toBeVisible();
    await expect(page.getByText("依赖：frontend → integration", { exact: true })).toBeVisible();

    await page.getByRole("tab", { name: "任务表", exact: true }).click();
    await expect(page.getByRole("heading", { name: "任务表", exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "在任务表中选择Backend execution" })).toBeVisible();
    const executionLink = page.getByRole("link", { name: /Execution 60000000/ });
    await expect(executionLink).toBeVisible();
    await executionLink.click();

    await expect(page.locator("#worker-trace-title")).toHaveText("Worker 轨迹");
    await expect(page).toHaveURL(new RegExp(`${EXECUTION_ID}/sessions/${SESSION_ID}/trace$`));
    await expect(page.locator(".trace-row")).toHaveCount(3);
    await expect(page.getByRole("heading", { name: "Worker 轨迹", exact: true })).toHaveCount(2);
    await expect(page.getByText("Backend checkpoint")).toBeVisible();
  });

  test("acknowledges a blocked child without changing the graph shape", async ({ page }) => {
    await page.goto(`/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}`);
    await expect(page.locator("#task-detail-title")).toBeVisible();
    await expect(page.getByRole("button", { name: "确认已处理" })).toBeVisible();

    await page.getByRole("button", { name: "确认已处理" }).click();
    await expect(page.getByText("当前没有待确认事项", { exact: true })).toBeVisible();
    if (await page.evaluate(() => window.innerWidth < 640)) {
      await page.getByRole("tab", { name: "DAG", exact: true }).click();
    }
    await expect(page.locator(".edge-summary")).toHaveCount(2);
  });

  test("keeps plan history read-only and exposes the generation comparison", async ({ page }) => {
    await page.goto(`/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}`);
    await expect(page.locator("#task-detail-title")).toBeVisible();
    await page.getByRole("link", { name: /计划历史/ }).click();

    await expect(page.locator("#plan-generation-title")).toHaveText("计划 Generation 2");
    await expect(page.getByRole("link", { name: "Generation 1" })).toBeVisible();
    await page.getByRole("link", { name: "比较当前" }).click();

    await expect(page.locator("#plan-diff-title")).toHaveText("计划差异");
    await expect(page.getByText("= 可复用", { exact: true })).toBeVisible();
    await expect(page.getByText("~ Context Patch 建议", { exact: true })).toBeVisible();
    await expect(page.getByText("+ 新增", { exact: true })).toBeVisible();
    await expect(page.getByText("此页是历史证据与建议视图，不提供 Apply、Stop 或 Reassign 操作。", { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: /Apply|Stop|Reassign/i })).toHaveCount(0);
  });
});
