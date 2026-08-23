import { test, expect, R5_WORKSPACE_ID, runPlanningWorkerOnce } from "./fixtures";

test.describe.configure({ timeout: 60_000 });

test.describe("R5.1 bounded large proposal", () => {
  test("renders 200 subtasks and defers the expensive DAG until requested", async ({ page, seedR5 }) => {
    seedR5("scale");
    await page.goto(`/workspaces/${R5_WORKSPACE_ID}/tasks`);
    await page.getByRole("region", { name: "任务" }).getByRole("link", { name: "新建任务" }).click();
    await page.getByLabel("需求目标").fill("验证大规模规划方案仍然可审阅");
    await page.getByRole("textbox", { name: "验收条件" }).first().fill("默认先看任务架构，明确请求后再计算依赖图");
    await page.getByRole("button", { name: "提交需求" }).click();
    await expect(page).toHaveURL(new RegExp(`/workspaces/${R5_WORKSPACE_ID}/planning/[0-9a-f-]{36}$`));

    const summary = runPlanningWorkerOnce();
    expect(summary.succeeded).toBe(1);
    await page.reload();
    await expect(page.getByRole("heading", { name: "执行架构", exact: true })).toBeVisible();
    await expect(page.getByText("200 个子任务 · 200 个 WorkerItem", { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: /展开子任务：Scale subtask/ })).toHaveCount(200);
    await expect(page.getByTestId("proposal-graph-canvas")).toHaveCount(0);

    await page.getByRole("tab", { name: "依赖 DAG", exact: true }).click();
    await expect(page.getByTestId("proposal-graph-canvas")).toBeVisible();
    await expect(
      page.getByRole("group", { name: "方案依赖清单" }).locator(".edge-summary"),
    ).toHaveCount(199, { timeout: 45_000 });
  });
});
