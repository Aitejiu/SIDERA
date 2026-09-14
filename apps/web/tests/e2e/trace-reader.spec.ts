import { AxeBuilder } from "@axe-core/playwright";
import type { Page } from "@playwright/test";
import type { WorkerTrace, WorkerTraceEvent } from "../../src/features/trace/types";
import { test, expect, WORKSPACE_ID, IMPLEMENTER_WORKER_ITEM_ID } from "./fixtures";

const workerPath = `/workspaces/${WORKSPACE_ID}/worker-items/${IMPLEMENTER_WORKER_ITEM_ID}`;

async function traceFixture(page: Page) {
  let address = "";
  await page.route("**/api/v1/tasks/*/executions/*/sessions/*/trace?*", async route => {
    const response = await route.fetch();
    const body: WorkerTrace = await response.json();
    address = new URL(route.request().url()).pathname.replace("/api/v1", `/workspaces/${WORKSPACE_ID}`);
    const older = new URL(route.request().url()).searchParams.has("before_sequence");
    const base = body.events[0]!;
    const events: WorkerTraceEvent[] = Array.from({ length: older ? 100 : 60 }, (_, index) => {
      const sequence = index + (older ? 1 : 101);
      const isTool = sequence % 3 === 0;
      return {
        ...base, sequence, category: isTool ? "tool" : "assistant_summary",
        title: isTool ? `运行验证 ${sequence}` : `检查实现 ${sequence}`,
        summary: isTool ? "执行回调幂等测试，检查重复请求是否被正确处理。" : "## 检查结果\n\n已核对回调处理逻辑。\n\n```ts\nconst result = await verifyCallback(request);\n```",
        status: sequence === 150 ? "failed" : "completed", duration_ms: isTool ? 320 : null,
        usage: null, artifact_refs: [], provider_extensions: isTool ? {
          rawInput: { command: "pytest tests/callback -q", description: "验证回调幂等" },
          rawOutput: "12 passed in 0.32s\n".repeat(24),
          "vendor:unknown": { retained: true },
        } : {},
      };
    });
    await route.fulfill({ response, json: { ...body, events, previous_before_sequence: older ? null : 101, ...(older ? { plan_generation: 1 } : {}) } });
  });
  return () => address;
}

test.describe("Trace reader", () => {
  test.beforeEach(({ seedR4 }) => seedR4());

  test("keeps keyboard focus inside Trace and restores its entry on Escape", async ({ page }) => {
    await page.goto(workerPath);
    const entry = page.getByRole("button", { name: "查看当前 Agent Trace" });
    await entry.click();
    const dialog = page.getByRole("dialog", { name: "Agent Trace" });
    await expect(dialog.locator(".trace-row").first()).toBeVisible();
    await dialog.getByRole("button", { name: "关闭 Agent Trace" }).focus();
    await page.keyboard.press("Shift+Tab");
    // A native modal permits visiting browser chrome, but never background app controls.
    if (await page.evaluate(() => document.activeElement === document.body)) await page.keyboard.press("Shift+Tab");
    expect(await dialog.evaluate(node => node.contains(document.activeElement))).toBe(true);
    expect(await dialog.evaluate(node => node.matches(":modal"))).toBe(true);
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    await expect(entry).toBeFocused();
  });

  test("reads selected records in place and preserves mobile list position", async ({ page }, testInfo) => {
    await traceFixture(page);
    await page.goto(workerPath);
    await page.getByRole("button", { name: "查看当前 Agent Trace" }).click();
    const dialog = page.getByRole("dialog", { name: "Agent Trace" });
    const ledger = dialog.locator(".trace-ledger__scroll");
    await expect(dialog.getByRole("button", { name: /检查实现 160/ })).toBeInViewport();
    await ledger.evaluate(node => { node.scrollTop = 1120; });
    await expect(dialog.getByRole("button", { name: "跟随尾部" })).toBeVisible();
    const row = dialog.getByRole("button", { name: /运行验证 123/ });
    await expect(row).toBeInViewport();
    await row.click();
    const before = await ledger.evaluate(node => node.scrollTop);
    await expect(dialog.locator("#trace-details-title")).toHaveText("运行验证 123");
    await expect(dialog.getByRole("region", { name: "输入", exact: true })).toContainText("pytest tests/callback -q");
    await expect(dialog.getByRole("region", { name: "输出", exact: true })).toContainText("12 passed");
    await expect(dialog.locator("#trace-details-title")).toBeInViewport();
    if (testInfo.project.name === "mobile-chromium") {
      await expect(dialog.getByRole("searchbox")).toBeHidden();
    } else {
      const listBox = await ledger.boundingBox();
      const detailBox = await dialog.locator(".trace-reader__detail").boundingBox();
      expect(detailBox!.x).toBeGreaterThanOrEqual(listBox!.x + listBox!.width - 1);
    }
    expect(await dialog.evaluate(node => node.scrollWidth <= node.clientWidth + 1)).toBe(true);
    expect((await new AxeBuilder({ page }).include(".worker-trace-dialog").withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze()).violations).toEqual([]);
    await page.screenshot({ path: testInfo.outputPath("trace-reader-detail.png"), animations: "disabled" });
    if (testInfo.project.name === "mobile-chromium") {
      await dialog.getByRole("button", { name: "返回记录" }).click();
      await expect(row).toBeFocused();
      expect(await ledger.evaluate(node => node.scrollTop)).toBe(before);
    }
    await dialog.getByRole("button", { name: "更早事件" }).click();
    await expect(dialog.getByRole("heading", { name: "160 条已加载记录" })).toBeVisible();
    await expect(row).toBeInViewport();
    await dialog.getByText("会话信息", { exact: true }).click();
    await expect(dialog.locator(".trace-session-details .trace-facts div").filter({ has: page.getByText("Plan generation", { exact: true }) }).locator("dd")).toHaveText("2");
    await dialog.getByText("会话信息", { exact: true }).click();
    await dialog.getByRole("searchbox").fill("未出现的记录");
    await expect(dialog.getByText("已加载记录中没有匹配项")).toBeVisible();
    await dialog.getByRole("button", { name: "清除筛选" }).click();
    await dialog.getByRole("combobox", { name: "记录类型" }).selectOption("error");
    await expect(dialog.locator(".trace-row")).toHaveCount(1);
    await expect(dialog.locator(".trace-row")).toContainText("运行验证 150");
    await page.screenshot({ path: testInfo.outputPath("trace-reader-filter.png"), animations: "disabled" });
  });

  test("uses the same reader on the exact-session page", async ({ page }) => {
    const address = await traceFixture(page);
    await page.goto(workerPath);
    await page.getByRole("button", { name: "查看当前 Agent Trace" }).click();
    await expect(page.locator(".trace-row").first()).toBeVisible();
    expect(address()).toContain("/sessions/");
    await page.goto(address());
    await expect(page.locator("#worker-trace-title")).toHaveText("Worker 轨迹");
    await page.getByRole("searchbox").fill("运行验证 150");
    await page.getByRole("button", { name: /运行验证 150/ }).click();
    await expect(page.getByRole("region", { name: "轨迹详情" })).toContainText("失败");
    await expect(page.locator("#trace-details-title")).toBeInViewport();
  });
});
