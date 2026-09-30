import { execFileSync } from "node:child_process";
import { resolve } from "node:path";

import { expect, type APIRequestContext, type Page } from "@playwright/test";

import { assertClosureSampleProposal as assertClosureSampleProposalContract } from "../../../src/e2e/closureSampleContract";

const WORKSPACE_ENV = "SIDERA_BASIC_FAKE_WORKSPACE_ID";
const MANIFEST_ENV = "SIDERA_BASIC_FAKE_MANIFEST";
const REAL_OPT_IN_ENV = "SIDERA_RUN_REAL_BASIC";
const repositoryRoot = resolve(import.meta.dirname, "../../../../..");
const controlPlaneRoot = resolve(repositoryRoot, "services/control-plane");
const demoPython = resolve(controlPlaneRoot, ".venv/bin/python");

export type BasicPlanningJourneyResult = {
  workspaceId: string;
  planningRequestId: string;
  rootTaskId: string;
  overview: Record<string, unknown>;
};

export type BasicExecutionChainResult = {
  sourceExecutions: Record<string, unknown>[];
  integrationExecution: Record<string, unknown>;
  workers: Record<string, unknown>[];
  resultOverview: Record<string, unknown>;
};

export async function runBasicPlanningJourney(
  page: Page,
  request: APIRequestContext,
): Promise<BasicPlanningJourneyResult> {
  const workspaceId = requireEnvironment(WORKSPACE_ENV);
  expect(process.env[REAL_OPT_IN_ENV] ?? "0").not.toBe("1");

  await assertProviderFreeDemo(request, workspaceId);
  await page.goto(`/workspaces/${workspaceId}/chat`);
  await page.getByRole("button", { name: "新建对话" }).first().click();

  const composer = page.getByRole("textbox", { name: "给 Main Agent 的消息" });
  await expect(composer).toBeVisible();
  await composer.fill(
    "请处理 closure-sample-v1：在当前样例仓库实现离线任务状态汇总，不联网，不安装依赖。" +
      "请规划两个可并行的实现 WorkerItem source-a、source-b，以及依赖二者成果的 integration。" +
      "source-a 只负责 out/source-a.json，source-b 只负责 out/source-b.json；" +
      "integration 必须读取控制面选定的这两份成果并生成 out/summary.json。" +
      "integration 输入组只能要求 source-a/source-b 执行意图声明会产出的成果或证据；integration 自己的 acceptance evidence 不得成为启动它的前置条件，执行顺序由 implementation 到 integration 的依赖控制。" +
      "请直接生成 Proposal 并等待确认，不要提出澄清问题。",
  );
  await page.getByRole("button", { name: "发送消息" }).click();

  await expect(page.getByText(/需求已提交，规划请求/)).toBeVisible({
    timeout: 120_000,
  });
  const planningLink = page.getByRole("link", { name: "查看对象" }).last();
  await expect(planningLink).toBeVisible({ timeout: 30_000 });
  const planningHref = await planningLink.getAttribute("href");
  if (planningHref === null) {
    throw new Error("fake planning result did not expose a planning deep link");
  }
  const planningMatch = planningHref.match(/\/planning\/([0-9a-f-]{36})$/i);
  if (planningMatch === null) {
    throw new Error("fake planning result exposed an invalid planning deep link");
  }

  const planningRequestId = planningMatch[1]!;
  const planningDetailPath =
    `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/planning-requests/${planningRequestId}`;
  await expect.poll(
    async () => {
      const response = await request.get(planningDetailPath);
      if (!response.ok()) {
        return `http_${response.status()}`;
      }
      const body = await objectJson(response, "planning request");
      return typeof body.status === "string" ? body.status : "missing_status";
    },
    { timeout: 120_000, intervals: [250, 500, 1_000, 2_000] },
  ).toBe("proposal_ready");

  const planningDetailResponse = await request.get(planningDetailPath);
  expect(planningDetailResponse.ok()).toBeTruthy();
  const planningDetail = await objectJson(planningDetailResponse, "planning request");
  expect(planningDetail.status).toBe("proposal_ready");
  const proposalRevision = Number(planningDetail.current_proposal_revision);
  expect(Number.isInteger(proposalRevision)).toBeTruthy();
  expect(proposalRevision).toBeGreaterThan(0);

  const proposalResponse = await request.get(
    `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/planning-requests/${planningRequestId}/proposals/${proposalRevision}`,
  );
  expect(proposalResponse.ok()).toBeTruthy();
  const proposal = await objectJson(proposalResponse, "planning proposal");
  assertClosureSampleProposal(proposal);

  const taskOverviewsResponse = await request.get(
    `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/task-overviews`,
  );
  expect(taskOverviewsResponse.ok()).toBeTruthy();
  const taskOverviews = await taskOverviewsResponse.json();
  expect(Array.isArray(taskOverviews)).toBeTruthy();
  expect(taskOverviews).toHaveLength(0);

  const executionsResponse = await request.get(
    `/api/v1/executions?workspace_id=${encodeURIComponent(workspaceId)}`,
  );
  expect(executionsResponse.ok()).toBeTruthy();
  const executions = await executionsResponse.json();
  expect(Array.isArray(executions)).toBeTruthy();
  expect(executions).toHaveLength(0);

  await planningLink.click();
  await expect(page).toHaveURL(
    new RegExp(`/workspaces/${escapeRegExp(workspaceId)}/planning/${planningMatch[1]}$`),
  );

  await expect(page.getByRole("button", { name: "确认并创建任务" })).toBeVisible({
    timeout: 120_000,
  });
  await page.getByRole("button", { name: "确认并创建任务" }).click();
  await expect(page).toHaveURL(
    new RegExp(`/workspaces/${escapeRegExp(workspaceId)}/tasks/[0-9a-f-]{36}$`),
  );

  const rootTaskId = lastPathSegment(page.url());
  await expect(page.locator("#task-detail-title")).toBeVisible();
  const overviewResponse = await request.get(
    `/api/v1/tasks/${rootTaskId}/overview?workspace_id=${encodeURIComponent(workspaceId)}`,
  );
  expect(overviewResponse.ok()).toBeTruthy();
  const overview = await objectJson(overviewResponse, "task overview");
  expect(overview.root_task_id).toBe(rootTaskId);
  expect(Number(overview.subtask_count)).toBeGreaterThan(0);

  const graphResponse = await request.get(
    `/api/v1/tasks/${rootTaskId}/graph?workspace_id=${encodeURIComponent(workspaceId)}`,
  );
  expect(graphResponse.ok()).toBeTruthy();
  const graph = await objectJson(graphResponse, "task graph");
  const graphOverview = graph.overview;
  if (typeof graphOverview !== "object" || graphOverview === null || Array.isArray(graphOverview)) {
    throw new Error("task graph response did not include an overview object");
  }
  expect((graphOverview as Record<string, unknown>).root_task_id).toBe(rootTaskId);

  releaseBasicFakeGate("parallel");
  releaseBasicFakeGate("before-artifact");

  return {
    workspaceId,
    planningRequestId,
    rootTaskId,
    overview,
  };
}

