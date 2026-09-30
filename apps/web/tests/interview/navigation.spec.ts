import { expect, test } from "./fixtures";

test.use({ demoScenario: "mainline" });

test("F01/F04/F05/F15/F17 nested task navigation keeps route and content aligned", async ({
  interview,
  page,
}) => {
  for (const id of [
    "F01-01",
    "F01-03",
    "F01-05",
    "F04-01",
    "F04-02",
    "F04-04",
    "F05-01",
    "F15-02",
    "F17-02",
  ]) {
    test.info().annotations.push({ type: "action", description: id });
  }

  await interview.openChat();
  await interview.sendRequirement("为项目增加 CSV 批量导入功能，手机号为空的策略在执行时向用户确认");
  await interview.openProposal();
  await interview.confirmPlan();
  await expect.poll(async () => (await interview.getFacts()).parallelObserved).toBe(true);
  await interview.releaseGate("parallel");

  await interview.openRoot();
  const rootUrl = page.url();
  await expect(page.locator("#task-detail-title")).toBeVisible();
  const attention = page.getByRole("region", { name: "任务待处理事项", exact: true });
  const attentionLink = attention.locator('a[href*="/worker-items/"]').first();
  await expect(attentionLink).toBeVisible({ timeout: 90_000 });
  const attentionHref = await attentionLink.getAttribute("href");
  expect(attentionHref).toMatch(/\/worker-items\/[0-9a-f-]{36}$/i);
  await attentionLink.click();
  await expect(page).toHaveURL(new RegExp("/worker-items/[0-9a-f-]{36}$", "i"));
  await page.goBack();
  await expect(page).toHaveURL(rootUrl);
  const attentionAfterBack = page.getByRole("region", { name: "任务待处理事项", exact: true });
  await attentionAfterBack.getByRole("button", { name: "确认已处理", exact: true }).first().click();
  await expect(attentionAfterBack.getByText("当前没有待确认事项", { exact: true })).toBeVisible();

  await interview.openSubtask("reporting");
  const subtaskUrl = page.url();
  await expect(page.locator("#subtask-title")).toBeVisible();
  await expect(page.getByRole("region", { name: "Worker 看板", exact: true })).toBeVisible();

  await page.goBack();
  await expect(page).toHaveURL(rootUrl);
  await expect(page.locator("#task-detail-title")).toBeVisible();
  await page.goForward();
  await expect(page).toHaveURL(subtaskUrl);
  await expect(page.locator("#subtask-title")).toBeVisible();

  await interview.openWorker("report");
  const workerUrl = page.url();
  await expect(page.locator("#worker-item-title")).toBeVisible();
  await expect(page.getByRole("region", { name: "WorkerItem 活动时间线", exact: true })).toBeVisible();

  const tabs = page.getByRole("navigation", { name: "已打开的任务", exact: true });
  const activeTab = tabs.locator(".workspace-tab.is-active");
  await expect(activeTab).toBeVisible();
  await activeTab.getByRole("button", { name: /^关闭/ }).click();
  await expect(page).not.toHaveURL(workerUrl);
  await expect(page).toHaveURL(subtaskUrl);
  await expect(page.locator("#subtask-title")).toBeVisible();
  await expect(page.locator("#worker-item-title")).toHaveCount(0);

  await tabs.locator(".workspace-tab.is-active").getByRole("button", { name: /^关闭/ }).click();
  await expect(page).toHaveURL(rootUrl);
  await expect(page.locator("#task-detail-title")).toBeVisible();
  await expect(page.locator("#subtask-title")).toHaveCount(0);

  await page.goto(`${interview.manifest.endpoints.web}/workspaces/${interview.manifest.workspace_id}/history`);
  await expect(page.getByRole("heading", { name: "历史", exact: true })).toBeVisible();
  await expect(page.getByRole("link").filter({ hasText: /CSV|任务/ }).first()).toBeVisible();
});
