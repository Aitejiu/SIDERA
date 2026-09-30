import { expect, IMPLEMENTER_WORKER_ITEM_ID, ROOT_TASK_ID, test, WORKSPACE_ID } from "./fixtures";

test.describe("R6 basic product read-only path", () => {
  test.beforeEach(({ seedR4, seedKnowledge }) => {
    seedR4("context-knowledge");
    seedKnowledge();
  });

  test("walks the persisted task hierarchy, trace, chat, inbox, and context without writes", async ({ page }) => {
    const writes: string[] = [];
    page.on("request", (request) => {
      if (!new Set(["GET", "HEAD", "OPTIONS"]).has(request.method())) {
        writes.push(`${request.method()} ${new URL(request.url()).pathname}`);
      }
    });

    await page.goto(`/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}`);
    await expect(page.locator("#task-detail-title")).toHaveText("支付重构");
    await page.getByRole("tab", { name: "DAG", exact: true }).click();
    await expect(page.getByRole("region", { name: "任务 DAG" })).toBeVisible();
    await page.getByRole("tab", { name: "看板", exact: true }).click();

    await page.getByRole("button", { name: "打开子任务：回调幂等" }).click();
    await expect(page.locator("#subtask-title")).toHaveText("回调幂等");
    await page.getByRole("button", { name: "打开 Worker：实现者" }).click();
    await expect(page).toHaveURL(new RegExp(`/worker-items/${IMPLEMENTER_WORKER_ITEM_ID}$`));
    await expect(page.locator("#worker-item-title")).toHaveText("实现者");

    await page.getByRole("button", { name: "查看 Agent Trace" }).first().click();
    await expect(page.getByRole("dialog", { name: "Agent Trace" })).toBeVisible();
    await page.getByRole("button", { name: "关闭 Agent Trace" }).click();
    await expect(page.getByRole("dialog", { name: "Agent Trace" })).toHaveCount(0);

    await page.goto(`/workspaces/${WORKSPACE_ID}/inbox`);
    await expect(page.locator("#notification-inbox-title")).toHaveText("信箱");
    await page.goto(`/workspaces/${WORKSPACE_ID}/chat`);
    await expect(page.locator("#chat-page-title")).toHaveText("Main Agent");
    await page.goto(`/workspaces/${WORKSPACE_ID}/context-packages/b0000000-0000-0000-0000-000000000005`);
    await expect(page.locator("#context-package-title")).toHaveText("上下文包");
    expect(writes).toEqual([]);
  });
});
