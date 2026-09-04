import {
  test,
  expect,
  ROOT_TASK_ID,
  R5_WORKSPACE_ID,
  mockNotificationInbox,
  runPlanningWorkerOnce,
  WORKSPACE_ID,
} from "./fixtures";

test.describe("responsive control room visuals", () => {
  test.beforeEach(({ seedR4 }) => {
    seedR4();
  });

  test("keeps the task overview stable", async ({ page }) => {
    await page.goto(`/workspaces/${WORKSPACE_ID}/tasks`);
    await expect(page.getByRole("link", { name: "打开任务 支付重构" })).toBeVisible();
    await expect(page).toHaveScreenshot("task-list.png", {
      fullPage: true,
      animations: "disabled",
      mask: [page.locator(".mono-label"), page.locator(".freshness-label")],
    });
  });

  test("keeps the selected task surface stable", async ({ page }) => {
    await page.goto(`/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}`);
    await expect(page.locator("#task-detail-title")).toBeVisible();
    await expect(page.locator(".task-board-card")).toHaveCount(3);
    await expect(page).toHaveScreenshot("task-detail.png", {
      fullPage: true,
      animations: "disabled",
      mask: [page.locator(".mono-label"), page.locator(".freshness-label")],
    });
  });

  test("keeps the planning proposal surface stable", async ({ page, seedR5 }) => {
    seedR5("proposal");
    await page.goto(`/workspaces/${R5_WORKSPACE_ID}/tasks`);
    await page.getByRole("region", { name: "任务" }).getByRole("link", { name: "新建任务" }).click();
    await page.getByLabel("需求目标").fill("验证方案审阅视图的稳定布局");
    await page.getByRole("textbox", { name: "验收条件" }).first().fill("架构信息和确认门保持清晰");
    await page.getByRole("button", { name: "提交需求" }).click();
    await expect(page).toHaveURL(new RegExp(`/workspaces/${R5_WORKSPACE_ID}/planning/[0-9a-f-]{36}$`));
    expect(runPlanningWorkerOnce().succeeded).toBe(1);
    await page.reload();
    await expect(page.getByRole("heading", { name: "执行架构", exact: true })).toBeVisible();
    await expect(page.getByRole("heading", { name: "本次运行上下文", exact: true })).toBeVisible();
    await expect(page).toHaveScreenshot("planning-proposal.png", {
      fullPage: true,
      animations: "disabled",
      mask: [
        page.locator(".mono-label"),
        page.locator(".planning-run-status__facts"),
        page.locator(".planning-run-context__facts"),
        page.locator(".workspace-tab__label").filter({ hasText: "规划" }),
        page.locator(".planning-inspector__section").nth(1).locator("dd").nth(2),
      ],
    });
  });

  test("keeps the notification inbox stable across viewport projects", async ({ page }) => {
    await mockNotificationInbox(page);
    await page.goto(`/workspaces/${WORKSPACE_ID}/inbox`);
    await expect(page.getByRole("heading", { name: "信箱", exact: true })).toBeVisible();
    await expect(page.getByText("支付重构 / 通知网关 / 实现者")).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
    await expect(page).toHaveScreenshot("notification-inbox.png", {
      fullPage: true,
      animations: "disabled",
      mask: [
        page.locator(".mono-label"),
        page.locator(".notification-inbox__stream-state"),
      ],
    });
  });
});
