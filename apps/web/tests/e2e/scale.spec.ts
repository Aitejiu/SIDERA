import {
  test,
  expect,
  ROOT_TASK_ID,
  SCALE_EXECUTION_ID,
  SCALE_WORKER_ITEM_ID,
  SCALE_SESSION_ID,
  WORKSPACE_ID,
} from "./fixtures";

test.describe("large Task First graph", () => {
  test("renders 200 Subtasks and 400 dependency edges without dropping the board", async ({ page, seedR4 }) => {
    seedR4("scale");
    const startedAt = Date.now();
    await page.goto(`/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}`);
    await expect(page.locator("#task-detail-title")).toHaveText("Scale graph");
    await expect(page.locator(".task-board-card")).toHaveCount(200, { timeout: 15_000 });
    const boardElapsedMs = Date.now() - startedAt;
    test.info().attach("scale-board-timing", {
      body: JSON.stringify({ boardElapsedMs, cards: 200 }),
      contentType: "application/json",
    });
    expect(boardElapsedMs).toBeLessThan(8_000);

    await page.getByRole("tab", { name: "DAG", exact: true }).click();
    await expect(page.locator(".edge-summary")).toHaveCount(400, { timeout: 20_000 });
    await expect(page.getByText("依赖：work-001 → work-002", { exact: true })).toBeVisible();
    await expect(page.getByText("依赖：work-001 → work-004", { exact: true })).toBeVisible();
  });

  test("pages a 50,000-event trace without growing the DOM", async ({ page, request, seedR4 }) => {
    seedR4("scale");
    const response = await request.get(
      `/api/v1/tasks/${ROOT_TASK_ID}/executions/${SCALE_EXECUTION_ID}/sessions/${SCALE_SESSION_ID}/trace?workspace_id=${WORKSPACE_ID}&limit=100`,
    );
    expect(response.ok()).toBeTruthy();
    const body = await response.json() as { events?: unknown[]; previous_before_sequence?: number | null };
    expect(body.events).toHaveLength(100);
    expect(body.previous_before_sequence).toBe(49_901);

    await page.goto(
      `/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}/executions/${SCALE_EXECUTION_ID}/sessions/${SCALE_SESSION_ID}/trace`,
    );
    await expect(page.locator("#worker-trace-title")).toHaveText("Worker 轨迹");
    await expect(page.locator(".trace-row")).not.toHaveCount(0);
    expect(await page.locator(".trace-row").count()).toBeLessThanOrEqual(120);

    const selected = page.locator(".trace-row").last();
    await selected.click();
    await expect(selected).toHaveAttribute("aria-current", "true");
    await page.getByRole("button", { name: "更早事件" }).click();
    await expect(page.locator(".trace-row")).not.toHaveCount(0);
    await expect(page.locator(".trace-row[aria-current='true']")).toHaveCount(1);
    expect(await page.locator(".trace-row").count()).toBeLessThanOrEqual(120);
  });

  test("pages a WorkerItem activity stream with 2,000 items", async ({ request, seedR4 }) => {
    seedR4("scale");
    const workspaceResponse = await request.get(
      `/api/v1/tasks/${ROOT_TASK_ID}/subtasks/work-001?workspace_id=${WORKSPACE_ID}`,
    );
    expect(workspaceResponse.ok()).toBeTruthy();
    const workspaceBody = await workspaceResponse.json() as {
      worker_items?: unknown[];
      worker_graph?: { edges?: unknown[] };
    };
    expect(workspaceBody.worker_items).toHaveLength(40);
    expect(workspaceBody.worker_graph?.edges).toHaveLength(80);

    const response = await request.get(
      `/api/v1/workspaces/${WORKSPACE_ID}/worker-items/${SCALE_WORKER_ITEM_ID}/activity?limit=100`,
    );
    expect(response.ok()).toBeTruthy();
    const body = await response.json() as {
      items?: unknown[];
      next_cursor?: string | null;
    };
    expect(body.items).toHaveLength(100);
    expect(body.next_cursor).toBeTruthy();
  });
});
