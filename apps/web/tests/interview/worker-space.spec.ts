import { expect, test } from "./fixtures";

test.use({ demoScenario: "mainline" });

test("F05/F07 WorkerItem keeps its current step, intervention draft, and trace entry", async ({
  interview,
  page,
}) => {
  for (const id of ["F05-01", "F05-02", "F07-01"]) {
    test.info().annotations.push({ type: "action", description: id });
  }

  await interview.openChat();
  await interview.sendRequirement("为项目增加 CSV 批量导入功能，手机号为空的策略在执行时向用户确认");
  await interview.openProposal();
  await interview.confirmPlan();
  await expect.poll(async () => (await interview.getFacts()).parallelObserved).toBe(true);
  await interview.releaseGate("parallel");
  await interview.openAttention("validate");

  await expect(page.locator("#worker-item-title")).toBeVisible();
  await expect(page.getByRole("region", { name: "当前进展", exact: true })).toBeVisible();
  await expect(page.getByRole("region", { name: "WorkerItem 活动时间线", exact: true })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "介入这个 Worker", exact: true })).toBeEnabled();
  await expect(page.getByRole("button", { name: "查看当前 Agent Trace", exact: true })).toBeVisible();

  const composer = page.getByRole("region", { name: "WorkerItem 工作区" }).or(
    page.locator(".worker-item-page"),
  ).first();
  const input = page.getByRole("textbox", { name: "介入这个 Worker", exact: true });
  await input.fill("保留当前规则并补充一条边界说明");
  await page.reload();
  await expect(input).toHaveValue("保留当前规则并补充一条边界说明");
  await expect(page.getByText("查看精确 context", { exact: true })).toBeVisible();
  await expect(composer).toBeVisible();

  await interview.openTrace();
  await expect(page.getByRole("dialog", { name: "Agent Trace", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "关闭 Agent Trace", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "Agent Trace", exact: true })).toHaveCount(0);
  await expect(input).toHaveValue("保留当前规则并补充一条边界说明");
});
