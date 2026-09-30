import { randomUUID } from "node:crypto";

import { expect, test } from "./fixtures";

test.use({ demoScenario: "knowledge" });

test("F14 knowledge publication requires review and preserves its artifact source", async ({
  interview,
  page,
}) => {
  for (const actionId of ["F14-01", "F14-02", "F14-05", "F14-07"]) {
    test.info().annotations.push({ type: "action", description: actionId });
  }

  await interview.openChat();
  const chatUrl = page.url();
  await interview.sendRequirement(
    "为项目增加 CSV 批量导入功能，手机号为空的策略在执行时向用户确认",
  );
  await interview.openProposal();
  await interview.confirmPlan();
  await expect.poll(async () => (await interview.getFacts()).parallelObserved).toBe(true);
  await interview.releaseGate("parallel");
  await interview.openAttention("validate");
  await interview.sendInput("手机号允许为空，有值时才校验格式");
  await interview.releaseGate("before-artifact");

  await expect.poll(async () => {
    const response = await page.request.get(new URL(
      `/knowledge-api/api/v1/workspaces/${interview.manifest.workspace_id}/knowledge/candidates?limit=100`,
      interview.manifest.endpoints.web,
    ).toString());
    if (!response.ok()) return false;
    const payload = await response.json() as { items?: Array<{ proposed_title?: string }> };
    return payload.items?.some((item) => item.proposed_title === "样例项目：手机号允许为空") ?? false;
  }, { timeout: 45_000 }).toBe(true);

  await page.goto(new URL(
    `/workspaces/${interview.manifest.workspace_id}/knowledge/inbox`,
    interview.manifest.endpoints.web,
  ).toString());
  const candidateLink = page.getByRole("link", {
    name: "打开候选 样例项目：手机号允许为空",
    exact: true,
  });
  await expect(candidateLink).toBeVisible({ timeout: 30_000 });
  await candidateLink.click();
  await expect(page.getByRole("heading", { name: "样例项目：手机号允许为空", exact: true })).toBeVisible();
  await expect(page.getByText("样例项目校验产物", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "接受候选", exact: true }).click();
  await page.getByLabel("审核原因", { exact: true }).fill("仅用于本演示项目的导入规则");
  await page.getByRole("button", { name: "提交接受", exact: true }).click();
  await page.waitForURL(/\/knowledge\/pages\/[0-9a-f-]{36}/i);

  const pageId = page.url().match(/\/knowledge\/pages\/([^/?]+)/)?.[1];
  expect(pageId).toBeDefined();
  const pageResponse = await page.request.get(new URL(
    `/knowledge-api/api/v1/workspaces/${interview.manifest.workspace_id}/knowledge/pages/${pageId}`,
    interview.manifest.endpoints.web,
  ).toString());
  expect(pageResponse.ok()).toBeTruthy();
  const pagePayload = await pageResponse.json() as {
    summary?: { canonical_title?: string };
    current_revision?: {
      revision_id?: string;
      content_hash?: string;
      sections?: Array<{
        section_key?: string;
        section_hash?: string;
        body_markdown?: string;
      }>;
      sources?: Array<{ source_id?: string; metadata?: Record<string, unknown> }>;
    } | null;
  };
  expect(pagePayload.summary?.canonical_title).toBe("样例项目：手机号允许为空");
  expect(pagePayload.current_revision?.revision_id).toBeDefined();
  expect(pagePayload.current_revision?.content_hash).toMatch(/^[0-9a-f]{64}$/);
  expect(pagePayload.current_revision?.sections?.some((section) =>
    section.section_key === "phone-rule"
    && section.body_markdown?.includes("手机号字段允许为空"),
  )).toBe(true);
  expect(pagePayload.current_revision?.sources?.some((source) =>
    source.metadata?.artifact_id !== undefined,
  )).toBe(true);

  const revision = pagePayload.current_revision;
  const section = revision?.sections?.find((item) => item.section_key === "phone-rule");
  const revisionId = revision?.revision_id;
  const revisionHash = revision?.content_hash;
  const sectionHash = section?.section_hash;
  const sourceIds = revision?.sources
    ?.map((source) => source.source_id)
    .filter((sourceId): sourceId is string => sourceId !== undefined)
    .sort();
  const rootTaskId = (await interview.getFacts()).rootTaskId;
  if (
    pageId === undefined
    || revisionId === undefined
    || revisionHash === undefined
    || section?.section_key === undefined
    || sectionHash === undefined
    || rootTaskId === null
  ) {
    throw new Error("accepted knowledge revision is incomplete");
  }
  expect(sourceIds).toBeDefined();

  const controlPlane = interview.manifest.endpoints.control_plane;
  const workContextUrl = new URL(
    `/api/v1/workspaces/${interview.manifest.workspace_id}/tasks/${rootTaskId}/work-context`,
    controlPlane,
  ).toString();
  const workContextResponse = await page.request.get(workContextUrl);
  expect(workContextResponse.ok(), await workContextResponse.text()).toBeTruthy();
  const workContext = await workContextResponse.json() as {
    id: string;
    version: number;
    current_revision_number: number;
  };
  const attachResponse = await page.request.post(new URL(
    "/api/v1/commands",
    controlPlane,
  ).toString(), {
    data: {
      command_id: randomUUID(),
      workspace_id: interview.manifest.workspace_id,
      command_type: "AttachKnowledgeRevision",
      target_type: "work_context",
      target_id: workContext.id,
      actor: { type: "local", id: "aitejiu" },
      expected_version: workContext.version,
      idempotency_key: `interview-attach-knowledge:${pageId}:${revisionId}`,
      correlation_id: randomUUID(),
      payload: {
        knowledge_ref: {
          page_id: pageId,
          revision_id: revisionId,
          revision_hash: revisionHash,
          section_key: section.section_key,
          section_hash: sectionHash,
          source_ids: sourceIds ?? [],
          ordinal: 0,
        },
        selection_reason: "后续规划继续使用已审核的手机号规则",
      },
    },
  });
  expect(attachResponse.ok(), await attachResponse.text()).toBeTruthy();

  await expect.poll(async () => {
    const response = await page.request.get(workContextUrl);
    if (!response.ok()) return false;
    const value = await response.json() as {
      current_revision_number?: number;
      current_revision?: { snapshot?: { knowledge_refs?: Array<Record<string, unknown>> } };
    };
    const refs = value.current_revision?.snapshot?.knowledge_refs ?? [];
    return value.current_revision_number === workContext.current_revision_number + 1
      && refs.some((ref) =>
        ref.page_id === pageId
        && ref.revision_id === revisionId
        && ref.revision_hash === revisionHash
        && ref.section_key === section.section_key
        && ref.section_hash === sectionHash
        && JSON.stringify(ref.source_ids) === JSON.stringify(sourceIds ?? [])
      );
  }, { timeout: 30_000 }).toBe(true);

  await page.goto(chatUrl);
  await interview.sendRequirement("还需要支持 TSV 文件，并沿用已审核的手机号规则");
  await interview.openLatestRevisionProposal();

  const revisionRequestId = page.url().match(/\/revisions\/([^/?]+)$/)?.[1];
  expect(revisionRequestId).toBeDefined();
  const runsResponse = await page.request.get(new URL(
    `/api/v1/workspaces/${interview.manifest.workspace_id}/planning-requests/${revisionRequestId}/runs`,
    controlPlane,
  ).toString());
  expect(runsResponse.ok(), await runsResponse.text()).toBeTruthy();
  const runsPayload = await runsResponse.json() as {
    items?: Array<{ id?: string; status?: string }>;
  };
  const planningRunId = runsPayload.items?.at(-1)?.id;
  expect(planningRunId).toBeDefined();

  const packageUrl = new URL(
    `/api/v1/workspaces/${interview.manifest.workspace_id}/planning-runs/${planningRunId}/context-package`,
    controlPlane,
  ).toString();
  let plannerPackage: {
    work_context_revision_number?: number;
    knowledge_refs?: Array<Record<string, unknown>>;
    assembly?: { status?: string };
    acknowledgements?: Array<Record<string, unknown>>;
    package_hash?: string;
  } | null = null;
  let latestWorkContextRevisionNumber: number | null = null;
  await expect.poll(async () => {
    const response = await page.request.get(packageUrl);
    if (!response.ok()) return false;
    const value = await response.json() as typeof plannerPackage;
    const currentContextResponse = await page.request.get(workContextUrl);
    if (!currentContextResponse.ok()) return false;
    const currentContext = await currentContextResponse.json() as {
      current_revision_number?: number;
    };
    latestWorkContextRevisionNumber = currentContext.current_revision_number ?? null;
    const refs = value?.knowledge_refs ?? [];
    const hasKnowledgeRef = refs.some((ref) =>
      ref.page_id === pageId
      && ref.revision_id === revisionId
      && ref.revision_hash === revisionHash
      && ref.section_key === section.section_key
      && ref.section_hash === sectionHash
      && JSON.stringify(ref.source_ids) === JSON.stringify(sourceIds ?? [])
    );
    const hasPlannerAck = (value?.acknowledgements ?? []).some((ack) =>
      ack.consumer_type === "planner"
      && ack.consumer_id === planningRunId
      && ack.package_hash === value?.package_hash
    );
    if (
      value?.assembly?.status === "ready"
      && hasKnowledgeRef
      && hasPlannerAck
      && latestWorkContextRevisionNumber !== null
      && latestWorkContextRevisionNumber >= workContext.current_revision_number + 1
      && value.work_context_revision_number === latestWorkContextRevisionNumber
    ) {
      plannerPackage = value;
      return true;
    }
    return false;
  }, { timeout: 45_000 }).toBe(true);
  expect(latestWorkContextRevisionNumber).toBeGreaterThanOrEqual(workContext.current_revision_number + 1);
  expect(plannerPackage?.work_context_revision_number).toBe(latestWorkContextRevisionNumber);
  expect(plannerPackage?.knowledge_refs).toHaveLength(1);
  expect(plannerPackage?.acknowledgements?.some((ack) =>
    ack.consumer_type === "planner"
    && ack.consumer_id === planningRunId
    && ack.package_hash === plannerPackage?.package_hash,
  )).toBe(true);
  await page.goto(new URL(
    `/workspaces/${interview.manifest.workspace_id}/knowledge/pages/${pageId}?revision_id=${revisionId}`,
    interview.manifest.endpoints.web,
  ).toString());
  await expect(page.getByText("手机号校验规则", { exact: true })).toBeVisible();
});
