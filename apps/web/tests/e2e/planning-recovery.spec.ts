import { randomUUID } from "node:crypto";
import type { Page } from "@playwright/test";

import {
  expect,
  R5_WORKSPACE_ID,
  runPlanningWorkerOnce,
  test,
} from "./fixtures";

test.describe("R5.1 Planning recovery and concurrency", () => {
  test("answers a blocking clarification and resumes the same request", async ({ page, seedR5 }) => {
    seedR5("clarification");
    const planningUrl = await submitBrief(page, "确认规划器使用哪一种代码适配器");

    expect(runPlanningWorkerOnce().clarifications).toBe(1);
    await page.reload();
    await expect(page.locator(".planning-status").filter({ hasText: "等待你的回答" })).toBeVisible();
    await expect(page.getByText("Should the fake planner use the standard code adapter?", { exact: true })).toBeVisible();

    await page.getByRole("textbox", { name: "澄清答复" }).fill("fake");
    await page.getByRole("button", { name: "提交澄清答复" }).click();
    expect(runPlanningWorkerOnce().succeeded).toBe(1);
    await page.goto(planningUrl);
    await expect(page.locator(".planning-status").filter({ hasText: "方案待确认" })).toBeVisible();
    await expect(page.getByText(/Proposal V1/)).toBeVisible();
  });

  test("rejects a stale revision command and recovers with Proposal V2", async ({ page, request, seedR5 }) => {
    seedR5("proposal");
    const planningUrl = await submitBrief(page, "验证方案修订的并发控制");
    expect(runPlanningWorkerOnce().succeeded).toBe(1);
    await page.goto(planningUrl);
    await expect(page.locator(".planning-status").filter({ hasText: "方案待确认" })).toBeVisible();

    const requestId = planningRequestId(planningUrl);
    const requestDetailResponse = await request.get(
      `/api/v1/workspaces/${R5_WORKSPACE_ID}/planning-requests/${requestId}`,
    );
    const proposalResponse = await request.get(
      `/api/v1/workspaces/${R5_WORKSPACE_ID}/planning-requests/${requestId}/proposals/1`,
    );
    expect(requestDetailResponse.ok()).toBeTruthy();
    expect(proposalResponse.ok()).toBeTruthy();
    const requestDetail = await requestDetailResponse.json() as { version: number };
    const proposal = await proposalResponse.json() as { content_hash: string };
    const firstCommand = revisionCommand(requestId, requestDetail.version, proposal.content_hash, "保留现有 Worker 上下文");
    const firstResponse = await request.post("/api/v1/commands", { data: firstCommand });
    expect(firstResponse.ok()).toBeTruthy();

    const staleResponse = await request.post("/api/v1/commands", {
      data: revisionCommand(requestId, requestDetail.version, proposal.content_hash, "这份版本已经过期"),
    });
    expect(staleResponse.status()).toBe(409);

    expect(runPlanningWorkerOnce().succeeded).toBe(1);
    await page.goto(planningUrl);
    await expect(page.locator(".planning-status").filter({ hasText: "方案待确认" })).toBeVisible();
    await expect(page.getByText(/Proposal V2 · Brief V2/)).toBeVisible();
    await expect(page.getByText("Brief R2", { exact: true })).toBeVisible();
  });

  test("cancels a queued planning request into a read-only terminal state", async ({ page, seedR5 }) => {
    seedR5("proposal");
    await submitBrief(page, "验证取消规划");
    await page.getByRole("button", { name: "取消规划" }).click();
    const dialog = page.getByRole("dialog", { name: "确认取消规划" });
    await expect(dialog).toBeVisible();
    await dialog.getByRole("button", { name: "确认取消" }).click();
    await expect(page.locator(".planning-status").filter({ hasText: "已取消" })).toBeVisible();
    await expect(page.getByText("只读历史", { exact: true })).toBeVisible();
  });

  test("rejects a generated proposal without creating a task", async ({ page, request, seedR5 }) => {
    seedR5("proposal");
    const planningUrl = await submitBrief(page, "验证拒绝方案不会物化任务");
    expect(runPlanningWorkerOnce().succeeded).toBe(1);
    await page.goto(planningUrl);
    await page.getByRole("button", { name: "拒绝方案" }).click();
    const dialog = page.getByRole("dialog", { name: "确认拒绝方案" });
    await dialog.getByRole("button", { name: "确认拒绝" }).click();
    await expect(page.getByText("已拒绝", { exact: true })).toBeVisible();
    const tasksResponse = await request.get(`/api/v1/workspaces/${R5_WORKSPACE_ID}/task-overviews`);
    expect(await tasksResponse.json()).toEqual([]);
  });

  test("returns to the task index when the new-task tab is closed", async ({ page, seedR5 }) => {
    seedR5("proposal");
    await page.goto(`/workspaces/${R5_WORKSPACE_ID}/tasks`);
    await page.getByRole("region", { name: "任务" }).getByRole("link", { name: "新建任务" }).click();
    await expect(page.getByRole("heading", { name: "新建任务", exact: true })).toBeVisible();
    await page.getByRole("button", { name: "关闭新建任务" }).click();
    await expect(page).toHaveURL(`/workspaces/${R5_WORKSPACE_ID}/tasks`);
    await expect(page.getByRole("heading", { name: "任务", exact: true })).toBeVisible();
  });
});

async function submitBrief(page: Page, objective: string): Promise<string> {
  await page.goto(`/workspaces/${R5_WORKSPACE_ID}/tasks`);
  await page.getByRole("region", { name: "任务" }).getByRole("link", { name: "新建任务" }).click();
  await page.getByLabel("需求目标").fill(objective);
  await page.getByRole("textbox", { name: "验收条件" }).first().fill("状态和历史可恢复");
  await page.getByRole("button", { name: "提交需求" }).click();
  await expect(page).toHaveURL(new RegExp(`/workspaces/${R5_WORKSPACE_ID}/planning/[0-9a-f-]{36}$`));
  return page.url();
}

function planningRequestId(url: string): string {
  const value = url.split("/").at(-1);
  if (value === undefined || value.length === 0) throw new Error("planning request id is missing");
  return value;
}

function revisionCommand(
  requestId: string,
  expectedVersion: number,
  contentHash: string,
  feedback: string,
) {
  return {
    command_id: randomUUID(),
    workspace_id: R5_WORKSPACE_ID,
    command_type: "RequestProposalRevision",
    target_type: "planning_request",
    actor: { type: "local", id: "aitejiu" },
    target_id: requestId,
    expected_version: expectedVersion,
    idempotency_key: `e2e-revision-${randomUUID()}`,
    correlation_id: randomUUID(),
    payload: {
      current_proposal_revision: 1,
      current_proposal_content_hash: contentHash,
      feedback,
    },
  };
}
