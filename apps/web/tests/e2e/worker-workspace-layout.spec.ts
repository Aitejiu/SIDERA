import { test, expect, WORKSPACE_ID, IMPLEMENTER_WORKER_ITEM_ID } from "./fixtures";
import type { WorkerActivity } from "../../src/features/worker-items/types";

test.beforeEach(({ seedR4 }) => seedR4());

test("keeps the conversation composer close to work without covering it", async ({ page }, testInfo) => {
  await page.goto(`/workspaces/${WORKSPACE_ID}/worker-items/${IMPLEMENTER_WORKER_ITEM_ID}`);
  const composer = page.getByRole("textbox", { name: "介入这个 Worker" });
  await expect(composer).toBeVisible();
  await expect(page.locator(".worker-timeline-item").first()).toBeVisible();
  await page.evaluate(() => window.scrollTo(0, 0));
  const viewport = page.viewportSize()!;
  const input = await composer.boundingBox();
  expect(input!.y).toBeLessThan(viewport.height);
  const timeline = await page.locator(".worker-timeline").boundingBox();
  const region = await page.locator(".worker-composer").boundingBox();
  expect(region!.height).toBeLessThanOrEqual(160);
  expect(region!.y + region!.height).toBeLessThanOrEqual(viewport.height);
  expect(region!.y).toBeGreaterThanOrEqual(timeline!.y + timeline!.height);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  const sidebar = page.getByRole("complementary", { name: "WorkerItem 事实" });
  await expect(sidebar.getByRole("heading", { name: "当前 Agent" })).toBeVisible();
  await expect(sidebar.getByText("更多任务信息", { exact: true })).toBeVisible();
  await expect(sidebar.getByRole("region", { name: "结果输入" })).toBeHidden();
  const crumb = page.locator(".worker-item-page__breadcrumb");
  const crumbPositions = await crumb.locator(":scope > *").evaluateAll(elements => elements.map(element => {
    const box = element.getBoundingClientRect();
    return box.y + box.height / 2;
  }));
  expect(Math.max(...crumbPositions) - Math.min(...crumbPositions)).toBeLessThan(2);
  const record = page.getByRole("region", { name: "WorkerItem 活动时间线" });
  await expect.poll(() => record.evaluate(element => element.scrollHeight - element.clientHeight - element.scrollTop)).toBeLessThan(3);
  await page.screenshot({ path: testInfo.outputPath("worker-first-viewport.png"), animations: "disabled" });
  await page.screenshot({ path: testInfo.outputPath("worker-full.png"), fullPage: true, animations: "disabled" });
});

test("grows a local intervention draft without moving it over the work record", async ({ page }) => {
  await page.goto(`/workspaces/${WORKSPACE_ID}/worker-items/${IMPLEMENTER_WORKER_ITEM_ID}`);
  const input = page.getByRole("textbox", { name: "介入这个 Worker" });
  await expect(input).toBeEditable();
  const initialHeight = (await input.boundingBox())!.height;
  await input.fill("第一项：核对回调签名\n第二项：保留重试逻辑\n第三项：补充重复回调测试\n第四项：检查并发写入\n第五项：汇总结果");
  await expect.poll(async () => (await input.boundingBox())!.height).toBeGreaterThan(initialHeight);
  expect((await input.boundingBox())!.height).toBeLessThanOrEqual(180);
  const timeline = (await page.locator(".worker-timeline").boundingBox())!;
  const composer = (await page.locator(".worker-composer").boundingBox())!;
  expect(composer.y).toBeGreaterThanOrEqual(timeline.y + timeline.height);
  await expect(page.getByRole("button", { name: "发送到当前 Session" })).toBeEnabled();
  await input.fill("");
  await expect.poll(async () => (await input.boundingBox())!.height).toBe(initialHeight);
});

test("keeps the reading anchor when loading older steps and receiving new work", async ({ page, request }) => {
  let includeNewStep = false;
  const step = (index: number): WorkerActivity => ({
    activity_id: `a0000000-0000-0000-0000-${String(index).padStart(12, "0")}`,
    source_type: "execution_event", source_id: `step-${index}`,
    execution_id: null, session_id: null, occurred_at: new Date(Date.UTC(2026, 8, 10, 0, index)).toISOString(),
    kind: "assistant_summary", title: "assistant.summary", summary: `验证步骤 ${index}`,
    correlation_id: "e2e-reading-anchor", body: null, delivery_status: null, artifact_ids: [], trace_descriptor: null,
  });
  const response = await request.get(`/api/v1/workspaces/${WORKSPACE_ID}/worker-items/${IMPLEMENTER_WORKER_ITEM_ID}/activity?limit=100`);
  expect(response.ok()).toBe(true);
  const projection = await response.json();
  await page.route(`**/worker-items/${IMPLEMENTER_WORKER_ITEM_ID}/activity?*`, async route => {
    const isOlder = new URL(route.request().url()).searchParams.has("after");
    await route.fulfill({ json: {
      ...projection,
      items: isOlder ? Array.from({ length: 50 }, (_, index) => step(index))
        : Array.from({ length: includeNewStep ? 51 : 50 }, (_, index) => step(index + 50)),
      next_cursor: isOlder ? null : "older-steps",
    } });
  });
  await page.goto(`/workspaces/${WORKSPACE_ID}/worker-items/${IMPLEMENTER_WORKER_ITEM_ID}`);
  const record = page.getByRole("region", { name: "WorkerItem 活动时间线" });
  await expect.poll(() => record.evaluate(element => element.scrollHeight - element.clientHeight - element.scrollTop)).toBeLessThan(3);
  await record.evaluate(element => { element.scrollTop = 500; });
  await expect.poll(() => record.evaluate(element => element.scrollTop)).toBe(500);
  const visibleAnchor = () => record.evaluate(element => {
    const viewport = element.getBoundingClientRect();
    const row = Array.from(element.querySelectorAll("article")).find(item => {
      const box = item.getBoundingClientRect();
      return box.top >= viewport.top && box.bottom <= viewport.bottom;
    });
    return row ? { text: row.querySelector("strong")!.textContent!, offset: row.getBoundingClientRect().top - viewport.top } : null;
  });
  // scrollTop updates before the virtual list has rendered the new visible rows.
  await expect.poll(visibleAnchor).not.toBeNull();
  const anchor = (await visibleAnchor())!;
  await page.getByRole("button", { name: "加载更多活动" }).click();
  await expect(page.getByRole("button", { name: "加载更多活动" })).toBeHidden();
  const offset = () => record.getByText(anchor.text, { exact: true }).locator("xpath=ancestor::article").evaluate(element =>
    element.getBoundingClientRect().top - element.closest(".worker-timeline__scroll")!.getBoundingClientRect().top,
  );
  await expect.poll(async () => Math.abs(await offset() - anchor.offset)).toBeLessThan(2);
  includeNewStep = true;
  await page.getByRole("textbox", { name: "介入这个 Worker" }).fill("核对目前的工作步骤");
  await page.getByRole("button", { name: "发送到当前 Session" }).click();
  await expect(page.getByText("已接受", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "有新活动" })).toBeVisible();
  await expect.poll(async () => Math.abs(await offset() - anchor.offset)).toBeLessThan(2);
  await page.getByRole("button", { name: "有新活动" }).click();
  await expect.poll(() => record.evaluate(element => element.scrollHeight - element.clientHeight - element.scrollTop)).toBeLessThan(3);
  await expect(record.getByText("验证步骤 100", { exact: true })).toBeVisible();
  expect(await record.locator("article").count()).toBeLessThan(50);
  await page.unrouteAll({ behavior: "wait" });
});
