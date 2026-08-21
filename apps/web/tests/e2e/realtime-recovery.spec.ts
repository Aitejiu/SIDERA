import { randomUUID } from "node:crypto";

import { test, expect, ROOT_TASK_ID, WORKSPACE_ID } from "./fixtures";

const cursorKey = `sidera:projection-cursor:${WORKSPACE_ID}`;
const streamPattern = `**/api/v1/workspaces/${WORKSPACE_ID}/projection-changes*`;

test.describe("projection stream recovery", () => {
  test.beforeEach(({ seedR4 }) => {
    seedR4();
  });

  test("reconnects after a transport failure and consumes the next cursor", async ({ page, request }) => {
    let attempts = 0;
    await page.route(streamPattern, async (route) => {
      attempts += 1;
      if (attempts === 1) {
        await route.abort("failed");
        return;
      }
      await route.continue();
    });

    await page.goto(`/workspaces/${WORKSPACE_ID}/tasks`);
    await expect(page.getByRole("link", { name: "打开任务 R4 control room delivery" })).toBeVisible();
    await expect.poll(() => attempts).toBeGreaterThan(0);

    const response = await request.post("/api/v1/commands", {
      data: {
        command_id: randomUUID(),
        workspace_id: WORKSPACE_ID,
        command_type: "SetTaskPriority",
        target_type: "task",
        actor: { type: "local", id: "aitejiu" },
        target_id: ROOT_TASK_ID,
        expected_version: 1,
        idempotency_key: `e2e-priority-${randomUUID()}`,
        correlation_id: randomUUID(),
        payload: { priority: 55 },
      },
    });
    expect(response.ok()).toBeTruthy();

    await expect.poll(
      async () => Number(await page.evaluate((key) => localStorage.getItem(key), cursorKey)),
      { timeout: 10_000 },
    ).toBeGreaterThanOrEqual(4);
    await expect(page.getByRole("link", { name: "打开任务 R4 control room delivery" })).toBeVisible();
  });

  test("resyncs the canonical snapshot after a server gap notification", async ({ page }) => {
    let attempts = 0;
    await page.route(streamPattern, async (route) => {
      attempts += 1;
      if (attempts === 1) {
        await route.fulfill({
          status: 200,
          contentType: "text/event-stream",
          body: "event: resync_required\ndata: {\"reason\":\"e2e_gap\"}\n\n",
        });
        return;
      }
      await route.continue();
    });

    await page.goto(`/workspaces/${WORKSPACE_ID}/tasks`);
    await expect(page.getByRole("link", { name: "打开任务 R4 control room delivery" })).toBeVisible();
    await expect.poll(() => attempts).toBeGreaterThanOrEqual(2);
    await expect(page.getByRole("heading", { name: "任务", exact: true })).toBeVisible();
    await expect(page.getByText("CANONICAL SNAPSHOT", { exact: false })).toBeVisible();
  });
});
