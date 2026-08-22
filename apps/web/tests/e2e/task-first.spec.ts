import {
  test,
  expect,
  IMPLEMENTER_WORKER_ITEM_ID,
  ROOT_TASK_ID,
  SECOND_ROOT_TASK_ID,
  WORKSPACE_ID,
} from "./fixtures";

test.describe("Task First control room", () => {
  test.beforeEach(({ seedR4 }) => {
    seedR4();
  });

  test("moves from Root Task to Subtask, WorkerItem, Trace, and intervention", async ({ page }) => {
    await page.goto(`/workspaces/${WORKSPACE_ID}/tasks`);
    await expect(page.getByRole("heading", { name: "任务", exact: true })).toBeVisible();

    const taskLink = page.getByRole("link", { name: "打开任务 支付重构" });
    await expect(taskLink).toBeVisible();
    await expect(taskLink).toContainText("GEN 2");
    await expect(taskLink).toContainText("运行中 1");
    await expect(taskLink).toContainText("需要我");
    await taskLink.click();

    await expect(page.locator("#task-detail-title")).toHaveText("支付重构");
    await expect(page.getByRole("heading", { name: "需要我", exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "确认已处理" })).toBeVisible();
    await expect(page.getByRole("button", { name: "打开子任务：回调幂等" })).toBeVisible();

    await page.getByRole("button", { name: "打开子任务：回调幂等" }).click();
    await expect(page.locator("#subtask-title")).toHaveText("回调幂等");
    await expect(page.getByRole("button", { name: "打开 Worker：实现者" })).toBeVisible();
    await expect(page.getByRole("button", { name: "打开 Worker：测试者" })).toBeVisible();
    await expect(page.getByRole("button", { name: "打开 Worker：审查者" })).toBeVisible();

    await page.getByRole("button", { name: "打开 Worker：实现者" }).click();
    await expect(page).toHaveURL(new RegExp(`/worker-items/${IMPLEMENTER_WORKER_ITEM_ID}$`));
    await expect(page.locator("#worker-item-title")).toHaveText("实现者");
    await expect(page.getByText("只修改这个回调重试规则", { exact: true })).toBeVisible();
    await expect(page.getByRole("heading", { name: "介入这个 Worker", exact: true })).toBeVisible();

    const traceButton = page.getByRole("button", { name: "查看 Agent Trace" }).first();
    await expect(traceButton).toBeVisible();
    await traceButton.click();
    await expect(page.getByRole("dialog", { name: "Agent Trace" })).toBeVisible();
    await expect(page.getByRole("dialog", { name: "Agent Trace" }).getByText("Plan generation 2", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "关闭 Agent Trace" }).click();
    await expect(page.getByRole("dialog", { name: "Agent Trace" })).toHaveCount(0);

    await page.getByRole("textbox", { name: "介入这个 Worker" }).fill("先补并发测试");
    await page.getByRole("button", { name: "发送到当前 Session" }).click();
    await expect(page.getByText("先补并发测试", { exact: true })).toBeVisible();
  });

  test("shows dependency edges and acknowledges a blocked child", async ({ page }) => {
    await page.goto(`/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}`);
    await expect(page.locator("#task-detail-title")).toHaveText("支付重构");
    await expect(page.getByRole("button", { name: "确认已处理" })).toBeVisible();

    await page.getByRole("tab", { name: "DAG", exact: true }).click();
    await expect(page.getByRole("region", { name: "任务 DAG" })).toBeVisible();
    await expect(page.locator(".edge-summary")).toHaveCount(2);
    await expect(page.getByText("依赖：callback-idempotency → integration", { exact: true })).toBeVisible();
    await expect(page.getByText("依赖：callback-test → integration", { exact: true })).toBeVisible();

    await page.getByRole("button", { name: "确认已处理" }).click();
    await expect(page.getByText("当前没有待确认事项", { exact: true })).toBeVisible();
    await expect(page.locator(".edge-summary")).toHaveCount(2);
  });

  test("keeps plan history read-only and exposes the generation comparison", async ({ page }) => {
    await page.goto(`/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}`);
    await expect(page.locator("#task-detail-title")).toBeVisible();
    await page.getByRole("link", { name: /计划历史/ }).click();

    await expect(page.locator("#plan-generation-title")).toHaveText("计划 Generation 2");
    await expect(page.getByRole("link", { name: "Generation 1" })).toBeVisible();
    await page.getByRole("link", { name: "比较当前" }).click();

    await expect(page.locator("#plan-diff-title")).toHaveText("计划差异");
    await expect(page.getByText("此页是历史证据与建议视图，不提供 Apply、Stop 或 Reassign 操作。", { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: /Apply|Stop|Reassign/i })).toHaveCount(0);
  });

  test("keeps another Root Task available as a separate workspace tab", async ({ page }) => {
    await page.goto(`/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}`);
    await expect(page.locator("#task-detail-title")).toHaveText("支付重构");
    await page.goto(`/workspaces/${WORKSPACE_ID}/tasks/${SECOND_ROOT_TASK_ID}`);
    await expect(page.locator("#task-detail-title")).toHaveText("发布监控升级");

    const tabs = page.getByRole("navigation", { name: "已打开的任务" });
    await expect(tabs.getByText("支付重构", { exact: true })).toBeVisible();
    await expect(tabs.getByText("发布监控升级", { exact: true })).toBeVisible();
  });
});
