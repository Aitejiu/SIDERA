import { expect, test } from "./fixtures";

test.use({ demoScenario: "recovery" });

test("X3-recovery resumes the input window and changes the Worker artifact", async ({
  interview,
  page,
}) => {
  for (const actionId of ["F02-02", "F07-01", "F12-01", "F12-02", "F12-04"]) {
    test.info().annotations.push({ type: "action", description: actionId });
  }
  await interview.openChat();
  await interview.sendRequirement("为项目增加 CSV 批量导入功能，手机号为空的策略在执行时向用户确认");
  await interview.openProposal();
  await interview.confirmPlan();
  await expect.poll(async () => (await interview.getFacts()).parallelObserved).toBe(true);
  await interview.releaseGate("parallel");
  await interview.openAttention("validate");
  const before = await interview.getFacts();
  await interview.sendInput("手机号允许为空，有值时才校验格式");

  await interview.restart("gateway-worker");
  await expect.poll(async () => (await interview.getFacts()).workerItems.validate.inputIds)
    .toHaveLength(1);
  await interview.releaseGate("before-artifact");
  await interview.approvePendingRelease("review-report");
  await interview.approvePendingRelease("integrate");
  await expect.poll(
    async () => (await interview.getFacts()).candidateId,
    { timeout: 30_000 },
  ).not.toBeNull();
  await expect.poll(
    async () => (await interview.getFacts()).verificationStatus,
    { timeout: 30_000 },
  ).toBe("passed");
  const after = await interview.getFacts();

  expect(after.planGeneration).toBe(before.planGeneration);
  expect(after.workerItems.validate.inputIds.length).toBeGreaterThan(0);
  expect(after.workerItems.validate.inputIds).toHaveLength(1);
  expect(after.candidateId).not.toBeNull();
  expect(after.candidateHash).not.toBeNull();
  expect(after.candidateId).not.toBe(before.candidateId);

  const resultResponse = await page.request.get(
    new URL(
      `/api/v1/workspaces/${interview.manifest.workspace_id}/tasks/${after.rootTaskId}/results`,
      interview.manifest.endpoints.control_plane,
    ).toString(),
  );
  expect(resultResponse.ok()).toBeTruthy();
  const result = await resultResponse.json() as {
    current_candidate?: { artifacts?: Array<{ artifact_id?: string }> };
  };
  const artifactIds = (result.current_candidate?.artifacts ?? [])
    .map((artifact) => artifact.artifact_id)
    .filter((artifactId): artifactId is string => typeof artifactId === "string");
  const artifactContents = await Promise.all(artifactIds.map(async (artifactId) => {
    const response = await page.request.get(
      new URL(
        `/api/v1/workspaces/${interview.manifest.workspace_id}/artifacts/${artifactId}/content`,
        interview.manifest.endpoints.control_plane,
      ).toString(),
    );
    return response.ok() ? response.text() : "";
  }));
  expect(artifactContents.some((content) => content.includes("phone_optional=True"))).toBe(true);
});

test("X3-recovery reuses a published candidate after result-worker restart", async ({
  interview,
}) => {
  test.info().annotations.push({ type: "action", description: "F12-04" });
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
    async () => (await interview.getFacts()).candidateId,
    { timeout: 30_000 },
  ).not.toBeNull();
  const published = await interview.getFacts();
  expect(published.candidateHash).not.toBeNull();

  await interview.restart("result-worker");

  await expect.poll(
    async () => (await interview.getFacts()).candidateId,
    { timeout: 30_000 },
  ).toBe(published.candidateId);
  await expect.poll(
    async () => (await interview.getFacts()).verificationStatus,
    { timeout: 30_000 },
  ).toBe("passed");
  const recovered = await interview.getFacts();
  expect(recovered.candidateId).toBe(published.candidateId);
  expect(recovered.candidateHash).toBe(published.candidateHash);
});

test("X3-recovery restores the Main Agent turn without duplicating its message", async ({
  interview,
  page,
}) => {
  test.info().annotations.push({ type: "action", description: "F02-02" });
  test.info().annotations.push({ type: "action", description: "F12-03" });
  await interview.openChat();
  const input = "请保留这条恢复测试消息，不要重复创建回合";
  await page.getByRole("textbox", { name: "给 Main Agent 的消息", exact: true }).fill(input);
  await page.getByRole("button", { name: "发送消息", exact: true }).click();
  await expect(page.locator(".chat-message--user").filter({ hasText: input })).toHaveCount(1);

  await interview.restart("main-agent-gateway");
  await page.reload();

  await expect(page.locator(".chat-message--user").filter({ hasText: input })).toHaveCount(1);
  await expect(page.getByRole("main", { name: "Main Agent 对话工作面" })).toContainText(input);
});
