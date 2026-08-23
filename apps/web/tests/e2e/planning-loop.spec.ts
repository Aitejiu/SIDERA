import { test, expect, R5_WORKSPACE_ID, runActivationWorkerOnce, runPlanningWorkerOnce, runSchedulerOnce } from "./fixtures";

test.describe("R5.1 Planning control loop", () => {
  test.beforeEach(({ seedR5 }) => {
    seedR5("proposal");
  });

  test("keeps planning read-only until confirmation, then exposes the Task First graph", async ({ page, request }) => {
    await page.goto(`/workspaces/${R5_WORKSPACE_ID}/tasks`);
    await expect(page.getByRole("heading", { name: "任务", exact: true })).toBeVisible();

    await page.getByRole("region", { name: "任务" }).getByRole("link", { name: "新建任务" }).click();
    await page.getByLabel("需求目标").fill("为控制面增加可恢复的规划闭环");
    await page.getByRole("textbox", { name: "验收条件" }).first().fill("用户确认前不创建任务，确认后可看到 WorkerItem 状态");
    await page.getByRole("button", { name: "提交需求" }).click();
    await expect(page).toHaveURL(new RegExp(`/workspaces/${R5_WORKSPACE_ID}/planning/[0-9a-f-]{36}$`));
    const planningUrl = page.url();

    await expect(page.getByText("已提交", { exact: true })).toBeVisible();
    const beforeConfirmation = await request.get(
      `/api/v1/workspaces/${R5_WORKSPACE_ID}/task-overviews`,
    );
    expect(beforeConfirmation.ok()).toBeTruthy();
    expect(await beforeConfirmation.json()).toEqual([]);

    const planningSummary = runPlanningWorkerOnce();
    expect(planningSummary.succeeded).toBe(1);
    await page.reload();
    await expect(page.locator(".planning-status").filter({ hasText: "方案待确认" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "执行架构", exact: true })).toBeVisible();
    await expect(page.getByText("1 个子任务 · 1 个 WorkerItem", { exact: true })).toBeVisible();

    await page.getByRole("tab", { name: "依赖 DAG", exact: true }).click();
    await expect(page.getByRole("heading", { name: "依赖 DAG", exact: true })).toBeVisible();
    await expect(page.getByTestId("proposal-graph-canvas")).toBeVisible();
    await page.getByRole("tab", { name: "变更", exact: true }).click();
    await expect(page.getByRole("heading", { name: "变更", exact: true })).toBeVisible();

    await page.getByRole("button", { name: "确认并创建任务" }).click();
    await expect(page).toHaveURL(new RegExp(`/workspaces/${R5_WORKSPACE_ID}/tasks/[^/]+$`));
    const taskUrl = page.url();
    await expect(page.locator("#task-detail-title")).toHaveText("Planner generated task");
    await expect(page.getByText("1 个 WorkerItem", { exact: true })).toBeVisible();

    const activationSummary = runActivationWorkerOnce();
    expect(activationSummary.status).toBe("ok");
    const schedulerSummary = runSchedulerOnce();
    expect(schedulerSummary.status).toBe("ok");
    await page.reload();
    await expect(page.locator("#task-detail-title")).toHaveText("Planner generated task");
    await expect(page.getByText("1 个 WorkerItem", { exact: true })).toBeVisible();

    await page.getByRole("navigation", { name: "已打开的任务" }).getByRole("link", { name: /^规划 / }).click();
    await expect(page).toHaveURL(planningUrl);
    await expect(page.locator(".planning-status").filter({ hasText: "已转为任务" })).toBeVisible();
    await expect(page.getByText("只读历史", { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "确认并创建任务" })).toHaveCount(0);
    await expect(page.getByRole("navigation", { name: "已打开的任务" }).getByText("Planner generated task", { exact: true })).toBeVisible();

    await page.goto(taskUrl);
    await expect(page.locator("#task-detail-title")).toHaveText("Planner generated task");
  });
});
