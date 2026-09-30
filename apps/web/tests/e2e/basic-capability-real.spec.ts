import {
  chmodSync,
  mkdirSync,
  readFileSync,
  renameSync,
  writeFileSync,
} from "node:fs";
import { dirname, resolve } from "node:path";

import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

import {
  classifyRealExecutionAttempts,
  isRealSourceExecutionPurpose,
  latestRealExecutionAttempts,
  realTaskExecutions,
  realBasicBrowserTimeoutMs,
  safeRealExecutionFailureCode,
  requireRealAcceptanceGroup,
} from "../../src/e2e/basicCapabilityRealBoundary";
import {
  assertClosureSampleIntegrationInputs,
  assertClosureSampleProposal,
  closureSampleAcceptanceMarkerInstruction,
  findClosureSampleIntegrationInputGroups,
} from "../../src/e2e/closureSampleContract";
import { waitForProposalOrPlanningFailure } from "./support/planning-proposal-gate";

const REAL_RUN = process.env.SIDERA_RUN_REAL_BASIC === "1";
const SECRET_PATTERN = /(?:authorization|token|api[_-]?key|secret|credential)\s*[:=]/i;

test.describe("R6 basic real planning journey", () => {
  test("submits a brief, reviews a Proposal, and confirms one plan", async ({ page, request }) => {
    test.skip(!REAL_RUN, "SIDERA_RUN_REAL_BASIC is not enabled");
    test.skip(
      process.env.SIDERA_BASIC_ACCEPTANCE_GROUP !== "A",
      "A journey runs only when SIDERA_BASIC_ACCEPTANCE_GROUP=A",
    );

    const workspaceId = required("SIDERA_BASIC_WORKSPACE_ID");
    const gatewayURL = required("SIDERA_MAIN_AGENT_GATEWAY_URL").replace(/\/$/, "");
    const group = requireRealAcceptanceGroup(process.env.SIDERA_BASIC_ACCEPTANCE_GROUP, "A");
    const marker = `SIDERA-R6-BASIC-${group}-${Date.now().toString(36)}`;
    const markerInstruction = closureSampleAcceptanceMarkerInstruction(marker);
    const manifest = readRealManifest();
    const violations: string[] = [];
    page.on("request", (request) => {
      const headers = Object.keys(request.headers()).join(" ");
      const body = request.postData() ?? "";
      if (SECRET_PATTERN.test(headers) || SECRET_PATTERN.test(body)) {
        violations.push("provider credential crossed browser boundary");
      }
    });

    const health = await request.get(`${gatewayURL}/main-agent/v1/health`);
    expect(health.ok()).toBeTruthy();
    expect((await health.json() as { provider?: string }).provider).toBe("available");

    await page.goto(`/workspaces/${workspaceId}/tasks`);
    await page.getByRole("region", { name: "任务" }).getByRole("link", { name: "新建任务" }).click();
    await page.getByLabel("需求目标").fill(
      `请处理 closure-sample-v1，在当前样例仓库实现离线任务状态汇总，不联网、不安装依赖。` +
        "请只拆成两个 Subtask：implementation 包含可并行的 source-a、source-b，integration 只包含 integration；" +
        "source-a 只能创建或替换 out/source-a.json，内容必须严格为 " +
        '`{"marker":"SIDERA_CLOSURE_SOURCE_A_V1","items":[{"id":"api","status":"done"},{"id":"ui","status":"queued"}]}`' +
        "；source-b 只能创建或替换 out/source-b.json，内容必须严格为 " +
        '`{"marker":"SIDERA_CLOSURE_SOURCE_B_V1","items":[{"id":"tests","status":"running"},{"id":"docs","status":"blocked"}]}`' +
        `；integration 必须只读取控制面选定的这两份成果，保留两份 source 文件并创建 out/summary.json，内容汇总四条记录、四种状态各一条、按 source-a/source-b 顺序保留两个 source marker，并额外保留汇总标记 ${marker}。${markerInstruction}` +
        "integration 输入组只能要求 source-a/source-b 执行意图声明会产出的成果或证据；不得把 integration 自己的 acceptance evidence 设为启动 integration 的前置条件，执行顺序由 implementation 到 integration 的依赖控制。" +
        `该唯一汇总标记 ${marker} 是精确字面量：Planner Proposal 中 integration WorkerItem 的 responsibility 和 integration_spec.criteria.integration.text 都必须逐字包含它，不得概括成“汇总标记”或“required markers”。` +
        "integration_spec 必须选择系统提供的 patch-integrity 检查作为唯一 check，并关联到 automatic integration criterion。" +
        "不要修改 README 或其他代码，不要运行全量测试；控制面会负责验收。",
    );
    await page.getByRole("textbox", { name: "验收条件" }).first().fill(
      `source-a 和 source-b 并行完成；integration 等待两者完成，只读取控制面选定的两份输入，生成带有四种状态计数和汇总标记 ${marker} 的 out/summary.json；${markerInstruction} integration 输入组只能要求 source-a/source-b 执行意图声明的输出，不得要求 integration 自己的 acceptance evidence 才能启动；integration WorkerItem responsibility 与 integration_spec.criteria.integration.text 必须都原样包含 ${marker}；控制面验收结果并接受候选。`,
    );
    await page.getByRole("button", { name: "提交需求" }).click();
    await expect(page).toHaveURL(new RegExp(`/workspaces/${escapeRegExp(workspaceId)}/planning/[0-9a-f-]{36}$`));
    const planningRequestId = lastPathSegment(page.url());
    await expect(page.locator("#planning-request-title")).toHaveText("规划请求");
    await expect(page.locator(".planning-status")).toContainText(
      /规划中|方案待确认|等待你的回答|规划失败|需要运维恢复/,
    );

    const clarification = page.getByRole("button", { name: "提交澄清答复" });
    if (await clarification.count() > 0) {
      throw new Error("basic real planning unexpectedly requested a clarification");
    }
    await waitForProposalOrPlanningFailure(page, 360_000);
    await expect(page.getByRole("button", { name: "确认并创建任务" })).toBeVisible();
    const proposalState = await readProposalIdentity(
      request,
      workspaceId,
      planningRequestId,
      marker,
    );
    const confirmationResponsePromise = page.waitForResponse((response) => {
      const pathname = new URL(response.url()).pathname;
      return response.request().method() === "POST" && pathname === "/api/v1/commands";
    });
    await page.getByRole("button", { name: "确认并创建任务" }).click();
    const confirmationResponse = await confirmationResponsePromise;
    expect(confirmationResponse.ok()).toBeTruthy();
    const confirmation = await objectJson(confirmationResponse, "planning confirmation");
    const confirmationCommandId = stringField(confirmation, "command_id", "planning confirmation");
    await expect(page).toHaveURL(new RegExp(`/workspaces/${escapeRegExp(workspaceId)}/tasks/[0-9a-f-]{36}$`));
    const rootTaskId = lastPathSegment(page.url());
    await expect(page.locator("#task-detail-title")).toBeVisible();
    const subtask = page.getByRole("button", { name: /打开子任务：/ }).first();
    await expect(subtask).toBeVisible();
    await subtask.click();
    await expect(page.locator("#subtask-title")).toBeVisible();
    const worker = page.getByRole("button", { name: /打开 Worker：/ }).first();
    await expect(worker).toBeVisible();
    await worker.click();
    await expect(page.locator("#worker-item-title")).toBeVisible();
    await expect(page.getByRole("textbox", { name: "介入这个 Worker" })).toBeVisible();

    const rootGraph = await readRootGraph(request, workspaceId, rootTaskId);
    const executionTaskIds = [
      rootTaskId,
      ...arrayField(rootGraph, "nodes", "confirmed root graph nodes")
        .map((node, index) => stringField(node, "task_id", `confirmed root graph node ${index}`)),
    ];
    const chain = await waitForRealExecutionChain(request, {
      manifest,
      workspaceId,
      rootTaskId,
      executionTaskIds,
    });
    await assertRealIntegrationInputs(request, {
      workspaceId,
      rootTaskId,
      integrationWorkerItemId: stringField(
        chain.integrationExecution,
        "worker_item_id",
        "real integration execution",
      ),
      resultOverview: chain.resultOverview,
    });
    const downstreamArtifactId = await requireCandidateArtifactWithMarker(
      request,
      workspaceId,
      chain.resultOverview,
      marker,
    );
    await acceptRealResult(page, request, workspaceId, rootTaskId, chain.resultOverview);
    const scenarioFacts = await collectRealScenarioFacts(request, {
      manifest,
      workspaceId,
      rootTaskId,
      marker,
      chain,
      downstreamArtifactId,
    });
    const resultSnapshot = buildResultScenarioSnapshot(scenarioFacts);
    recordScenarioObservation(manifest, "result-inputs", resultSnapshot);
    recordScenarioObservation(manifest, "result-integration", resultSnapshot);
    recordScenarioObservation(manifest, "user-flow", buildUserFlowSnapshot(manifest, scenarioFacts));

    await recordGroupObservation(request, {
      group,
      workspaceId,
      planningRequestId,
      proposalId: proposalState.proposalId,
      confirmationCommandId,
      rootTaskId,
      manifest,
      chain,
      scenarioFacts,
    });

    expect(violations).toEqual([]);
    const storage = await page.evaluate(() => {
      const values = (source: Storage) => Array.from({ length: source.length }, (_, index) => {
        const key = source.key(index) ?? "";
        return `${key}=${source.getItem(key) ?? ""}`;
      });
      return [...values(window.localStorage), ...values(window.sessionStorage)].join("\n");
    });
    expect(SECRET_PATTERN.test(storage)).toBeFalsy();
  });
});