function releaseBasicFakeGate(gate: "parallel" | "before-artifact"): void {
  const manifestPath = process.env[MANIFEST_ENV];
  if (manifestPath === undefined || manifestPath.trim() === "") {
    throw new Error(`${MANIFEST_ENV} is required to release the fake gate`);
  }
  execFileSync(
    demoPython,
    [
      "-m",
      "sidera_control_plane.demo.cli",
      "continue",
      "--manifest",
      manifestPath,
      "--gate",
      gate,
    ],
    {
      cwd: controlPlaneRoot,
      env: {
        ...process.env,
        PATH: process.env.PATH ?? "/opt/homebrew/bin:/usr/bin:/bin",
        LANG: process.env.LANG ?? "C",
        LC_ALL: process.env.LC_ALL ?? "C",
      },
      stdio: "pipe",
    },
  );
}

export async function waitForBasicExecutionChain(
  request: APIRequestContext,
  journey: BasicPlanningJourneyResult,
): Promise<BasicExecutionChainResult> {
  expect(process.env[REAL_OPT_IN_ENV] ?? "0").not.toBe("1");
  const executionPath = `/api/v1/executions?workspace_id=${encodeURIComponent(journey.workspaceId)}`;
  let executions: Record<string, unknown>[] = [];
  await expect.poll(
    async () => {
      const response = await request.get(executionPath);
      if (!response.ok()) return `http_${response.status()}`;
      executions = await arrayJson(response, "executions");
      const taskExecutions = executions.filter(
        (execution) => execution.task_id !== undefined,
      );
      const sourceExecutions = taskExecutions.filter((execution) => {
        const purpose = stringValue(execution.purpose);
        return purpose.includes("source-a.json") || purpose.includes("source-b.json");
      });
      const integrationExecution = taskExecutions.find((execution) =>
        stringValue(execution.purpose).includes("summary.json"),
      );
      if (
        sourceExecutions.length !== 2
        || integrationExecution === undefined
        || sourceExecutions.some((execution) => execution.status !== "succeeded")
        || integrationExecution.status !== "succeeded"
      ) {
        return `waiting:${sourceExecutions.length}:${integrationExecution?.status ?? "missing"}`;
      }
      return "ready";
    },
    { timeout: 120_000, intervals: [250, 500, 1_000, 2_000] },
  ).toBe("ready");

  const sourceExecutions = executions.filter((execution) => {
    const purpose = stringValue(execution.purpose);
    return purpose.includes("source-a.json") || purpose.includes("source-b.json");
  });
  const integrationExecution = executions.find((execution) =>
    stringValue(execution.purpose).includes("summary.json"),
  );
  if (integrationExecution === undefined || sourceExecutions.length !== 2) {
    throw new Error("provider-free chain did not expose the expected execution graph");
  }

  const sourceWindows = sourceExecutions.map((execution) => ({
    start: dateValue(execution.started_at, "source started_at"),
    finish: dateValue(execution.finished_at, "source finished_at"),
  }));
  const latestSourceFinish = Math.max(...sourceWindows.map((window) => window.finish));
  const integrationStart = dateValue(integrationExecution.started_at, "integration started_at");
  expect(integrationStart).toBeGreaterThanOrEqual(latestSourceFinish);
  expect(Math.max(...sourceWindows.map((window) => window.start))).toBeLessThanOrEqual(
    Math.min(...sourceWindows.map((window) => window.finish)),
  );

  for (const execution of [...sourceExecutions, integrationExecution]) {
    expect(execution.active_lease_id ?? null).toBeNull();
    const leases = await request.get(
      `/api/v1/executions/${encodeURIComponent(stringValue(execution.id))}/leases?workspace_id=${encodeURIComponent(journey.workspaceId)}`,
    );
    expect(leases.ok()).toBeTruthy();
    const leaseRows = await arrayJson(leases, "execution leases");
    expect(leaseRows.every((lease) => lease.status !== "active")).toBeTruthy();
  }

  const workersResponse = await request.get(
    `/api/v1/workers?workspace_id=${encodeURIComponent(journey.workspaceId)}`,
  );
  expect(workersResponse.ok()).toBeTruthy();
  const workers = await arrayJson(workersResponse, "workers");
  expect(workers.every((worker) => worker.current_load === 0)).toBeTruthy();

  const resultPath =
    `/api/v1/workspaces/${encodeURIComponent(journey.workspaceId)}/tasks/${encodeURIComponent(journey.rootTaskId)}/results`;
  let resultOverview: Record<string, unknown> = {};
  await expect.poll(
    async () => {
      const response = await request.get(resultPath);
      if (!response.ok()) return `http_${response.status()}`;
      resultOverview = await objectJson(response, "result overview");
      if (resultOverview.current_candidate === null) return "candidate_missing";
      const criteria = resultOverview.criteria;
      if (!Array.isArray(criteria) || criteria.length === 0) return "criteria_missing";
      if (criteria.some((criterion) => (
        typeof criterion !== "object"
        || criterion === null
        || Array.isArray(criterion)
        || (criterion as Record<string, unknown>).observed_status !== "passed"
      ))) {
        return "verification_pending";
      }
      return "verified";
    },
    { timeout: 120_000, intervals: [250, 500, 1_000, 2_000] },
  ).toBe("verified");
  return { sourceExecutions, integrationExecution, workers, resultOverview };
}

