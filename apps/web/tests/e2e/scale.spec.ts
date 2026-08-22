import {
  test,
  expect,
  ROOT_TASK_ID,
  SCALE_EXECUTION_ID,
  SCALE_SESSION_ID,
  WORKSPACE_ID,
} from "./fixtures";

test.describe("large Task First graph", () => {
  test("renders 200 WorkItems and 400 dependency edges without dropping the ledger", async ({ page, seedR4 }) => {
    seedR4("scale");
    const startedAt = Date.now();
    await page.goto(`/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}`);
    await expect(page.locator("#task-detail-title")).toHaveText("Scale graph");
    await page.getByRole("tab", { name: "任务表", exact: true }).click();
    await expect(page.locator(".ledger-row")).toHaveCount(200, { timeout: 15_000 });
    const ledgerElapsedMs = Date.now() - startedAt;
    test.info().attach("scale-ledger-timing", {
      body: JSON.stringify({ ledgerElapsedMs, rows: 200 }),
      contentType: "application/json",
    });
    expect(ledgerElapsedMs).toBeLessThan(8_000);

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
});