function required(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is required for the real basic journey`);
  return value;
}

type AcceptanceGroup = "A" | "B" | "C";

type RealChain = {
  sourceExecutions: Record<string, unknown>[];
  integrationExecution: Record<string, unknown>;
  workers: Record<string, unknown>[];
  workerIds: string[];
  resultOverview: Record<string, unknown>;
};

type RealScenarioFacts = {
  workspaceId: string;
  rootTaskId: string;
  sourceExecutionIds: string[];
  integrationExecutionId: string;
  sourceSessionIds: string[];
  integrationSessionId: string;
  selectionId: string;
  selectionHash: string;
  bundleId: string;
  contextPackageId: string;
  contextPackageHash: string;
  selectedArtifactHashes: [string, string][];
  downstreamArtifactId: string;
  downstreamMarker: string;
  candidateId: string;
  candidateHash: string;
  candidateSelectionHash: string;
  candidateResultTree: string;
  acceptanceId: string;
  acceptedCandidateId: string;
  acceptedCandidateHash: string;
  budgetState: "completed" | "reserved" | "exhausted" | "unknown";
  sessionState: "succeeded" | "failed" | "timed_out" | "unknown";
};

async function readProposalIdentity(
  request: APIRequestContext,
  workspaceId: string,
  planningRequestId: string,
  requiredMarker: string,
): Promise<{ proposalId: string }> {
  const detailResponse = await request.get(
    `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/planning-requests/${planningRequestId}`,
  );
  expect(detailResponse.ok()).toBeTruthy();
  const detail = await objectJson(detailResponse, "planning request");
  const revision = Number(detail.current_proposal_revision);
  expect(Number.isInteger(revision)).toBeTruthy();
  expect(revision).toBeGreaterThan(0);
  const proposalResponse = await request.get(
    `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/planning-requests/${planningRequestId}/proposals/${revision}`,
  );
  expect(proposalResponse.ok()).toBeTruthy();
  const proposal = await objectJson(proposalResponse, "planning proposal");
  const proposalId = stringField(proposal, "id", "planning proposal");
  try {
    assertClosureSampleProposal(proposal, requiredMarker);
    const display = recordField(proposal, "display", "planning proposal");
    const integrationSpec = recordField(display, "integration_spec", "planning proposal");
    const checks = arrayField(integrationSpec, "checks", "planning proposal integration spec");
    expect(checks.map((check) => check.key)).toEqual(["patch-integrity"]);
    const integrationCriterion = arrayField(
      integrationSpec,
      "criteria",
      "planning proposal integration spec",
    ).find((criterion) => criterion.key === "integration");
    expect(integrationCriterion, "proposal must expose the automatic integration criterion").toBeDefined();
    expect(integrationCriterion?.check_keys).toEqual(["patch-integrity"]);
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    throw new Error(
      `closure-sample proposal rejected before confirmation: request=${planningRequestId} revision=${revision} proposal=${proposalId}; ${detail}`,
      { cause: error },
    );
  }
  return { proposalId };
}

async function assertRealIntegrationInputs(
  request: APIRequestContext,
  facts: {
    workspaceId: string;
    rootTaskId: string;
    integrationWorkerItemId: string;
    resultOverview: Record<string, unknown>;
  },
): Promise<void> {
  const resultInputs = await readResultInputs(
    request,
    facts.workspaceId,
    facts.integrationWorkerItemId,
  );
  const inputGroups = findClosureSampleIntegrationInputGroups(resultInputs);
  if (inputGroups === null) {
    throw new Error(
      `closure-sample integration input groups are missing: root_task=${facts.rootTaskId} worker_item=${facts.integrationWorkerItemId}`,
    );
  }
  const selectionId = stringField(inputGroups[0]!, "selection_id", "real result input group");
  const candidate = recordField(facts.resultOverview, "current_candidate", "real result overview");
  const candidateId = stringField(candidate, "candidate_id", "real result candidate");
  const workspace = encodeURIComponent(facts.workspaceId);
  const [selectionResponse, candidateResponse] = await Promise.all([
    request.get(
      `/api/v1/workspaces/${workspace}/collaboration/input-selections/${encodeURIComponent(selectionId)}`,
    ),
    request.get(
      `/api/v1/workspaces/${workspace}/tasks/${encodeURIComponent(facts.rootTaskId)}/results/${encodeURIComponent(candidateId)}`,
    ),
  ]);
  expect(selectionResponse.ok()).toBeTruthy();
  expect(candidateResponse.ok()).toBeTruthy();
  try {
    assertClosureSampleIntegrationInputs({
      resultInputs,
      selection: await objectJson(selectionResponse, "real input selection"),
      resultOverview: facts.resultOverview,
      candidateDetail: await objectJson(candidateResponse, "real result candidate detail"),
    });
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    throw new Error(
      `closure-sample integration inputs rejected before acceptance: root_task=${facts.rootTaskId} candidate=${candidateId}; ${detail}`,
      { cause: error },
    );
  }
}

async function recordGroupObservation(
  request: APIRequestContext,
  facts: {
    group: AcceptanceGroup;
    workspaceId: string;
    planningRequestId: string;
    proposalId: string;
    confirmationCommandId: string;
    rootTaskId: string;
    manifest: Record<string, unknown>;
    chain: RealChain;
    scenarioFacts: RealScenarioFacts;
  },
): Promise<void> {
  const commandResponse = await request.get(
    `/api/v1/workspaces/${encodeURIComponent(facts.workspaceId)}/commands/${facts.confirmationCommandId}`,
  );
  expect(commandResponse.ok()).toBeTruthy();
  const command = await objectJson(commandResponse, "planning confirmation command");
  expect(command.status).toBe("applied");
  const result = recordField(command, "result", "planning confirmation command");
  const resultData = recordField(result, "data", "planning confirmation result");
  expect(findUuidField(resultData, "root_task_id")).toBe(facts.rootTaskId);

  const taskResponse = await request.get(
    `/api/v1/tasks/${facts.rootTaskId}?workspace_id=${encodeURIComponent(facts.workspaceId)}`,
  );
  expect(taskResponse.ok()).toBeTruthy();
  const task = await objectJson(taskResponse, "confirmed root task");
  expect(findUuidField(task, "root_task_id") ?? findUuidField(task, "id")).toBe(facts.rootTaskId);

  const runId = stringField(facts.manifest, "run_id", "real manifest");
  const implementationFingerprint = stringField(
    facts.manifest,
    "implementation_fingerprint",
    "real manifest",
  );
  const runtimeFingerprint = stringField(facts.manifest, "runtime_fingerprint", "real manifest");
  recordAtomicObservation(facts.manifest, `group-${facts.group}`, {
    schema_version: 1,
    run_id: runId,
    workspace_id: facts.workspaceId,
    implementation_fingerprint: implementationFingerprint,
    runtime_fingerprint: runtimeFingerprint,
    group: facts.group,
    root_task_id: facts.rootTaskId,
    command_ids: [facts.confirmationCommandId],
    resource_ids: [
      `planning_request:${facts.planningRequestId}`,
      `proposal:${facts.proposalId}`,
      `root_task:${facts.rootTaskId}`,
    ],
    source_execution_ids: [...facts.scenarioFacts.sourceExecutionIds].sort(),
    integration_execution_id: facts.scenarioFacts.integrationExecutionId,
    terminal_execution_ids: [
      ...facts.scenarioFacts.sourceExecutionIds,
      facts.scenarioFacts.integrationExecutionId,
    ].sort(),
    released_lease_execution_ids: [
      ...facts.scenarioFacts.sourceExecutionIds,
      facts.scenarioFacts.integrationExecutionId,
    ].sort(),
    released_worker_ids: [...facts.chain.workerIds].sort(),
    result_candidate_id: facts.scenarioFacts.candidateId,
    result_accepted: facts.scenarioFacts.acceptanceId !== "",
    status: "completed",
  });
}

function readRealManifest(): Record<string, unknown> {
  const manifestPath = required("SIDERA_BASIC_REAL_MANIFEST");
  const manifest = JSON.parse(readFileSync(manifestPath, "utf8")) as unknown;
  if (typeof manifest !== "object" || manifest === null || Array.isArray(manifest)) {
    throw new Error("real manifest must be an object");
  }
  const value = manifest as Record<string, unknown>;
  const runRoot = stringField(value, "run_root", "real manifest");
  if (!runRoot.startsWith("/")) throw new Error("real manifest run_root must be absolute");
  return value;
}

async function readRootGraph(
  request: APIRequestContext,
  workspaceId: string,
  rootTaskId: string,
): Promise<Record<string, unknown>> {
  const response = await request.get(
    `/api/v1/tasks/${encodeURIComponent(rootTaskId)}/graph?workspace_id=${encodeURIComponent(workspaceId)}`,
  );
  expect(response.ok()).toBeTruthy();
  const graph = await objectJson(response, "confirmed root graph");
  const overview = recordField(graph, "overview", "confirmed root graph");
  expect(overview.root_task_id).toBe(rootTaskId);
  expect(arrayField(graph, "nodes", "confirmed root graph nodes").length).toBeGreaterThan(0);
  return graph;
}

async function waitForRealExecutionChain(
  request: APIRequestContext,
  facts: {
    manifest: Record<string, unknown>;
    workspaceId: string;
    rootTaskId: string;
    executionTaskIds: readonly string[];
  },
): Promise<RealChain> {
  const timeoutMs = realBasicBrowserTimeoutMs(
    facts.manifest as { scenarios: Record<string, unknown> },
  );
  const deadline = Date.now() + timeoutMs;
  const intervals = [500, 1_000, 2_000, 5_000];
  let intervalIndex = 0;
  let lastState = "executions_missing";
  while (Date.now() < deadline) {
    const executionsResponse = await safeRequestGet(
      request,
      `/api/v1/executions?workspace_id=${encodeURIComponent(facts.workspaceId)}`,
    );
    if (executionsResponse === null) {
      lastState = "executions_request_unavailable";
    } else if (!executionsResponse.ok()) {
      lastState = `executions_http_${executionsResponse.status()}`;
    } else {
      const executions = await arrayJson(executionsResponse, "real executions");
      const taskExecutions = realTaskExecutions(executions, facts.executionTaskIds);
      const relevantExecutions = taskExecutions.filter((execution) => (
        isRealSourceExecutionPurpose(stringValue(execution.purpose))
        || isIntegrationPurpose(stringValue(execution.purpose))
      ));
      const currentExecutions = latestRealExecutionAttempts(relevantExecutions);
      const sourceExecutions = currentExecutions
        .filter((execution) => isRealSourceExecutionPurpose(stringValue(execution.purpose)))
        .sort((left, right) => stringValue(left.purpose).localeCompare(stringValue(right.purpose)));
      const integrationExecution = currentExecutions.find((execution) => (
        isIntegrationPurpose(stringValue(execution.purpose))
      ));
      if (classifyRealExecutionAttempts(currentExecutions) === "failed") {
        throw new Error(`real execution chain reached terminal failure: ${executionStatusSummary(sourceExecutions, integrationExecution)}`);
      }
      if (sourceExecutions.length !== 2 || integrationExecution === undefined) {
        lastState = executionStatusSummary(sourceExecutions, integrationExecution);
      } else {
        const executionSet = [...sourceExecutions, integrationExecution];
        const attemptState = classifyRealExecutionAttempts(executionSet);
        if (attemptState === "failed") {
          throw new Error(`real execution chain reached terminal failure: ${executionStatusSummary(sourceExecutions, integrationExecution)}`);
        }
        if (attemptState === "pending") {
          lastState = executionStatusSummary(sourceExecutions, integrationExecution);
        } else {
          const sourceWindows = sourceExecutions.map((execution) => ({
            started: dateValue(execution.started_at, "real source started_at"),
            finished: dateValue(execution.finished_at, "real source finished_at"),
          }));
          const integrationStarted = dateValue(
            integrationExecution.started_at,
            "real integration started_at",
          );
          if (integrationStarted < Math.max(...sourceWindows.map((window) => window.finished))) {
            lastState = "waiting:integration_before_sources";
          } else if (Math.max(...sourceWindows.map((window) => window.started)) >= Math.min(...sourceWindows.map((window) => window.finished))) {
            lastState = "waiting:sources_not_overlapped";
          } else if (!(await executionLeasesReleased(request, facts.workspaceId, executionSet))) {
            lastState = "waiting:leases";
          } else {
            const workersResponse = await safeRequestGet(
              request,
              `/api/v1/workers?workspace_id=${encodeURIComponent(facts.workspaceId)}`,
            );
            if (workersResponse === null) {
              lastState = "workers_request_unavailable";
            } else if (!workersResponse.ok()) {
              lastState = `workers_http_${workersResponse.status()}`;
            } else {
              const workers = await arrayJson(workersResponse, "real workers");
              const workerIds = [...new Set(
                executionSet
                  .map((execution) => stringValue(execution.assigned_worker_id))
                  .filter((workerId) => workerId !== ""),
              )].sort();
              if (workerIds.length === 0) {
                lastState = "waiting:assigned_workers";
              } else {
                const workerById = new Map(
                  workers.map((worker) => [stringValue(worker.id), worker]),
                );
                if (workerIds.some((workerId) => workerById.get(workerId)?.current_load !== 0)) {
                  lastState = "waiting:worker_capacity";
                } else {
                  const resultOverviewResponse = await safeRequestGet(
                    request,
                    `/api/v1/workspaces/${encodeURIComponent(facts.workspaceId)}/tasks/${encodeURIComponent(facts.rootTaskId)}/results`,
                  );
                  if (resultOverviewResponse === null) {
                    lastState = "results_request_unavailable";
                  } else if (!resultOverviewResponse.ok()) {
                    lastState = `results_http_${resultOverviewResponse.status()}`;
                  } else {
                    const resultOverview = await objectJson(resultOverviewResponse, "real result overview");
                    const candidate = resultOverview.current_candidate;
                    const criteria = resultOverview.criteria;
                    if (typeof candidate !== "object" || candidate === null || Array.isArray(candidate)) {
                      lastState = "waiting:result_candidate";
                    } else if (!Array.isArray(criteria) || criteria.length === 0 || criteria.some((criterion) => (
                      typeof criterion !== "object"
                      || criterion === null
                      || Array.isArray(criterion)
                      || (criterion as Record<string, unknown>).observed_status !== "passed"
                    ))) {
                      lastState = "waiting:result_verification";
                    } else {
                      return {
                        sourceExecutions,
                        integrationExecution,
                        workers,
                        workerIds,
                        resultOverview,
                      };
                    }
                  }
                }
              }
            }
          }
        }
      }
    }

    const remainingMs = deadline - Date.now();
    if (remainingMs <= 0) break;
    const interval = intervals[Math.min(intervalIndex, intervals.length - 1)] ?? 5_000;
    intervalIndex += 1;
    await new Promise((resolveDelay) => setTimeout(resolveDelay, Math.min(interval, remainingMs)));
  }
  throw new Error(`real execution chain timed out after ${timeoutMs}ms; ${lastState}`);
}

async function executionLeasesReleased(
  request: APIRequestContext,
  workspaceId: string,
  executions: Record<string, unknown>[],
): Promise<boolean> {
  for (const execution of executions) {
    if (execution.active_lease_id !== null && execution.active_lease_id !== undefined) {
      return false;
    }
    const response = await safeRequestGet(
      request,
      `/api/v1/executions/${encodeURIComponent(stringField(execution, "id", "real execution"))}/leases?workspace_id=${encodeURIComponent(workspaceId)}`,
    );
    if (response === null || !response.ok()) return false;
    const leases = await arrayJson(response, "real execution leases");
    if (leases.some((lease) => lease.status === "active")) return false;
  }
  return true;
}

async function safeRequestGet(request: APIRequestContext, url: string) {
  try {
    return await request.get(url);
  } catch {
    return null;
  }
}

async function acceptRealResult(
  page: Page,
  request: APIRequestContext,
  workspaceId: string,
  rootTaskId: string,
  expectedOverview: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  await page.goto(
    `/workspaces/${encodeURIComponent(workspaceId)}/tasks/${encodeURIComponent(rootTaskId)}?view=results`,
  );
  const results = page.getByRole("region", { name: "成果验收" });
  await expect(results).toContainText("等待用户验收", { timeout: 120_000 });
  const decisions = await results.getByRole("combobox").all();
  if (decisions.length === 0) throw new Error("real result did not expose acceptance decisions");
  for (const decision of decisions) await decision.selectOption("accept_passed");
  await results.getByRole("button", { name: "接受成果" }).click();
  const expectedCandidate = recordField(expectedOverview, "current_candidate", "real result overview");
  const candidateId = stringField(expectedCandidate, "candidate_id", "real result candidate");
  let acceptedOverview: Record<string, unknown> | null = null;
  await expect.poll(
    async () => {
      const response = await request.get(
        `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/tasks/${encodeURIComponent(rootTaskId)}/results`,
      );
      if (!response.ok()) return `results_http_${response.status()}`;
      const overview = await objectJson(response, "accepted result overview");
      const acceptance = overview.acceptance;
      if (typeof acceptance !== "object" || acceptance === null || Array.isArray(acceptance)) {
        return "waiting:acceptance";
      }
      if ((acceptance as Record<string, unknown>).candidate_id !== candidateId) {
        return "waiting:acceptance_candidate";
      }
      acceptedOverview = overview;
      return "accepted";
    },
    { timeout: 120_000, intervals: [500, 1_000, 2_000] },
  ).toBe("accepted");
  if (acceptedOverview === null) throw new Error("real result acceptance was not observed");
  return acceptedOverview;
}

async function collectRealScenarioFacts(
  request: APIRequestContext,
  facts: {
    manifest: Record<string, unknown>;
    workspaceId: string;
    rootTaskId: string;
    marker: string;
    chain: RealChain;
    downstreamArtifactId: string;
  },
): Promise<RealScenarioFacts> {
  const integrationExecutionId = stringField(
    facts.chain.integrationExecution,
    "id",
    "real integration execution",
  );
  const integrationWorkerItemId = stringField(
    facts.chain.integrationExecution,
    "worker_item_id",
    "real integration execution",
  );
  const resultInputs = await readResultInputs(
    request,
    facts.workspaceId,
    integrationWorkerItemId,
  );
  const groups = arrayField(resultInputs, "groups", "real result inputs");
  const inputGroup = groups.find((group) => (
    typeof group.selection_id === "string"
    && Array.isArray(group.selected_sources)
    && group.selected_sources.length >= 2
  ));
  if (inputGroup === undefined) throw new Error("real integration result inputs were not selected");
  const selectionId = stringField(inputGroup, "selection_id", "real result input group");
  const selectionHash = stringField(inputGroup, "selection_hash", "real result input group");
  const selectedSources = arrayField(inputGroup, "selected_sources", "real result input group");
  const selectedArtifactHashes: [string, string][] = [];
  for (const source of selectedSources) {
    for (const artifact of arrayField(source, "artifacts", "real selected source")) {
      selectedArtifactHashes.push([
        stringField(artifact, "artifact_id", "real selected artifact"),
        stringField(artifact, "content_hash", "real selected artifact"),
      ]);
    }
  }
  if (selectedArtifactHashes.length === 0) throw new Error("real result inputs did not expose artifacts");
  const selectionResponse = await request.get(
    `/api/v1/workspaces/${encodeURIComponent(facts.workspaceId)}/collaboration/input-selections/${encodeURIComponent(selectionId)}`,
  );
  expect(selectionResponse.ok()).toBeTruthy();
  const selection = await objectJson(selectionResponse, "real input selection");
  expect(selection.selection_id).toBe(selectionId);
  expect(selection.workspace_id).toBe(facts.workspaceId);
  expect(selection.root_task_id).toBe(facts.rootTaskId);
  expect(selection.selection_hash).toBe(selectionHash);

  const contextPackage = await readExecutionContextPackage(
    request,
    facts.workspaceId,
    integrationExecutionId,
  );
  const contextPackageId = stringField(contextPackage, "package_id", "real context package");
  const contextPackageHash = stringField(contextPackage, "package_hash", "real context package");
  const resultOverviewResponse = await request.get(
    `/api/v1/workspaces/${encodeURIComponent(facts.workspaceId)}/tasks/${encodeURIComponent(facts.rootTaskId)}/results`,
  );
  expect(resultOverviewResponse.ok()).toBeTruthy();
  const resultOverview = await objectJson(resultOverviewResponse, "accepted real result overview");
  const candidate = recordField(resultOverview, "current_candidate", "accepted real result overview");
  const acceptance = recordField(resultOverview, "acceptance", "accepted real result overview");
  const candidateId = stringField(candidate, "candidate_id", "real result candidate");
  const candidateHash = stringField(candidate, "candidate_hash", "real result candidate");
  const candidateResultTree = stringField(candidate, "result_tree", "real result candidate");
  if (!/^[0-9a-f]{40}$/i.test(candidateResultTree)) {
    throw new Error("real result candidate did not expose a 40-character result tree");
  }
  const acceptanceId = stringField(acceptance, "acceptance_id", "real result acceptance");
  const acceptedCandidateId = stringField(acceptance, "candidate_id", "real result acceptance");
  const acceptedCandidateHash = stringField(acceptance, "candidate_hash", "real result acceptance");
  expect(acceptedCandidateId).toBe(candidateId);
  expect(acceptedCandidateHash).toBe(candidateHash);

  const downstreamArtifactId = facts.downstreamArtifactId;

  const sourceSessionIds: string[] = [];
  for (const execution of facts.chain.sourceExecutions) {
    sourceSessionIds.push(await readSessionId(request, facts.workspaceId, stringField(execution, "id", "real source execution")));
  }
  const integrationSessionId = await readSessionId(request, facts.workspaceId, integrationExecutionId);
  const budgetState = deriveBudgetState(facts.manifest);
  expect(budgetState).toBe("completed");
  const sessionState = facts.chain.sourceExecutions.every((execution) => execution.status === "succeeded")
    && facts.chain.integrationExecution.status === "succeeded"
    ? "succeeded"
    : "unknown";
  expect(sessionState).toBe("succeeded");
  return {
    workspaceId: facts.workspaceId,
    rootTaskId: facts.rootTaskId,
    sourceExecutionIds: facts.chain.sourceExecutions.map((execution) => stringField(execution, "id", "real source execution")),
    integrationExecutionId,
    sourceSessionIds,
    integrationSessionId,
    selectionId,
    selectionHash,
    bundleId: contextPackageId,
    contextPackageId,
    contextPackageHash,
    selectedArtifactHashes,
    downstreamArtifactId,
    downstreamMarker: facts.marker,
    candidateId,
    candidateHash,
    candidateSelectionHash: selectionHash,
    candidateResultTree,
    acceptanceId,
    acceptedCandidateId,
    acceptedCandidateHash,
    budgetState,
    sessionState,
  };
}

async function readResultInputs(
  request: APIRequestContext,
  workspaceId: string,
  workerItemId: string,
): Promise<Record<string, unknown>> {
  const response = await request.get(
    `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/worker-items/${encodeURIComponent(workerItemId)}/result-inputs`,
  );
  expect(response.ok()).toBeTruthy();
  return objectJson(response, "real result inputs");
}

async function readExecutionContextPackage(
  request: APIRequestContext,
  workspaceId: string,
  executionId: string,
): Promise<Record<string, unknown>> {
  const base = `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/executions/${encodeURIComponent(executionId)}/context-package`;
  for (const suffix of ["", "?manifest_schema_version=3", "?manifest_schema_version=2", "?manifest_schema_version=1"]) {
    const response = await request.get(`${base}${suffix}`);
    if (response.ok()) return objectJson(response, "real execution context package");
  }
  throw new Error("real integration execution did not expose a context package");
}

async function readSessionId(
  request: APIRequestContext,
  workspaceId: string,
  executionId: string,
): Promise<string> {
  const response = await request.get(
    `/api/v1/gateway/sessions?workspace_id=${encodeURIComponent(workspaceId)}&execution_id=${encodeURIComponent(executionId)}&limit=10`,
  );
  expect(response.ok()).toBeTruthy();
  const page = await objectJson(response, "real execution sessions");
  const sessions = arrayField(page, "items", "real execution sessions");
  const session = sessions.find((item) => item.execution_id === executionId);
  if (session === undefined) throw new Error("real execution did not expose a session");
  return stringField(session, "id", "real execution session");
}

async function readArtifactText(
  request: APIRequestContext,
  workspaceId: string,
  artifactId: string,
): Promise<string | null> {
  const prefix = `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/artifacts/${encodeURIComponent(artifactId)}`;
  const descriptorResponse = await request.get(`${prefix}/descriptor`);
  if (!descriptorResponse.ok()) return null;
  const descriptor = await objectJson(descriptorResponse, "real artifact descriptor");
  if (descriptor.content_available !== true) return null;
  const contentResponse = await request.get(`${prefix}/content`);
  if (!contentResponse.ok()) return null;
  return new TextDecoder().decode(await contentResponse.body());
}

async function requireCandidateArtifactWithMarker(
  request: APIRequestContext,
  workspaceId: string,
  resultOverview: Record<string, unknown>,
  marker: string,
): Promise<string> {
  const candidate = recordField(resultOverview, "current_candidate", "real result candidate");
  const artifacts = arrayField(candidate, "artifacts", "real result candidate artifacts");
  for (const artifact of artifacts) {
    const artifactId = stringField(artifact, "artifact_id", "real result artifact");
    const body = await readArtifactText(request, workspaceId, artifactId);
    if (body?.includes(marker)) return artifactId;
  }
  throw new Error("real integration artifact is missing the requested marker before acceptance");
}

function buildResultScenarioSnapshot(facts: RealScenarioFacts): Record<string, unknown> {
  return {
    root_task_id: facts.rootTaskId,
    selection_id: facts.selectionId,
    selection_hash: facts.selectionHash,
    bundle_id: facts.bundleId,
    context_package_id: facts.contextPackageId,
    context_package_hash: facts.contextPackageHash,
    selected_artifact_hashes: facts.selectedArtifactHashes,
    source_execution_ids: facts.sourceExecutionIds,
    downstream_execution_id: facts.integrationExecutionId,
    downstream_artifact_id: facts.downstreamArtifactId,
    downstream_marker: facts.downstreamMarker,
    real_session_ids: [...facts.sourceSessionIds, facts.integrationSessionId],
    candidate_id: facts.candidateId,
    candidate_hash: facts.candidateHash,
    acceptance_id: facts.acceptanceId,
    accepted_candidate_id: facts.acceptedCandidateId,
    accepted_candidate_hash: facts.acceptedCandidateHash,
    budget_state: facts.budgetState,
    session_state: facts.sessionState,
  };
}

function buildUserFlowSnapshot(
  manifest: Record<string, unknown>,
  facts: RealScenarioFacts,
): Record<string, unknown> {
  // This records browser-observed identities; the Python reporter re-reads verification and restore facts.
  return {
    run_id: stringField(manifest, "run_id", "real manifest"),
    workspace_id: facts.workspaceId,
    root_task_id: facts.rootTaskId,
    source_execution_ids: facts.sourceExecutionIds,
    source_session_ids: facts.sourceSessionIds,
    integration_execution_id: facts.integrationExecutionId,
    integration_session_id: facts.integrationSessionId,
    input_selection_id: facts.selectionId,
    input_selection_hash: facts.selectionHash,
    candidate_id: facts.candidateId,
    candidate_hash: facts.candidateHash,
    candidate_selection_hash: facts.candidateSelectionHash,
    verification_id: null,
    verification_candidate_id: null,
    verification_candidate_hash: null,
    verification_status: "unknown",
    acceptance_id: facts.acceptanceId,
    accepted_candidate_id: facts.acceptedCandidateId,
    accepted_candidate_hash: facts.acceptedCandidateHash,
    accepted: true,
    accepted_tree_hash: facts.candidateResultTree,
    restored_tree_hash: null,
    restore_check_passed: false,
    dependency_satisfied: false,
    source_windows: [],
  };
}

function recordScenarioObservation(
  manifest: Record<string, unknown>,
  scenario: "result-inputs" | "result-integration" | "user-flow",
  snapshot: Record<string, unknown>,
): void {
  recordAtomicObservation(manifest, scenario, {
    schema_version: 1,
    run_id: stringField(manifest, "run_id", "real manifest"),
    workspace_id: stringField(manifest, "workspace_id", "real manifest"),
    implementation_fingerprint: stringField(
      manifest,
      "implementation_fingerprint",
      "real manifest",
    ),
    runtime_fingerprint: stringField(manifest, "runtime_fingerprint", "real manifest"),
    scenario,
    snapshot,
  });
}

function recordAtomicObservation(
  manifest: Record<string, unknown>,
  name: string,
  payload: Record<string, unknown>,
): void {
  const runRoot = stringField(manifest, "run_root", "real manifest");
  if (!runRoot.startsWith("/")) throw new Error("real manifest run_root must be absolute");
  const observationPath = resolve(runRoot, "observations", `${name}.json`);
  mkdirSync(dirname(observationPath), { recursive: true, mode: 0o700 });
  chmodSync(dirname(observationPath), 0o700);
  const temporaryPath = `${observationPath}.${process.pid}.${Date.now()}.tmp`;
  writeFileSync(temporaryPath, `${JSON.stringify(payload, null, 2)}\n`, {
    encoding: "utf8",
    mode: 0o600,
  });
  chmodSync(temporaryPath, 0o600);
  renameSync(temporaryPath, observationPath);
}

function deriveBudgetState(
  manifest: Record<string, unknown>,
): "completed" | "reserved" | "exhausted" | "unknown" {
  const runRoot = stringField(manifest, "run_root", "real manifest");
  try {
    const raw = JSON.parse(readFileSync(resolve(runRoot, "acceptance-state.json"), "utf8")) as unknown;
    if (typeof raw !== "object" || raw === null || Array.isArray(raw)) return "unknown";
    const state = raw as Record<string, unknown>;
    const operationsValue = state.operations;
    if (typeof operationsValue !== "object" || operationsValue === null || Array.isArray(operationsValue)) {
      return "unknown";
    }
    const operations = Object.values(operationsValue as Record<string, unknown>).filter(
      (operation): operation is Record<string, unknown> => (
        typeof operation === "object" && operation !== null && !Array.isArray(operation)
      ),
    );
    const groupA = operations.filter((operation) => (
      operation.group === "A"
      || operation.acceptance_group === "A"
      || stringValue(operation.scenario).toLowerCase() === "user-flow"
      || stringValue(operation.scenario).toLowerCase() === "profile"
    ));
    const relevant = groupA.length > 0 ? groupA : operations;
    if (relevant.length === 0) return "unknown";
    const states = relevant.map((operation) => stringValue(operation.state || operation.status));
    if (states.every((value) => value === "completed")) return "completed";
    if (states.some((value) => value === "failed" || value === "unknown")) return "exhausted";
    return "reserved";
  } catch {
    return "unknown";
  }
}

function isIntegrationPurpose(purpose: string): boolean {
  const normalized = purpose.toLowerCase();
  return normalized.includes("summary.json") || normalized.includes("integration");
}

function executionStatusSummary(
  sources: Record<string, unknown>[],
  integration: Record<string, unknown> | undefined,
): string {
  const sourceSummary = sources
    .map((execution) => `${stringValue(execution.id)}:${stringValue(execution.status)}:${safeRealExecutionFailureCode(execution.failure_code)}`)
    .join(",");
  return `waiting:sources=${sourceSummary || "missing"};integration=${integration === undefined ? "missing" : `${stringValue(integration.id)}:${stringValue(integration.status)}:${safeRealExecutionFailureCode(integration.failure_code)}`}`;
}

function dateValue(value: unknown, label: string): number {
  if (typeof value !== "string") throw new Error(`${label} was missing`);
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) throw new Error(`${label} was invalid`);
  return timestamp;
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
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

function recordField(value: Record<string, unknown>, key: string, label: string): Record<string, unknown> {
  const candidate = value[key];
  if (typeof candidate !== "object" || candidate === null || Array.isArray(candidate)) {
    throw new Error(`${label} did not include object field ${key}`);
  }
  return candidate as Record<string, unknown>;
}

function arrayField(
  value: Record<string, unknown>,
  key: string,
  label: string,
): Record<string, unknown>[] {
  const candidate = value[key];
  if (
    !Array.isArray(candidate)
    || candidate.some((item) => (
      typeof item !== "object" || item === null || Array.isArray(item)
    ))
  ) {
    throw new Error(`${label} did not include object array field ${key}`);
  }
  return candidate as Record<string, unknown>[];
}

async function arrayJson(
  response: Awaited<ReturnType<APIRequestContext["get"]>>,
  label: string,
): Promise<Record<string, unknown>[]> {
  const value: unknown = await response.json();
  if (
    !Array.isArray(value)
    || value.some((item) => typeof item !== "object" || item === null || Array.isArray(item))
  ) {
    throw new Error(`${label} response was not an object array`);
  }
  return value as Record<string, unknown>[];
}

function stringField(value: Record<string, unknown>, key: string, label: string): string {
  const candidate = value[key];
  if (typeof candidate !== "string" || candidate.trim() === "") {
    throw new Error(`${label} did not include string field ${key}`);
  }
  return candidate;
}

function findUuidField(value: unknown, key: string): string | null {
  if (typeof value !== "object" || value === null) return null;
  if (Array.isArray(value)) {
    for (const item of value) {
      const found = findUuidField(item, key);
      if (found !== null) return found;
    }
    return null;
  }
  const record = value as Record<string, unknown>;
  const direct = record[key];
  if (typeof direct === "string" && /^[0-9a-f-]{36}$/i.test(direct)) return direct;
  for (const nested of Object.values(record)) {
    const found = findUuidField(nested, key);
    if (found !== null) return found;
  }
  return null;
}

function lastPathSegment(value: string): string {
  const segment = new URL(value).pathname.split("/").filter(Boolean).at(-1);
  if (segment === undefined || !/^[0-9a-f-]{36}$/i.test(segment)) {
    throw new Error("URL did not expose a UUID identity");
  }
  return segment;
}
