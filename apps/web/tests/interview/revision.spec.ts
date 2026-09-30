import { test, expect } from "./fixtures";

test.use({ demoScenario: "revision" });

test("X1 revision keeps completed reporting work and adds TSV", async ({ interview, page }) => {
  for (const actionId of ["F09-01", "F09-02", "F09-03", "F09-04"]) {
    test.info().annotations.push({ type: "action", description: actionId });
  }
  await interview.openChat();
  const chatUrl = page.url();
  await interview.sendRequirement("为项目增加 CSV 批量导入功能，手机号为空的策略在执行时向用户确认");
  await interview.openProposal();
  await interview.confirmPlan();
  await expect.poll(async () => (await interview.getFacts()).parallelObserved).toBe(true);
  await interview.releaseGate("parallel");
  await interview.openAttention("validate");

  const before = await interview.getFacts();
  expect(before.rootTaskId).not.toBeNull();
  expect(before.workerItems.report).toBeDefined();
  expect(before.workerItems["review-report"]).toBeDefined();
  expect(before.planGeneration).toBe(1);

  await interview.releaseGate("before-artifact");
  await interview.approvePendingRelease("review-report");
  await page.goto(chatUrl);
  await interview.sendRequirement("还需要支持 TSV 文件");
  await interview.openLatestRevisionProposal();

  expect((await interview.getFacts()).planGeneration).toBe(before.planGeneration);
  await expect(page.getByText(/优先复用已有 WorkerItem|保留/, { exact: false }).first()).toBeVisible();
  await expect(page.getByText(/TSV/, { exact: false }).first()).toBeVisible();

  await interview.confirmRevision();
  await expect.poll(async () => (await interview.getFacts()).planGeneration).toBe((before.planGeneration ?? 0) + 1);
  const after = await interview.getFacts();
  expect(after.workerItems.report.id).toBe(before.workerItems.report.id);
  expect(after.workerItems["review-report"]?.id).toBe(before.workerItems["review-report"]?.id);
  expect(after.workerItems["parse-tsv"]).toBeDefined();

  await interview.openWorker("report");
  await expect(page.getByRole("heading", { name: /错误报告|报告/, exact: false }).first()).toBeVisible();
});