export async function inspectBasicWorkerTrace(
  page: Page,
  request: APIRequestContext,
  journey: BasicPlanningJourneyResult,
  chain: BasicExecutionChainResult,
): Promise<void> {
  const sourceExecution = chain.sourceExecutions[0];
  if (sourceExecution === undefined) {
    throw new Error("provider-free chain did not expose a source execution for Trace");
  }
  const executionId = stringField(sourceExecution, "id", "source execution id");
  const workerItemId = stringField(sourceExecution, "worker_item_id", "source WorkerItem id");
  const sessionsResponse = await request.get(
    `/api/v1/gateway/sessions?workspace_id=${encodeURIComponent(journey.workspaceId)}&execution_id=${encodeURIComponent(executionId)}&limit=10`,
  );
  expect(sessionsResponse.ok()).toBeTruthy();
  const sessionsPage = await objectJson(sessionsResponse, "execution sessions");
  const sessions = arrayField(sessionsPage, "items", "execution sessions");
  const session = sessions[0];
  if (session === undefined) {
    throw new Error("provider-free source execution did not expose a session");
  }
  const sessionId = stringField(session, "id", "execution session id");
  const traceResponse = await request.get(
    `/api/v1/tasks/${encodeURIComponent(journey.rootTaskId)}/executions/${encodeURIComponent(executionId)}/sessions/${encodeURIComponent(sessionId)}/trace?workspace_id=${encodeURIComponent(journey.workspaceId)}&limit=100`,
  );
  expect(traceResponse.ok()).toBeTruthy();
  const trace = await objectJson(traceResponse, "worker trace");
  const traceEvents = arrayField(trace, "events", "worker trace events");
  const completion = traceEvents.find((event) => event.title === "回合已完成");
  expect(completion).toBeDefined();
  expect(completion?.summary).toBe("Agent 已完成本轮执行");
  expect(JSON.stringify(trace)).not.toContain("end_turn");

  await page.goto(`/workspaces/${journey.workspaceId}/worker-items/${workerItemId}`);
  await expect(page.locator("#worker-item-title")).toBeVisible();
  const traceButton = page.getByRole("button", { name: "查看当前 Agent Trace" });
  await expect(traceButton).toBeVisible();
  await traceButton.click();
  const dialog = page.getByRole("dialog", { name: "Agent Trace" });
  await expect(dialog).toBeVisible();
  const completionRow = dialog.getByRole("button", { name: /回合已完成/ });
  await expect(completionRow).toBeVisible();
  await expect(completionRow).toContainText("已完成");
  await expect(dialog).not.toContainText("end_turn");
  await dialog.getByRole("button", { name: "关闭 Agent Trace" }).click();
  await expect(dialog).toHaveCount(0);
}

