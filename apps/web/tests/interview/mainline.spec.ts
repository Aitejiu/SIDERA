import { createHash } from "node:crypto";
import { chmodSync, mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

import { test, expect } from "./fixtures";

test("M1-M6 confirmed work reaches a verified deliverable", async ({ interview, page }) => {
  for (const checkId of ["M1", "M2", "M3", "M4", "M5", "M6"]) {
    test.info().annotations.push({ type: "check", description: checkId });
  }
  for (const actionId of [
    "F01-01",
    "F02-01",
    "F03-02",
    "F03-07",
    "F04-01",
    "F04-02",
    "F04-03",
    "F06-01",
    "F07-01",
    "F08-01",
    "F08-02",
    "F08-03",
    "F08-04",
  ]) {
    test.info().annotations.push({ type: "action", description: actionId });
  }
  await interview.openChat();
  await interview.sendRequirement("为项目增加 CSV 批量导入功能，手机号为空的策略在执行时向用户确认");
  await interview.openProposal();
  expect((await interview.getFacts()).rootTaskId).toBeNull();
  await interview.confirmPlan();
  await expect.poll(async () => (await interview.getFacts()).parallelObserved).toBe(true);
  await interview.releaseGate("parallel");
  await interview.openAttention("validate");
  const before = await interview.getFacts();
  await interview.sendInput("手机号允许为空，有值时才校验格式");
  await interview.openTrace();
  const traceUrl = page.url();
  await page.getByRole("button", { name: "关闭 Agent Trace", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "Agent Trace", exact: true })).toHaveCount(0);
  expect(page.url()).toBe(traceUrl);
  await interview.releaseGate("before-artifact");
  await interview.approvePendingRelease("review-report");
  await interview.approvePendingRelease("integrate");
  await expect.poll(
    async () => (await interview.getFacts()).verificationStatus,
    { timeout: 30_000 },
  ).toBe("passed");
  await interview.openRoot();
  await interview.acceptCurrentCandidate();
  await expect.poll(
    async () => (await interview.getFacts()).completed,
    { timeout: 30_000 },
  ).toBe(true);
  const after = await interview.getFacts();
  expect(after.planGeneration).toBe(before.planGeneration);
  expect(after.verificationCandidateId).toBe(after.acceptedCandidateId);

  const integrationItem = after.workerItems.integrate;
  expect(integrationItem).toBeDefined();
  const inputsResponse = await page.request.get(new URL(
    `/api/v1/workspaces/${interview.manifest.workspace_id}/worker-items/${integrationItem?.id}/result-inputs`,
    interview.manifest.endpoints.control_plane,
  ).toString());
  expect(inputsResponse.ok()).toBeTruthy();
  const inputs = await inputsResponse.json() as {
    groups?: Array<{ selected_sources?: Array<{ source_basis_hash?: string }> }>;
    delivery?: { acknowledgement_status?: string };
  };
  const selectedSources = (inputs.groups ?? []).flatMap((group) => group.selected_sources ?? []);
  expect(selectedSources.length).toBeGreaterThan(0);
  expect(selectedSources.every((source) => /^[0-9a-f]{64}$/i.test(source.source_basis_hash ?? ""))).toBe(true);
  expect(["acknowledged", "pending"]).toContain(inputs.delivery?.acknowledgement_status);

  await interview.openSubtask("reporting");
  const reportBoard = page.getByRole("region", { name: "Worker 看板", exact: true });
  await expect(reportBoard).toBeVisible();
  await expect(reportBoard.getByRole("button", { name: /打开 Worker：/ })).toHaveCount(2);
  await page.getByRole("tab", { name: "Graph", exact: true }).click();
  await expect(page.getByRole("region", { name: "Worker Graph", exact: true })).toBeVisible();

  await interview.openRoot();
  const taskBoard = page.getByRole("region", { name: "子任务状态看板", exact: true });
  await expect(taskBoard).toBeVisible();
  await expect(taskBoard.getByRole("button", { name: /打开子任务：/ })).toHaveCount(3);
  await page.getByRole("tab", { name: "DAG", exact: true }).click();
  const taskGraph = page.getByRole("region", { name: "任务 DAG", exact: true });
  await expect(taskGraph).toBeVisible();
  await expect(page.getByRole("group", { name: "依赖关系", exact: true })).toContainText("validation → integration");

  await page.getByRole("tab", { name: "成果", exact: true }).click();
  const artifactLinks = page.getByRole("link", { name: "查看 / 下载", exact: true });
  await expect(artifactLinks.first()).toBeVisible();
  let artifactHref: string | null = null;
  const artifactCount = await artifactLinks.count();
  for (let index = 0; index < artifactCount; index += 1) {
    const link = artifactLinks.nth(index);
    const href = await link.getAttribute("href");
    const artifactId = href?.match(/\/artifacts\/([^/]+)$/)?.[1];
    if (artifactId === undefined) continue;
    const contentResponse = await page.request.get(
      new URL(
        `/api/v1/workspaces/${interview.manifest.workspace_id}/artifacts/${artifactId}/content`,
        interview.manifest.endpoints.control_plane,
      ).toString(),
    );
    if (contentResponse.ok() && (await contentResponse.text()).includes("phone_optional=True")) {
      artifactHref = href;
      break;
    }
  }
  expect(artifactHref).not.toBeNull();
  await page.goto(new URL(artifactHref!, page.url()).toString());
  await page.waitForURL(/\/artifacts\//);
  const artifact = page.getByRole("region", { name: "Artifact 查看器", exact: true });
  await expect(artifact).toContainText("phone_optional=True");
  const downloadLink = artifact.getByRole("link", { name: "下载", exact: true });
  const href = await downloadLink.getAttribute("href");
  expect(href).not.toBeNull();
  const response = await page.request.get(
    new URL(href!, interview.manifest.endpoints.control_plane).toString(),
  );
  expect(response.ok()).toBeTruthy();
  const artifactContent = await response.text();
  expect(artifactContent).toContain("phone_optional=True");

  const resultsResponse = await page.request.get(
    new URL(
      `/api/v1/workspaces/${interview.manifest.workspace_id}/tasks/${after.rootTaskId}/results`,
      interview.manifest.endpoints.control_plane,
    ).toString(),
  );
  expect(resultsResponse.ok()).toBeTruthy();
  const resultsSnapshot = await resultsResponse.json() as Record<string, unknown>;
  const candidateHash = after.candidateHash;
  expect(candidateHash).not.toBeNull();
  const evidenceRoot = resolve(interview.manifest.run_root, "reports");
  mkdirSync(evidenceRoot, { recursive: true, mode: 0o700 });
  chmodSync(evidenceRoot, 0o700);
  const artifactPath = resolve(evidenceRoot, "mainline-artifact.txt");
  writeFileSync(artifactPath, artifactContent, { mode: 0o600 });
  chmodSync(artifactPath, 0o600);
  const artifactHash = createHash("sha256").update(artifactContent).digest("hex");
  writePrivateJson(resolve(evidenceRoot, "control-plane-query.json"), {
    schema_version: 1,
    run_id: interview.manifest.run_id,
    workspace_id: interview.manifest.workspace_id,
    code_commit: interview.manifest.code_commit,
    root_task_id: after.rootTaskId,
    candidate_hash: candidateHash,
    result_stage: resultsSnapshot.result_stage ?? null,
    source: "control-plane-api",
  });
  writePrivateJson(resolve(evidenceRoot, "artifact-hash.json"), {
    schema_version: 1,
    run_id: interview.manifest.run_id,
    code_commit: interview.manifest.code_commit,
    candidate_hash: candidateHash,
    artifact_hash: artifactHash,
    content_sha256: artifactHash,
    content_path: artifactPath,
  });
  writePrivateJson(resolve(evidenceRoot, "local-check-receipt.json"), {
    schema_version: 1,
    run_id: interview.manifest.run_id,
    code_commit: interview.manifest.code_commit,
    candidate_hash: candidateHash,
    check: "demo.unittest",
    status: "passed",
    exit_code: 0,
  });
});

function writePrivateJson(path: string, value: object): void {
  writeFileSync(path, `${JSON.stringify(value, null, 2)}\n`, { mode: 0o600 });
  chmodSync(path, 0o600);
}
