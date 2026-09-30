import { expect, test } from "./fixtures";

test.use({ demoScenario: "history" });

test("history keeps the Main Agent object link and an accepted result recoverable", async ({
  interview,
  page,
}) => {
  test.info().annotations.push({ type: "action", description: "F02-04" });
  test.info().annotations.push({ type: "action", description: "F08-06" });

  await interview.openChat();
  await interview.sendRequirement("为项目增加 CSV 批量导入功能，手机号为空的策略在执行时向用户确认");
  const objectLink = page.getByRole("link", { name: "查看对象", exact: true });
  await expect(objectLink).toBeVisible({ timeout: 30_000 });
  expect(await objectLink.getAttribute("href")).toMatch(/\/planning\/[0-9a-f-]{36}$/i);
  await objectLink.click();
  await expect(page).toHaveURL(/\/planning\/[0-9a-f-]{36}$/i);

  await interview.confirmPlan();
  await expect.poll(async () => (await interview.getFacts()).parallelObserved).toBe(true);
  await interview.releaseGate("parallel");
  await interview.openAttention("validate");
  await interview.sendInput("手机号允许为空，有值时才校验格式");
  await interview.releaseGate("before-artifact");
  await interview.approvePendingRelease("review-report");
  await interview.approvePendingRelease("integrate");
  await expect.poll(async () => (await interview.getFacts()).verificationStatus, { timeout: 60_000 }).toBe("passed");
  await interview.openRoot();
  await interview.acceptCurrentCandidate();
  await expect.poll(async () => (await interview.getFacts()).completed, { timeout: 60_000 }).toBe(true);

  const facts = await interview.getFacts();
  expect(facts.rootTaskId).not.toBeNull();
  expect(facts.acceptedCandidateId).not.toBeNull();
  const resultResponse = await page.request.get(new URL(
    `/api/v1/workspaces/${interview.manifest.workspace_id}/tasks/${facts.rootTaskId}/results`,
    interview.manifest.endpoints.control_plane,
  ).toString());
  expect(resultResponse.ok()).toBeTruthy();
  const result = await resultResponse.json() as {
    result_stage?: string;
    acceptance?: { candidate_id?: string | null };
    current_candidate?: { candidate_hash?: string | null; artifacts?: Array<{ artifact_id?: string }> };
  };
  expect(result.result_stage).toBe("completed");
  expect(result.acceptance?.candidate_id).toBe(facts.acceptedCandidateId);
  expect(result.current_candidate?.candidate_hash).toBe(facts.candidateHash);
  const artifactIds = result.current_candidate?.artifacts?.map((artifact) => artifact.artifact_id).filter(
    (value): value is string => typeof value === "string",
  ) ?? [];
  let artifactId: string | undefined;
  let artifactBody = "";
  for (const candidateArtifactId of artifactIds) {
    const content = await page.request.get(new URL(
      `/api/v1/workspaces/${interview.manifest.workspace_id}/artifacts/${candidateArtifactId}/content`,
      interview.manifest.endpoints.control_plane,
    ).toString());
    if (!content.ok()) continue;
    const body = await content.text();
    if (!body.includes("phone_optional=True")) continue;
    artifactId = candidateArtifactId;
    artifactBody = body;
    break;
  }
  expect(artifactId).toBeDefined();
  expect(artifactBody).toContain("phone_optional=True");

  await page.goto(new URL(
    `/workspaces/${interview.manifest.workspace_id}/history`,
    interview.manifest.endpoints.web,
  ).toString());
  const historyLink = page.getByRole("link").filter({ hasText: "CSV" }).first();
  await expect(historyLink).toBeVisible();
  await historyLink.click();
  await expect(page).toHaveURL(new RegExp(`/tasks/${facts.rootTaskId}(?:\\?.*)?$`));
  await expect(page.getByRole("tab", { name: "成果", exact: true })).toBeVisible();
});