export async function assertProviderFreeDemo(
  request: APIRequestContext,
  workspaceId: string,
): Promise<void> {
  const response = await request.get("/api/v1/demo/status");
  expect(response.ok()).toBeTruthy();
  const status = await objectJson(response, "demo status");
  expect(status.mode).toBe("interview-demo");
  expect(status.workspace_id).toBe(workspaceId);
  expect(status.phase).toBe("running");
  for (const name of [
    "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY",
    "OPENAI_API_KEY",
    "OPENCODE_API_KEY",
  ]) {
    expect(process.env[name] ?? "").toBe("");
  }
}

function requireEnvironment(name: string): string {
  const value = process.env[name];
  if (value === undefined || value.trim() === "") {
    throw new Error(`${name} is required by the provider-free fake journey`);
  }
  return value;
}

async function objectJson(
  response: Awaited<ReturnType<APIRequestContext["get"]>>,
  label: string,
): Promise<Record<string, unknown>> {
  const value: unknown = await response.json();
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error(`${label} response was not an object`);
  }
  return value as Record<string, unknown>;
}

async function arrayJson(
  response: Awaited<ReturnType<APIRequestContext["get"]>>,
  label: string,
): Promise<Record<string, unknown>[]> {
  const value: unknown = await response.json();
  if (!Array.isArray(value) || value.some((item) => typeof item !== "object" || item === null || Array.isArray(item))) {
    throw new Error(`${label} response was not an object array`);
  }
  return value as Record<string, unknown>[];
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function dateValue(value: unknown, label: string): number {
  if (typeof value !== "string") throw new Error(`${label} was missing`);
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) throw new Error(`${label} was invalid`);
  return timestamp;
}

function lastPathSegment(value: string): string {
  const segment = new URL(value).pathname.split("/").filter(Boolean).at(-1);
  if (segment === undefined || !/^[0-9a-f-]{36}$/i.test(segment)) {
    throw new Error("task URL did not expose a UUID root task id");
  }
  return segment;
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function assertClosureSampleProposal(proposal: Record<string, unknown>): void {
  assertClosureSampleProposalContract(proposal);

  const collaboration = recordField(proposal, "collaboration", "planning proposal");
  const inputGroups = arrayField(
    collaboration,
    "input_groups",
    "proposal input groups",
  );
  if (!inputGroups.some((item) => (
    stringField(item, "group_key", "proposal input group key") === "closure-inputs"
  ))) {
    throw new Error("closure sample proposal did not include closure-inputs");
  }
}

function recordField(
  value: Record<string, unknown>,
  key: string,
  label: string,
): Record<string, unknown> {
  const candidate = value[key];
  if (typeof candidate !== "object" || candidate === null || Array.isArray(candidate)) {
    throw new Error(`${label} did not include an object field: ${key}`);
  }
  return candidate as Record<string, unknown>;
}

function arrayField(
  value: Record<string, unknown>,
  key: string,
  label: string,
): Record<string, unknown>[] {
  const candidate = value[key];
  if (!Array.isArray(candidate) || candidate.some((item) => typeof item !== "object" || item === null || Array.isArray(item))) {
    throw new Error(`${label} did not include an object array: ${key}`);
  }
  return candidate as Record<string, unknown>[];
}

function stringField(value: Record<string, unknown>, key: string, label: string): string {
  const candidate = value[key];
  if (typeof candidate !== "string" || candidate.trim() === "") {
    throw new Error(`${label} did not include a string field: ${key}`);
  }
  return candidate;
}
