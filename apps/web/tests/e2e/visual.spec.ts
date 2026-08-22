import { test, expect, ROOT_TASK_ID, WORKSPACE_ID } from "./fixtures";

test.describe("responsive control room visuals", () => {
  test.beforeEach(({ seedR4 }) => {
    seedR4();
  });

  test("keeps the task overview stable", async ({ page }) => {
    await page.goto(`/workspaces/${WORKSPACE_ID}/tasks`);
    await expect(page.getByRole("link", { name: "打开任务 R4 control room delivery" })).toBeVisible();
    await expect(page).toHaveScreenshot("task-list.png", {
      fullPage: true,
      animations: "disabled",
      mask: [page.locator(".mono-label"), page.locator(".freshness-label")],
    });
  });

  test("keeps the selected task surface stable", async ({ page }) => {
    await page.goto(`/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}`);
    await expect(page.locator("#task-detail-title")).toBeVisible();
    await expect(page.locator(".task-board-card")).toHaveCount(4);
    await expect(page).toHaveScreenshot("task-detail.png", {
      fullPage: true,
      animations: "disabled",
      mask: [page.locator(".mono-label"), page.locator(".freshness-label")],
    });
  });
});
