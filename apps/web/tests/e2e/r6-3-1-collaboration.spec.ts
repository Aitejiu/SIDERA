import { expect, test } from "./fixtures";

const ROOT_TITLE = "Collaboration runtime rollout";
const UPSTREAM_A_TITLE = "Prepare upstream A";
const DOWNSTREAM_TITLE = "Integrate downstream";

test.describe("R6.3.1 collaboration control room", () => {
  test.describe.configure({ mode: "serial", timeout: 120_000 });

  test("follows root, fan-in, WorkerItem trace, intervention, and decision tabs", async ({
    page,
    seedR6Collaboration,
    runR6CollaborationOnce,
    prepareR6CollaborationBrowser,
  }, testInfo) => {
    const seed = seedR6Collaboration();
    const healthySummary = runR6CollaborationOnce(seed.workspace_id);
    expect(healthySummary.run_status).toBe("active");

    await page.goto(`/workspaces/${seed.workspace_id}/tasks/${seed.root_task_id}`);
    await expect(page.locator("#task-detail-title")).toHaveText(ROOT_TITLE);
    await expect(page.getByRole("region", { name: "需要我" })).toContainText(
      "当前没有待确认事项",
    );
    await expect(page.getByRole("region", { name: "需要我" })).not.toContainText(
      DOWNSTREAM_TITLE,
    );
    const collaborationSummary = page.getByRole("region", { name: "协同调度" });
    await expect(collaborationSummary.getByRole("button", { name: `打开子任务：${DOWNSTREAM_TITLE}` })).toBeVisible();

    const browserSummary = prepareR6CollaborationBrowser(seed.workspace_id);
    expect(browserSummary.run_status).toBe("blocked");
    await page.reload();
    await expect(page.locator(".collaboration-summary--attention")).toBeVisible();
    await expect(page.locator('[aria-label="协同卡点"]')).toContainText(DOWNSTREAM_TITLE);
    await expect(page.getByRole("region", { name: "需要我" })).toContainText(
      "当前没有待确认事项",
    );

    await collaborationSummary.getByRole("button", { name: `打开子任务：${DOWNSTREAM_TITLE}` }).click();
    await expect(page).toHaveURL(
      new RegExp(`/workspaces/${seed.workspace_id}/tasks/${seed.root_task_id}/subtasks/subtask-downstream$`),
    );
    await expect(page.locator("#subtask-title")).toHaveText(DOWNSTREAM_TITLE);
    await expect(page.getByRole("heading", { name: "Worker 看板" })).toBeVisible();
    const fanIn = page.getByRole("region", { name: "输入汇聚" });
    await expect(fanIn).toContainText("等待上游");
    await expect(fanIn).toContainText(UPSTREAM_A_TITLE);
    await expect(fanIn).toContainText("Prepare upstream B");
    await expect(page.getByRole("button", { name: `打开 Worker：${DOWNSTREAM_TITLE}` })).toBeVisible();

    await page.getByRole("button", { name: `打开 Worker：${DOWNSTREAM_TITLE}` }).click();
    await expect(page.locator("#worker-item-title")).toHaveText(DOWNSTREAM_TITLE);
    const workerFacts = page.getByRole("complementary", { name: "WorkerItem 事实" });
    await expect(workerFacts).toContainText("等待必要产物");
    await expect(workerFacts.getByRole("link", { name: "查看上游：Prepare upstream A" })).toBeVisible();
    await expect(workerFacts.getByRole("link", { name: "查看上游：Prepare upstream B" })).toBeVisible();
    await expect(page.getByRole("textbox", { name: `介入这个 Worker` })).toBeDisabled();

    await workerFacts.getByRole("link", { name: "查看上游：Prepare upstream A" }).click();
    await expect(page.locator("#subtask-title")).toHaveText(UPSTREAM_A_TITLE);
    await page.getByRole("button", { name: `打开 Worker：${UPSTREAM_A_TITLE}` }).click();
    await expect(page.locator("#worker-item-title")).toHaveText(UPSTREAM_A_TITLE);
    await expect(page.getByRole("textbox", { name: "介入这个 Worker" })).toBeEnabled();

    const traceButton = page.getByRole("button", { name: "查看 Agent Trace" }).first();
    await expect(traceButton).toBeVisible();
    await traceButton.click();
    const traceDialog = page.getByRole("dialog", { name: "Agent Trace" });
    await expect(traceDialog).toBeVisible();
    await expect(traceDialog).toContainText("正在运行上游验证");
    await expect(traceDialog).toContainText("pytest");
    await traceDialog.getByRole("button", { name: "关闭 Agent Trace" }).click();
    await expect(traceDialog).toHaveCount(0);

    const tabs = page.getByRole("navigation", { name: "已打开的任务" });
    const rootTab = tabs.locator(
      `a[href="/workspaces/${seed.workspace_id}/tasks/${seed.root_task_id}"]`,
    );
    await expect(rootTab).toBeVisible();
    await rootTab.click();
    await expect(page.locator("#task-detail-title")).toHaveText(ROOT_TITLE);
    await page.getByRole("link", { name: /查看协同记录/ }).click();
    await expect(page.locator("#collaboration-detail-title")).toHaveText("协同记录");
    await expect(page.getByRole("heading", { name: "协同决策" })).toBeVisible();
    await expect(page.getByRole("article", { name: /第 \d+ 次协同决策/ }).first()).toBeVisible();
    await expect(tabs.getByRole("link", { name: "协同记录" })).toBeVisible();
    await expect(
      tabs.locator(
        `a[href="/workspaces/${seed.workspace_id}/tasks/${seed.root_task_id}"]`,
      ),
    ).toBeVisible();

    await tabs.getByRole("button", { name: "关闭协同记录" }).click();
    await expect(page).toHaveURL(new RegExp(`/workspaces/${seed.workspace_id}/tasks/${seed.root_task_id}$`));
    await expect(page.locator("#task-detail-title")).toHaveText(ROOT_TITLE);
    await expectNoHorizontalOverflow(page);
    await page.screenshot({
      path: testInfo.outputPath(`r6-3-1-collaboration-${testInfo.project.name}.png`),
      fullPage: true,
    });
  });
});

async function expectNoHorizontalOverflow(page: import("@playwright/test").Page): Promise<void> {
  const metrics = await page.evaluate(() => ({
    body: document.body.scrollWidth,
    document: document.documentElement.scrollWidth,
    viewport: window.innerWidth,
  }));
  expect(metrics.body).toBeLessThanOrEqual(metrics.viewport + 1);
  expect(metrics.document).toBeLessThanOrEqual(metrics.viewport + 1);
}
