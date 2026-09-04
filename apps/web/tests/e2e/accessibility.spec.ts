import { AxeBuilder } from "@axe-core/playwright";
import type { Page } from "@playwright/test";

import {
  test,
  expect,
  IMPLEMENTER_WORKER_ITEM_ID,
  R5_WORKSPACE_ID,
  mockNotificationInbox,
  runPlanningWorkerOnce,
  WORKSPACE_ID,
} from "./fixtures";

test.describe("accessible R4 routes", () => {
  test.beforeEach(({ seedR4 }) => {
    seedR4();
  });

  test("has no axe violations on task, plan, settings, and WorkerItem surfaces", async ({ page }) => {
    await page.goto(`/workspaces/${WORKSPACE_ID}/tasks`);
    await expect(page.getByRole("heading", { name: "任务", exact: true })).toBeVisible();
    await audit(page, "task list");

    await page.getByRole("link", { name: "打开任务 支付重构" }).click();
    await expect(page.locator("#task-detail-title")).toBeVisible();
    await audit(page, "task detail");

    await page.getByRole("tab", { name: "DAG", exact: true }).click();
    await expect(page.locator(".edge-summary")).toHaveCount(2);
    await audit(page, "task DAG");

    await page.getByRole("link", { name: /计划历史/ }).click();
    await expect(page.locator("#plan-generation-title")).toBeVisible();
    await audit(page, "plan history");

    await page.getByRole("link", { name: "比较当前" }).click();
    await expect(page.locator("#plan-diff-title")).toBeVisible();
    await audit(page, "plan diff");

    await page.goto(`/workspaces/${WORKSPACE_ID}/settings`);
    await expect(page.locator("#notification-settings-title")).toBeVisible();
    await audit(page, "settings");

    await page.goto(`/workspaces/${WORKSPACE_ID}/worker-items/${IMPLEMENTER_WORKER_ITEM_ID}`);
    await expect(page.locator("#worker-item-title")).toHaveText("实现者");
    await audit(page, "WorkerItem workspace");

    await page.getByRole("button", { name: "查看 Agent Trace" }).first().click();
    await expect(page.getByRole("dialog", { name: "Agent Trace" })).toBeVisible();
    await audit(page, "Agent Trace dialog");
  });

  test("reaches task selection, WorkerItem, Send, and Stop by keyboard", async ({ page }) => {
    await page.goto(`/workspaces/${WORKSPACE_ID}/tasks`);
    const taskLink = page.getByRole("link", { name: "打开任务 支付重构" });
    await expect(taskLink).toBeVisible();

    await taskLink.focus();
    await page.keyboard.press("Enter");
    await expect(page.locator("#task-detail-title")).toBeVisible();

    const subtask = page.getByRole("button", { name: "打开子任务：回调幂等" });
    await subtask.focus();
    await page.keyboard.press("Enter");
    await expect(page.locator("#subtask-title")).toHaveText("回调幂等");

    const worker = page.getByRole("button", { name: "打开 Worker：实现者" });
    await worker.focus();
    await page.keyboard.press("Enter");
    await expect(page.locator("#worker-item-title")).toHaveText("实现者");

    const traceButton = page.getByRole("button", { name: "查看 Agent Trace" }).first();
    await traceButton.focus();
    await page.keyboard.press("Enter");
    await expect(page.getByRole("dialog", { name: "Agent Trace" })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog", { name: "Agent Trace" })).toHaveCount(0);

    const input = page.getByRole("textbox", { name: "介入这个 Worker" });
    await input.focus();
    await input.fill("通过键盘验证精确 Session");
    const send = page.getByRole("button", { name: "发送到当前 Session" });
    await send.focus();
    await page.keyboard.press("Enter");
    await expect(page.getByText("已接受", { exact: true })).toBeVisible();

    page.once("dialog", (dialog) => void dialog.accept());
    const stop = page.getByRole("button", { name: "停止 Execution" });
    await stop.focus();
    await page.keyboard.press("Enter");
    await expect(page.getByText("停止已请求", { exact: true })).toBeVisible();
  });

  test("has no axe violations on the planning proposal review surfaces", async ({ page, seedR5 }) => {
    seedR5("proposal");
    await page.goto(`/workspaces/${R5_WORKSPACE_ID}/tasks`);
    await page.getByRole("region", { name: "任务" }).getByRole("link", { name: "新建任务" }).click();
    await page.getByLabel("需求目标").fill("验证规划方案审阅界面的可访问性");
    await page.getByRole("textbox", { name: "验收条件" }).first().fill("键盘和辅助技术可以审阅执行架构");
    await page.getByRole("button", { name: "提交需求" }).click();
    await expect(page).toHaveURL(new RegExp(`/workspaces/${R5_WORKSPACE_ID}/planning/[0-9a-f-]{36}$`));
    expect(runPlanningWorkerOnce().succeeded).toBe(1);
    await page.reload();
    await expect(page.getByRole("heading", { name: "执行架构", exact: true })).toBeVisible();
    await audit(page, "planning architecture");

    await page.getByRole("tab", { name: "依赖 DAG", exact: true }).click();
    await expect(page.getByTestId("proposal-graph-canvas")).toBeVisible();
    await audit(page, "planning DAG");

    await page.getByRole("tab", { name: "变更", exact: true }).click();
    await expect(page.getByRole("heading", { name: "变更", exact: true })).toBeVisible();
    await audit(page, "planning diff");
  });

  test("has no axe violations on the persistent notification inbox", async ({ page }) => {
    await mockNotificationInbox(page);
    await page.goto(`/workspaces/${WORKSPACE_ID}/inbox`);
    await expect(page.getByRole("heading", { name: "信箱", exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "展开需要确认执行分支" })).toBeVisible();
    await audit(page, "notification inbox");

    await page.getByRole("button", { name: "展开需要确认执行分支" }).click();
    await expect(page.getByRole("region", { name: "需要确认执行分支的线程历史" })).toBeVisible();
    await audit(page, "notification thread history");
  });
});

test.describe("accessible runtime routing", () => {
  test("keeps the queued execution node chooser accessible", async ({ page, seedR4 }) => {
    seedR4("runtime-routing");
    await page.goto(`/workspaces/${WORKSPACE_ID}/worker-items/${IMPLEMENTER_WORKER_ITEM_ID}`);
    await expect(page.locator("#worker-item-title")).toHaveText("实现者");
    await page.locator(".worker-item-routing").getByRole("button", { name: "调整执行节点" }).click();
    await expect(page.getByRole("region", { name: "执行节点选择器" })).toBeVisible();
    await audit(page, "runtime routing chooser");
  });
});

async function audit(page: Page, surface: string): Promise<void> {
  const result = await new AxeBuilder({ page }).analyze();
  expect(result.violations, `${surface} has accessibility violations`).toEqual([]);
}
