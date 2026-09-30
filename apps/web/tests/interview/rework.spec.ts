import { test, expect } from "./fixtures";

test.use({ demoScenario: "rework" });

test("X3-rework observes a real failed check and a repaired candidate", async ({ interview, page }) => {
  for (const actionId of ["F11-01", "F11-02", "F11-05"]) {
    test.info().annotations.push({ type: "action", description: actionId });
  }
  await interview.openChat();
  await interview.sendRequirement("为项目增加 CSV 批量导入功能，手机号为空的策略在执行时向用户确认");
  await interview.openProposal();
  await interview.confirmPlan();
  await expect.poll(async () => (await interview.getFacts()).parallelObserved).toBe(true);
  await interview.releaseGate("parallel");
  await interview.openAttention("validate");
  await interview.sendInput("手机号允许为空，有值时才校验格式");
  await interview.releaseGate("before-artifact");
  await interview.approvePendingRelease("review-report");
  await interview.approvePendingRelease("integrate");

  await expect.poll(
    async () => (await interview.getFacts()).verificationStatus,
    { timeout: 30_000 },
  ).toBe("failed");
  const failed = await interview.getFacts();
  expect(failed.candidateId).not.toBeNull();

  await interview.openWorker("integrate");
  await expect(page.getByRole("button", { name: "局部返工", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "局部返工", exact: true }).click();
  const form = page.getByRole("form", { name: "局部返工", exact: true });
  await form.getByLabel("返工说明", { exact: true }).fill("修复手机号允许为空的集成实现");
  await form.getByRole("button", { name: "提交局部返工", exact: true }).click();

  await expect(page.getByRole("button", { name: "重新集成", exact: true })).toBeVisible({ timeout: 30_000 });
  await page.getByRole("button", { name: "重新集成", exact: true }).click();
  const reintegrateForm = page.getByRole("form", { name: "重新集成", exact: true });
  await reintegrateForm.getByLabel("操作原因", { exact: true }).fill("合并修复后的 WorkerItem 结果");
  await reintegrateForm.getByRole("button", { name: "提交重新集成", exact: true }).click();

  await expect.poll(
    async () => (await interview.getFacts()).candidateId,
    { timeout: 30_000 },
  ).not.toBe(failed.candidateId);
  await expect.poll(
    async () => (await interview.getFacts()).verificationStatus,
    { timeout: 30_000 },
  ).toBe("passed");

  const repaired = await interview.getFacts();
  expect(repaired.candidateId).not.toBe(failed.candidateId);
  await interview.openRoot();
  await interview.acceptCurrentCandidate();
  await expect.poll(
    async () => (await interview.getFacts()).completed,
    { timeout: 30_000 },
  ).toBe(true);
});
