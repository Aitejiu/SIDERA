import { readdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

import type { Page } from "@playwright/test";

import type { InterviewDriver } from "./driver";
import { expect, test } from "./fixtures";

type JsonRecord = Record<string, unknown>;
type ThreadDetail = JsonRecord & {
  thread: JsonRecord;
  deliveries: JsonRecord[];
  facts: JsonRecord;
};

type DeliveryClaim = {
  delivery_id: string;
  claim_token: string;
  fencing_token: number;
};

test.use({ demoScenario: "controls" });

test.describe("recovered notification history", () => {
test.use({ demoScenario: "recovery" });

test("F15 archives a recovered notification thread without deleting its history", async ({
  interview,
  page,
}) => {
  test.info().annotations.push({ type: "action", description: "F15-04" });

  await establishRecoveryBeforeArtifact(interview, page);
  await interview.openAttention("validate");
  const validation = (await interview.getFacts()).workerItems.validate;
  expect(validation?.id).toBeDefined();
  await interview.sendInput("手机号允许为空，有值时才校验格式");
  await interview.restart("gateway-worker");
  await expect.poll(async () => (await interview.getFacts()).workerItems.validate.inputIds)
    .toHaveLength(1);
  await interview.releaseGate("before-artifact");

  const resolved = await waitForThread(page, interview, validation?.id ?? "", (detail) =>
    detail.thread.business_status === "resolved"
      && detail.thread.organization_status === "active"
      && asArray(detail.facts?.items).some((fact) => asRecord(fact).transition === "recovered"),
  );
  await page.goto(new URL(
    `/workspaces/${interview.manifest.workspace_id}/inbox`,
    interview.manifest.endpoints.web,
  ).toString());
  await page.getByRole("tab", { name: /进度通报/ }).click();
  expect(resolved.thread.worker_item_title).toBeTruthy();
  const thread = page.locator("article.notification-thread").filter({
    hasText: resolved.thread.worker_item_title ?? "",
  });
  await expect(thread).toBeVisible({ timeout: 30_000 });
  await thread.getByRole("button").first().click();
  const history = thread.locator(".notification-thread-history");
  await expect(history.getByRole("button", { name: "归档线程", exact: true })).toBeVisible();
  await history.getByRole("button", { name: "归档线程", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "归档线程已提交" })).toBeVisible();

  await page.getByRole("tab", { name: "已归档", exact: true }).click();
  const archived = page.locator("article.notification-thread").filter({ hasText: resolved.thread.title });
  await expect(archived).toBeVisible({ timeout: 15_000 });
  const archivedDetail = await getJson(page, interview, `/api/v1/workspaces/${interview.manifest.workspace_id}/notification-threads/${resolved.thread.id}`);
  expect(archivedDetail.thread.organization_status).toBe("archived");
  expect(asArray(asRecord(archivedDetail.facts).items).length).toBeGreaterThan(1);
  expect(await getJson(page, interview, `/api/v1/tasks/${(await interview.getFacts()).rootTaskId}/graph?workspace_id=${interview.manifest.workspace_id}`)).toHaveProperty("nodes");
});
});

test("F15 requeues an exhausted delivery through the Web inbox", async ({ interview, page }) => {
  test.info().annotations.push({ type: "action", description: "F15-05" });

  await stopDemoComponent(interview, "demo-notification-sink");
  await establishControlsBeforeParallelRelease(interview, page);
  await interview.releaseGate("parallel");

  const validationId = (await interview.getFacts()).workerItems.validate?.id;
  expect(validationId).toBeDefined();
  const detail = await waitForThread(page, interview, validationId ?? "", (value) =>
    value.deliveries.some((delivery) => delivery.status === "pending"),
  );
  const pending = detail.deliveries.find((delivery) => delivery.status === "pending");
  expect(pending).toBeDefined();
  const previousDeliveryIds = new Set(detail.deliveries.map((delivery) => delivery.delivery_id));
  const claim = await claimDelivery(page, interview, pending?.delivery_id ?? "");
  expect(claim.delivery_id).toBe(pending?.delivery_id);
  const nacked = await page.request.post(new URL(
    `/api/v1/workspaces/${interview.manifest.workspace_id}/notification-deliveries/${claim.delivery_id}/nack`,
    interview.manifest.endpoints.control_plane,
  ).toString(), {
    data: {
      claim_token: claim.claim_token,
      fencing_token: claim.fencing_token,
      outcome: "terminal_failure",
      stable_error_code: "provider_unavailable",
    },
  });
  expect(nacked.ok(), await nacked.text()).toBeTruthy();
  expect((await nacked.json()).status).toBe("exhausted");

  await interview.restart("demo-notification-sink");
  await page.goto(new URL(
    `/workspaces/${interview.manifest.workspace_id}/inbox`,
    interview.manifest.endpoints.web,
  ).toString());
  const thread = page.locator("article.notification-thread").filter({ hasText: "校验" }).first();
  await expect(thread).toBeVisible({ timeout: 30_000 });
  await thread.getByRole("button").first().click();
  const history = thread.locator(".notification-thread-history");
  await expect(history).toContainText("已耗尽");
  await history.getByRole("button", { name: "查看投递诊断", exact: true }).click();
  await expect(history).toContainText("provider_unavailable");
  await history.getByRole("button", { name: "重新投递", exact: true }).click();
  await history.getByLabel("重新投递原因", { exact: true }).fill("本地模拟收件端已恢复");
  await history.getByRole("button", { name: "确认重新投递", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "重新投递已提交" })).toBeVisible({ timeout: 15_000 });

  const afterRequeue = await waitForThreadById(page, interview, detail.thread.id, (value) =>
    value.deliveries.some((delivery) =>
      !previousDeliveryIds.has(delivery.delivery_id) && delivery.status === "delivered"
    ),
  );
  const delivered = afterRequeue.deliveries.find((delivery) =>
    !previousDeliveryIds.has(delivery.delivery_id) && delivery.status === "delivered"
  );
  expect(delivered).toBeDefined();
  expect(delivered?.attempts).toBe(1);
  expect(delivered?.delivered_at).toBeTruthy();
  const targetMessages = readdirSync(resolve(interview.manifest.run_root, "notifications"))
    .filter((name) => name.startsWith("message-") && name.endsWith(".json"))
    .filter((name) => {
      const payload = JSON.parse(readFileSync(
        resolve(interview.manifest.run_root, "notifications", name),
        "utf8",
      )) as { delivery_id?: string };
      return payload.delivery_id === delivered?.delivery_id;
    });
  expect(targetMessages).toHaveLength(1);
});

