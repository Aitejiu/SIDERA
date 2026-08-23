import { test, expect, R5_WORKSPACE_ID, runPlanningWorkerOnce } from "./fixtures";

test.describe.configure({ timeout: 120_000 });

test.describe("real OpenCode planning smoke", () => {
  test.skip(
    process.env.SIDERA_RUN_REAL_OPENCODE !== "1",
    "set SIDERA_RUN_REAL_OPENCODE=1 to run the real OpenCode browser gate",
  );

  test.beforeEach(({ seedR5 }) => {
    seedR5("real");
  });

  test("renders and confirms a real OpenCode proposal", async ({ page }) => {
    await page.goto(`/workspaces/${R5_WORKSPACE_ID}/tasks`);
    await page.getByRole("region", { name: "任务" }).getByRole("link", { name: "新建任务" }).click();
    await page.getByLabel("需求目标").fill("Make one small, reviewable repository change.");
    await page.getByRole("textbox", { name: "约束" }).fill(
      "Return exactly one subtask and one WorkerItem. Do not ask for clarification. Set the proposal to activate immediately after confirmation.",
    );
    await page.getByRole("textbox", { name: "验收条件" }).first().fill("A reviewable Git diff is produced.");
    await page.getByRole("button", { name: "提交需求" }).click();

    await expect(page).toHaveURL(new RegExp(`/workspaces/${R5_WORKSPACE_ID}/planning/[0-9a-f-]{36}$`));
    await expect(page.getByText("已提交", { exact: true })).toBeVisible();

    const summary = runPlanningWorkerOnce();
    expect(summary.succeeded).toBe(1);
    await page.reload();

    await expect(page.locator(".planning-status").filter({ hasText: "方案待确认" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "执行架构", exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "确认并创建任务" })).toBeVisible();

    await page.getByRole("button", { name: "确认并创建任务" }).click();
    await expect(page).toHaveURL(new RegExp(`/workspaces/${R5_WORKSPACE_ID}/tasks/[^/]+$`));
    await expect(page.locator("#task-detail-title")).toBeVisible();
  });
});
