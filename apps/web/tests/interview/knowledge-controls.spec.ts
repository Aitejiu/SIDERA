import { randomUUID } from "node:crypto";

import type { Page } from "@playwright/test";

import { expect, test } from "./fixtures";

test.use({ demoScenario: "controls" });

test("F14 knowledge review controls keep publication and candidate versions explicit", async ({
  interview,
  page,
}) => {
  for (const actionId of ["F14-03", "F14-04", "F14-06", "F14-08"]) {
    test.info().annotations.push({ type: "action", description: actionId });
  }

  const changesCandidate = await submitCandidate(page, interview, "请求修改");
  await openCandidate(page, interview, changesCandidate);
  await page.getByRole("button", { name: "请求修改", exact: true }).click();
  await page.getByLabel("审核原因", { exact: true }).fill("补充手机号为空时的处理边界");
  await page.getByRole("button", { name: "提交请求修改", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("审核命令已提交");
  await expect.poll(async () => (await getCandidate(page, interview, changesCandidate)).summary.status)
    .toBe("changes_requested");

  const rejectedCandidate = await submitCandidate(page, interview, "拒绝");
  await openCandidate(page, interview, rejectedCandidate);
  page.once("dialog", (dialog) => void dialog.accept());
  await page.getByRole("button", { name: "拒绝候选", exact: true }).click();
  await page.getByLabel("审核原因", { exact: true }).fill("来源不足，不发布到工作区知识");
  await page.getByRole("button", { name: "提交拒绝", exact: true }).click();
  await expect.poll(async () => (await getCandidate(page, interview, rejectedCandidate)).summary.status)
    .toBe("rejected");
  await expect(page.getByText("已拒绝", { exact: true })).toBeVisible();

  const withdrawnCandidate = await submitCandidate(page, interview, "撤回");
  await openCandidate(page, interview, withdrawnCandidate);
  page.once("dialog", (dialog) => void dialog.accept());
  await page.getByRole("button", { name: "撤回候选", exact: true }).click();
  await expect.poll(async () => (await getCandidate(page, interview, withdrawnCandidate)).summary.status)
    .toBe("withdrawn");
  await expect(page.getByText("已撤回", { exact: true })).toBeVisible();

  const replayCandidate = await submitCandidate(page, interview, "重复审核");
  const initial = await getCandidate(page, interview, replayCandidate);
  const review = reviewCommand(interview.manifest.workspace_id, initial, "request_changes");
  const first = await postKnowledgeCommand(page, interview, review);
  expect(first.status()).toBe(200);
  const replay = await postKnowledgeCommand(page, interview, review);
  expect(replay.status()).toBe(200);
  expect(await replay.json()).toEqual(await first.json());

  const stale = reviewCommand(interview.manifest.workspace_id, initial, "reject");
  const staleResponse = await postKnowledgeCommand(page, interview, stale);
  expect(staleResponse.status()).toBe(409);
  const staleBody = await staleResponse.json() as { error?: { code?: string } };
  expect(staleBody.error?.code).toBe("expected_version_conflict");
  expect((await getCandidate(page, interview, replayCandidate)).summary.status).toBe("changes_requested");
});

type CandidateDetail = {
  summary: {
    candidate_id: string;
    candidate_hash: string;
    expected_revision_id: string | null;
    expected_revision_hash: string | null;
    version: number;
  };
};

async function submitCandidate(
  page: Page,
  interview: { manifest: { workspace_id: string; endpoints: { web: string } } },
  label: string,
): Promise<string> {
  const candidateId = randomUUID();
  const commandId = randomUUID();
  const response = await page.request.post(new URL(
    "/knowledge-api/api/v1/commands",
    interview.manifest.endpoints.web,
  ).toString(), {
    data: {
      command_id: commandId,
      workspace_id: interview.manifest.workspace_id,
      command_type: "SubmitKnowledgeCandidate",
      target_type: "knowledge_candidate",
      actor: { type: "local_owner", id: "aitejiu" },
      target_id: candidateId,
      expected_version: null,
      idempotency_key: `interview-knowledge-${candidateId}`,
      correlation_id: randomUUID(),
      payload: {
        candidate_schema_version: 1,
        candidate_id: candidateId,
        operation: "create_page",
        title: `演示控制候选 · ${label}`,
        slug: `interview-control-${candidateId}`,
        summary: "独立 controls 候选",
        tags: ["interview", "controls"],
        sections: [{
          section_key: "phone-rule",
          heading: "手机号规则",
          summary: "候选审核用例",
          body_markdown: "手机号字段允许为空，有值时才校验格式。",
          ordinal: 0,
        }],
        relations: [],
        sources: [{ source_type: "manual", label: "controls 手工候选", metadata: { label } }],
        provenance: { producer_type: "local_owner", producer_id: "interview-controls" },
      },
    },
  });
  expect(response.ok(), await response.text()).toBeTruthy();
  return candidateId;
}

async function openCandidate(
  page: import("@playwright/test").Page,
  interview: { manifest: { workspace_id: string; endpoints: { web: string } } },
  candidateId: string,
): Promise<void> {
  await page.goto(new URL(
    `/workspaces/${interview.manifest.workspace_id}/knowledge/candidates/${candidateId}`,
    interview.manifest.endpoints.web,
  ).toString());
  await expect(page.getByRole("heading", { name: /演示控制候选/, exact: false })).toBeVisible();
}

async function getCandidate(
  page: import("@playwright/test").Page,
  interview: { manifest: { workspace_id: string; endpoints: { web: string } } },
  candidateId: string,
): Promise<CandidateDetail> {
  const response = await page.request.get(new URL(
    `/knowledge-api/api/v1/workspaces/${interview.manifest.workspace_id}/knowledge/candidates/${candidateId}`,
    interview.manifest.endpoints.web,
  ).toString());
  expect(response.ok(), await response.text()).toBeTruthy();
  return await response.json() as CandidateDetail;
}

function reviewCommand(workspaceId: string, detail: CandidateDetail, decision: "reject" | "request_changes") {
  const commandId = randomUUID();
  return {
    command_id: commandId,
    workspace_id: workspaceId,
    command_type: "ReviewKnowledgeCandidate",
    target_type: "knowledge_candidate",
    actor: { type: "local_owner", id: "aitejiu" },
    target_id: detail.summary.candidate_id,
    expected_version: null,
    idempotency_key: `interview-review-${commandId}`,
    correlation_id: randomUUID(),
    payload: {
      candidate_id: detail.summary.candidate_id,
      decision,
      reason: "controls 版本回归",
      observed_candidate_hash: detail.summary.candidate_hash,
      expected_candidate_version: detail.summary.version,
      observed_target_revision_id: detail.summary.expected_revision_id,
      observed_target_revision_hash: detail.summary.expected_revision_hash,
    },
  };
}

async function postKnowledgeCommand(
  page: import("@playwright/test").Page,
  interview: { manifest: { endpoints: { web: string } } },
  body: Record<string, unknown>,
) {
  return page.request.post(new URL(
    "/knowledge-api/api/v1/commands",
    interview.manifest.endpoints.web,
  ).toString(), { data: body });
}