test("F15 reclaims a lost notification ack without writing a second local message", async ({ interview, page }) => {
  test.info().annotations.push({ type: "action", description: "F15-06" });

  await establishControlsMainline(interview, page);
  const validationId = (await interview.getFacts()).workerItems.validate?.id;
  expect(validationId).toBeDefined();
  const detail = await waitForThread(page, interview, validationId ?? "", (value) =>
    value.deliveries.some((delivery) => delivery.status === "delivered" && delivery.attempts >= 2),
    30_000,
  );
  const delivered = detail.deliveries.find((delivery) => delivery.status === "delivered" && delivery.attempts >= 2);
  expect(delivered).toBeDefined();

  const notificationDir = resolve(interview.manifest.run_root, "notifications");
  const messages = readdirSync(notificationDir).filter((name) => name.startsWith("message-") && name.endsWith(".json"));
  expect(messages).toHaveLength(1);
  const record = JSON.parse(readFileSync(resolve(notificationDir, messages[0]!), "utf8")) as {
    delivery_id?: string;
    message_id?: string;
  };
  expect(record.delivery_id).toBe(delivered?.delivery_id);
  expect(record.message_id).toBe(`demo-message:${delivered?.delivery_id}`);
  const attempts = await getJson(page, interview, `/api/v1/workspaces/${interview.manifest.workspace_id}/notification-deliveries/${delivered?.delivery_id}/attempts`);
  expect(asArray(attempts.items).length).toBeGreaterThanOrEqual(2);
  expect(asArray(attempts.items).map((item) => asRecord(item).outcome)).toContain("unknown_result");
});

async function establishControlsMainline(interview: InterviewDriver, page: Page): Promise<void> {
  await establishControlsBeforeParallelRelease(interview, page);
  await interview.releaseGate("parallel");
}

async function establishRecoveryBeforeArtifact(interview: InterviewDriver, page: Page): Promise<void> {
  await interview.openChat();
  await interview.sendRequirement("为项目增加 CSV 批量导入功能，手机号为空的策略在执行时向用户确认");
  await interview.openProposal();
  await expect(page.getByRole("button", { name: "确认并创建任务", exact: true })).toBeVisible({ timeout: 30_000 });
  await interview.confirmPlan();
  await expect.poll(async () => (await interview.getFacts()).parallelObserved).toBe(true);
  await interview.releaseGate("parallel");
}

async function establishControlsBeforeParallelRelease(interview: InterviewDriver, page: Page): Promise<void> {
  await interview.openChat();
  await interview.sendRequirement("为项目增加 CSV 批量导入功能，手机号为空的策略在执行时向用户确认");
  await interview.openProposal();
  const clarification = page.getByRole("textbox", { name: "澄清答复", exact: true });
  const confirm = page.getByRole("button", { name: "确认并创建任务", exact: true });
  await expect.poll(
    async () => await clarification.isVisible().catch(() => false) || await confirm.isVisible().catch(() => false),
    { timeout: 30_000 },
  ).toBe(true);
  if (await clarification.isVisible().catch(() => false)) {
    await clarification.fill("optional");
    await page.getByRole("button", { name: "提交澄清答复", exact: true }).click();
  }
  await expect(confirm).toBeVisible({ timeout: 30_000 });
  await interview.confirmPlan();
  await expect.poll(async () => (await interview.getFacts()).parallelObserved).toBe(true);
}

