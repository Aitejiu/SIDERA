import { expect, test } from "@playwright/test";

const workspaceId = process.env.SIDERA_BASIC_MAINTENANCE_WORKSPACE_ID;
if (workspaceId === undefined || workspaceId.trim() === "") {
  throw new Error("SIDERA_BASIC_MAINTENANCE_WORKSPACE_ID is required");
}

const rootTaskId = "30000000-0000-0000-0000-000000000001";
const implementerWorkerItemId = "a1000000-0000-0000-0000-000000000001";
const knowledgePageId = "c0000000-0000-0000-0000-000000000002";
const threadId = `maintenance-thread-${workspaceId}`;

test.describe("restored maintenance runtime", () => {
  test("@closure-maintenance reads the restored product path and rejects every write", async ({ page }) => {
    const writes: string[] = [];
    page.on("request", (request) => {
      if (!new Set(["GET", "HEAD", "OPTIONS"]).has(request.method())) {
        writes.push(`${request.method()} ${new URL(request.url()).pathname}`);
      }
    });

    await page.goto(`/workspaces/${workspaceId}/tasks/${rootTaskId}`);
    await expect(page.locator("#task-detail-title")).toHaveText("支付重构");
    await page.getByRole("tab", { name: "DAG", exact: true }).click();
    await expect(page.getByRole("region", { name: "任务 DAG" })).toBeVisible();
    await page.getByRole("tab", { name: "看板", exact: true }).click();
    await page.getByRole("button", { name: "打开子任务：回调幂等" }).click();
    await expect(page.locator("#subtask-title")).toHaveText("回调幂等");
    await page.getByRole("button", { name: "打开 Worker：实现者" }).click();
    await expect(page).toHaveURL(new RegExp(`/worker-items/${implementerWorkerItemId}$`));
    await expect(page.locator("#worker-item-title")).toHaveText("实现者");
    await page.getByRole("button", { name: "查看 Agent Trace" }).first().click();
    await expect(page.getByRole("dialog", { name: "Agent Trace" })).toBeVisible();
    await page.getByRole("button", { name: "关闭 Agent Trace" }).click();
    await expect(page.getByRole("dialog", { name: "Agent Trace" })).toHaveCount(0);

    await page.goto(`/workspaces/${workspaceId}/tasks/${rootTaskId}?view=results`);
    await expect(page.getByRole("region", { name: "成果验收" })).toBeVisible();

    await page.goto(`/workspaces/${workspaceId}/knowledge`);
    await expect(page.locator("#knowledge-library-title")).toHaveText("知识库");
    await page.goto(`/workspaces/${workspaceId}/knowledge/pages/${knowledgePageId}`);
    await expect(page.locator("#knowledge-article-title")).toHaveText("Worker context reference");

    await page.goto(`/workspaces/${workspaceId}/chat/${encodeURIComponent(threadId)}`);
    await expect(page.locator("#chat-page-title")).toHaveText("恢复后的维护历史");
    await expect(page.getByText("恢复副本中的主 Agent 历史仍然可读。", { exact: true })).toBeVisible();
    expect(writes).toEqual([]);

    const controlWrite = await page.request.post("/api/v1/commands", { data: {} });
    expect(controlWrite.status()).toBe(409);
    expect((await controlWrite.json()).error.code).toBe("restore_read_only");

    const gatewayWrite = await page.request.post("/main-agent/v1/threads", {
      data: { title: "should reject", context_refs: [] },
    });
    expect(gatewayWrite.status()).toBe(409);
    expect((await gatewayWrite.json()).error.code).toBe("restore_read_only");

    await page.goto(`/workspaces/${workspaceId}/tasks/${rootTaskId}`);
    await expect(page.locator("#task-detail-title")).toHaveText("支付重构");
  });
});
