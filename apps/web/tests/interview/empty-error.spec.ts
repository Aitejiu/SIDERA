import { randomUUID } from "node:crypto";

import { expect, test } from "./fixtures";

test.describe("empty and error states", () => {
  test.use({ demoScenario: "mainline" });

  test("F18 empty workspace has a usable task entry", async ({ interview, page }) => {
    test.info().annotations.push({ type: "action", description: "F18-01" });
    await page.goto(`${interview.manifest.endpoints.web}/workspaces/${interview.manifest.workspace_id}/tasks`);
    await expect(page.getByRole("heading", { name: "任务", exact: true })).toBeVisible();
    await expect(page.locator(".task-list-page").getByRole("link", { name: "新建任务", exact: true })).toBeVisible();
    await expect(page.getByRole("alert")).toHaveCount(0);
  });
});

test.describe("resource and request errors", () => {
  test.use({ demoScenario: "controls" });

  test("F18 missing resources and a retryable request failure stay explicit", async ({ interview, page }) => {
    test.info().annotations.push({ type: "action", description: "F18-02" });
    test.info().annotations.push({ type: "action", description: "F18-03" });
    const missingId = randomUUID();

    await page.goto(`${interview.manifest.endpoints.web}/workspaces/${interview.manifest.workspace_id}/tasks/${missingId}`);
    await expect(page.getByRole("alert")).toContainText("无法读取任务图");
    await expect(page.getByRole("button", { name: "重试读取任务图", exact: true })).toBeVisible();

    await page.goto(`${interview.manifest.endpoints.web}/workspaces/${interview.manifest.workspace_id}/worker-items/${missingId}`);
    await expect(page.getByRole("alert")).toContainText("无法读取 WorkerItem 工作区");
    await expect(page.getByRole("button", { name: "重试读取 WorkerItem", exact: true })).toBeVisible();

    await page.goto(`${interview.manifest.endpoints.web}/workspaces/${interview.manifest.workspace_id}/tasks`);
    await expect(page.getByRole("heading", { name: "任务", exact: true })).toBeVisible();

    let injectFailure = true;
    await page.route("**/*", async (route) => {
      if (!route.request().url().includes("/task-overviews")) {
        await route.continue();
        return;
      }
      if (injectFailure) {
        await route.abort();
      } else {
        await route.continue();
      }
    });
    await page.getByRole("button", { name: "刷新任务列表", exact: true }).click();
    await expect(page.getByRole("alert")).toContainText("无法读取任务快照");
    injectFailure = false;
    await page.getByRole("button", { name: "重试加载任务", exact: true }).click();
    await expect(page.getByRole("heading", { name: "任务", exact: true })).toBeVisible();
    await page.unroute("**/*");
  });
});
