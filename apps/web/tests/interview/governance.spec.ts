import { expect, test } from "./fixtures";

test.use({ demoScenario: "governance" });

test("F13 governance keeps release control local to the WorkerItem", async ({
  interview,
  page,
}) => {
  test.info().annotations.push({ type: "action", description: "F13-02" });
  test.info().annotations.push({ type: "action", description: "F13-03" });
  test.info().annotations.push({ type: "action", description: "F13-04" });
  test.info().annotations.push({ type: "action", description: "F13-01" });

  await interview.openChat();
  await interview.sendRequirement(
    "为项目增加 CSV 批量导入功能，手机号为空的策略在执行时向用户确认",
  );
  await interview.openProposal();
  await interview.confirmPlan();
  await expect.poll(async () => Object.keys((await interview.getFacts()).workerItems).length).toBeGreaterThan(0);
  const before = await interview.getFacts();

  await interview.openWorker("report");
  const reportWorkerItemId = (await interview.getFacts()).workerItems.report?.id;
  expect(reportWorkerItemId).toBeDefined();
  const resultInputsResponse = await page.request.get(new URL(
    `/api/v1/workspaces/${interview.manifest.workspace_id}/worker-items/${reportWorkerItemId}/result-inputs`,
    interview.manifest.endpoints.control_plane,
  ).toString());
  expect(resultInputsResponse.ok()).toBeTruthy();
  const resultInputs = await resultInputsResponse.json() as Record<string, unknown>;
  expect(resultInputs.groups).toEqual(expect.any(Array));
  expect(resultInputs.governance).toEqual(expect.any(Object));
  expect(resultInputs.delivery).toEqual(expect.any(Object));
  const governance = page.getByRole("region", { name: "协同放行控制", exact: true });
  await expect(governance).toBeVisible();
  await governance.getByRole("button", { name: "暂停自动放行", exact: true }).click();
  const pause = page.getByRole("dialog", { name: "暂停自动放行", exact: true });
  await pause.getByLabel("暂停原因", { exact: true }).fill("先检查报告输入是否完整");
  await pause.getByRole("button", { name: "确认暂停自动放行", exact: true }).click();
  await expect(governance).toContainText("已暂停");

  const paused = await interview.getFacts();
  expect(paused.planGeneration).toBe(before.planGeneration);
  expect(paused.workerItems.report?.id).toBe(before.workerItems.report?.id);

  await interview.releaseGate("parallel");
  await expect.poll(async () => {
    const response = await page.request.get(new URL(
      `/api/v1/workspaces/${interview.manifest.workspace_id}/worker-items/${paused.workerItems.report.id}/workspace`,
      interview.manifest.endpoints.control_plane,
    ).toString());
    if (!response.ok()) return "unavailable";
    const payload = await response.json() as { collaboration?: { active_proposal?: unknown } };
    return payload.collaboration?.active_proposal === null || payload.collaboration?.active_proposal === undefined
      ? "waiting"
      : "proposal";
  }, { timeout: 30_000 }).toBe("proposal");

  await page.reload();
  await expect(governance.getByRole("button", { name: "批准 Proposal", exact: true })).toBeEnabled();
  await governance.getByRole("button", { name: "批准 Proposal", exact: true }).click();
  const approve = page.getByRole("dialog", { name: "批准协同放行", exact: true });
  await approve.getByRole("button", { name: "批准这次放行", exact: true }).click();
  await expect(governance).toContainText("已放行");

  await expect.poll(async () => {
    const response = await page.request.get(new URL(
      `/api/v1/workspaces/${interview.manifest.workspace_id}/worker-items/${paused.workerItems.report.id}/workspace`,
      interview.manifest.endpoints.control_plane,
    ).toString());
    if (!response.ok()) return "unavailable";
    const payload = await response.json() as { collaboration?: { governance_mode?: string } };
    return payload.collaboration?.governance_mode ?? "unknown";
  }).toBe("paused");

  await governance.getByRole("button", { name: "恢复自动放行", exact: true }).click();
  const resume = page.getByRole("dialog", { name: "恢复自动放行", exact: true });
  await resume.getByRole("button", { name: "确认恢复自动放行", exact: true }).click();
  await expect(governance).toContainText("自动放行");
  expect((await interview.getFacts()).planGeneration).toBe(before.planGeneration);
});