async function waitForThread(
  page: Page,
  interview: InterviewDriver,
  workerItemId: string,
  predicate: (detail: ThreadDetail) => boolean,
  timeout = 45_000,
): Promise<ThreadDetail> {
  let latest: ThreadDetail = { thread: {}, deliveries: [], facts: {} };
  await expect.poll(async () => {
    const pagePayload = await getJson(page, interview, `/api/v1/workspaces/${interview.manifest.workspace_id}/notification-threads?category=action&limit=100`);
    const progressPayload = await getJson(page, interview, `/api/v1/workspaces/${interview.manifest.workspace_id}/notification-threads?category=progress&limit=100`);
    const items = [...asArray(pagePayload.items), ...asArray(progressPayload.items)].map(asRecord);
    const thread = items.find((item) => item.worker_item_id === workerItemId);
    if (thread === undefined) return false;
    latest = asThreadDetail(await getJson(page, interview, `/api/v1/workspaces/${interview.manifest.workspace_id}/notification-threads/${thread.id}`));
    return predicate(latest);
  }, { timeout }).toBe(true);
  return latest;
}

async function waitForThreadById(
  page: Page,
  interview: InterviewDriver,
  threadId: string,
  predicate: (detail: ThreadDetail) => boolean,
  timeout = 45_000,
): Promise<ThreadDetail> {
  let latest: ThreadDetail = { thread: {}, deliveries: [], facts: {} };
  await expect.poll(async () => {
    latest = asThreadDetail(await getJson(page, interview, `/api/v1/workspaces/${interview.manifest.workspace_id}/notification-threads/${threadId}`));
    return predicate(latest);
  }, { timeout }).toBe(true);
  return latest;
}

async function claimDelivery(
  page: Page,
  interview: InterviewDriver,
  targetDeliveryId: string,
): Promise<DeliveryClaim> {
  const response = await page.request.post(new URL(
    `/api/v1/workspaces/${interview.manifest.workspace_id}/notification-deliveries/claims`,
    interview.manifest.endpoints.control_plane,
  ).toString(), {
    data: {
      module_instance_id: interview.manifest.module_ids.notification,
      supported_contract_versions: [1],
      batch_size: 20,
      lease_seconds: 5,
    },
  });
  expect(response.ok(), await response.text()).toBeTruthy();
  const payload = await response.json() as { deliveries?: JsonRecord[] };
  const envelopes = (payload.deliveries ?? []).map(asRecord);
  const envelope = envelopes.find(
    (item) => String(item.delivery_id) === targetDeliveryId,
  );
  expect(envelope).toBeDefined();
  for (const other of envelopes) {
    if (other === envelope) continue;
    const released = await page.request.post(new URL(
      `/api/v1/workspaces/${interview.manifest.workspace_id}/notification-deliveries/${String(other.delivery_id)}/nack`,
      interview.manifest.endpoints.control_plane,
    ).toString(), {
      data: {
        claim_token: String(other.claim_token),
        fencing_token: Number(other.fencing_token),
        outcome: "retryable_failure",
        stable_error_code: "transport_unavailable",
        retry_after_seconds: 30,
      },
    });
    expect(released.ok(), await released.text()).toBeTruthy();
  }
  return {
    delivery_id: String(envelope?.delivery_id),
    claim_token: String(envelope?.claim_token),
    fencing_token: Number(envelope?.fencing_token),
  };
}

async function stopDemoComponent(interview: InterviewDriver, component: string): Promise<void> {
  const identityPath = resolve(interview.manifest.run_root, "processes", `${component}.json`);
  const identity = JSON.parse(readFileSync(identityPath, "utf8")) as { pid?: number };
  if (typeof identity.pid !== "number") throw new Error(`missing process identity ${component}`);
  try {
    process.kill(identity.pid, "SIGTERM");
  } catch (error) {
    if (!(error instanceof Error) || !error.message.includes("ESRCH")) throw error;
  }
  await expect.poll(() => {
    try {
      process.kill(identity.pid!, 0);
      return false;
    } catch {
      return true;
    }
  }, { timeout: 5_000 }).toBe(true);
}

async function getJson(page: Page, interview: InterviewDriver, path: string): Promise<JsonRecord> {
  const response = await page.request.get(new URL(path, interview.manifest.endpoints.control_plane).toString());
  expect(response.ok(), `${response.status()} ${path}`).toBeTruthy();
  return await response.json() as JsonRecord;
}

function asRecord(value: unknown): JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as JsonRecord
    : {};
}

function asThreadDetail(value: JsonRecord): ThreadDetail {
  return {
    ...value,
    thread: asRecord(value.thread),
    deliveries: asArray(value.deliveries).map(asRecord),
    facts: asRecord(value.facts),
  };
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}
