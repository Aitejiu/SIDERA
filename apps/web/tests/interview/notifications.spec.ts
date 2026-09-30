import { expect, test } from "./fixtures";

test("F15 notification sink deep-links an attention thread to its WorkerItem", async ({
  interview,
  page,
}) => {
  for (const actionId of ["F15-01", "F15-02", "F15-03"]) {
    test.info().annotations.push({ type: "action", description: actionId });
  }

  await interview.openChat();
  await interview.sendRequirement(
    "为项目增加 CSV 批量导入功能，手机号为空的策略在执行时向用户确认",
  );
  await interview.openProposal();
  await interview.confirmPlan();
  await expect.poll(async () => (await interview.getFacts()).parallelObserved).toBe(true);
  await interview.releaseGate("parallel");
  await interview.openAttention("validate");

  await page.goto(new URL(
    `/workspaces/${interview.manifest.workspace_id}/inbox`,
    interview.manifest.endpoints.web,
  ).toString());
  await expect(page.getByRole("heading", { name: "信箱", exact: true })).toBeVisible();
  const thread = page.locator("article.notification-thread").first();
  await expect(thread).toBeVisible({ timeout: 30_000 });
  await expect(thread).toContainText("校验");
  await thread.getByRole("button").first().click();
  const history = thread.locator(".notification-thread-history");
  await expect(history).toBeVisible();
  await expect(history.getByRole("button", { name: "打开 WorkerItem", exact: true })).toBeVisible();
  await history.getByRole("button", { name: "打开 WorkerItem", exact: true }).click();
  await expect(page).toHaveURL(/\/worker-items\/[0-9a-f-]{36}/i);
  await expect(page.getByRole("heading", { name: /校验/, exact: false }).first()).toBeVisible();

  await page.goBack();
  await expect(thread).toBeVisible();
  await thread.getByRole("button").first().click();
  const afterOpen = thread.locator(".notification-thread-history");
  await expect(afterOpen.getByRole("button", { name: "标记已读", exact: true })).toBeVisible();
  await afterOpen.getByRole("button", { name: "标记已读", exact: true }).click();
  await expect(afterOpen.getByRole("button", { name: "标记已读", exact: true })).toHaveCount(0);
});