test("F13 rejection records a local decision without releasing downstream work", async ({
  interview,
  page,
}) => {
  test.info().annotations.push({ type: "action", description: "F13-06" });

  await interview.openChat();
  await interview.sendRequirement(
    "为项目增加 CSV 批量导入功能，手机号为空的策略在执行时向用户确认",
  );
  await interview.openProposal();
  await interview.confirmPlan();
  await expect.poll(async () => Object.keys((await interview.getFacts()).workerItems).length).toBeGreaterThan(0);
  await interview.releaseGate("parallel");

  await interview.openWorker("report");
  const before = await interview.getFacts();
  const reportWorkerItemId = before.workerItems.report?.id;
  expect(reportWorkerItemId).toBeDefined();
  const governance = page.getByRole("region", { name: "协同放行控制", exact: true });
  await governance.getByRole("button", { name: "暂停自动放行", exact: true }).click();
  const pause = page.getByRole("dialog", { name: "暂停自动放行", exact: true });
  await pause.getByLabel("暂停原因", { exact: true }).fill("先保留 Proposal，演示驳回边界");
  await pause.getByRole("button", { name: "确认暂停自动放行", exact: true }).click();
  await expect(governance).toContainText("已暂停");
  await expect.poll(async () => {
    const response = await page.request.get(new URL(
      `/api/v1/workspaces/${interview.manifest.workspace_id}/worker-items/${reportWorkerItemId}/workspace`,
      interview.manifest.endpoints.control_plane,
    ).toString());
    if (!response.ok()) return "unavailable";
    const payload = await response.json() as { collaboration?: { active_proposal?: unknown } };
    return payload.collaboration?.active_proposal == null ? "waiting" : "proposal";
  }, { timeout: 30_000 }).toBe("proposal");

  await page.reload();
  await expect(governance.getByRole("button", { name: "驳回 Proposal", exact: true })).toBeEnabled();
  await governance.getByRole("button", { name: "驳回 Proposal", exact: true }).click();
  const reject = page.getByRole("dialog", { name: "驳回协同放行", exact: true });
  await expect(reject).toBeVisible();
  await reject.getByLabel("驳回原因", { exact: true }).fill("结果输入尚未确认，暂不向下游交付");
  await reject.getByRole("button", { name: "驳回这次放行", exact: true }).click();

  const workspacePath = new URL(
    `/api/v1/workspaces/${interview.manifest.workspace_id}/worker-items/${reportWorkerItemId}/workspace`,
    interview.manifest.endpoints.control_plane,
  ).toString();
  await expect.poll(async () => {
    const response = await page.request.get(workspacePath);
    if (!response.ok()) return false;
    const payload = await response.json() as Record<string, unknown>;
    const collaboration = payload.collaboration as Record<string, unknown> | undefined;
    const history = collaboration?.governance_history as Array<Record<string, unknown>> | undefined;
    if (history === undefined) return false;
    return history.some((item) => (
      item.kind === "resolution"
      && item.status === "applied"
      && item.resolution_action === "reject"
    ) || (
      item.kind === "proposal"
      && item.status === "rejected"
    ));
  }, { timeout: 15_000 }).toBe(true);

  const workspaceResponse = await page.request.get(workspacePath);
  expect(workspaceResponse.ok()).toBeTruthy();
  const workspace = await workspaceResponse.json() as Record<string, unknown>;
  const collaboration = workspace.collaboration as Record<string, unknown>;
  const history = collaboration.governance_history as Array<Record<string, unknown>>;
  expect(history.some((item) => (
    item.kind === "resolution"
    && item.status === "applied"
    && item.resolution_action === "reject"
  ) || (
    item.kind === "proposal"
    && item.status === "rejected"
  ))).toBeTruthy();
  const after = await interview.getFacts();
  expect(after.workerItems.report?.executionId).toBe(before.workerItems.report?.executionId ?? null);
});
