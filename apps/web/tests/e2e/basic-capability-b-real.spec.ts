import {
  chmodSync,
  existsSync,
  mkdirSync,
  readFileSync,
  renameSync,
  writeFileSync,
} from "node:fs";
import { randomUUID } from "node:crypto";
import { dirname, resolve } from "node:path";

import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

import {
  candidateHistoryItems,
  classifyPlanTransitionStatus,
  findExactExecutionInput,
  findPostInputTurnCompletionSequence,
  isIntegrationDispatchStalled,
  isWorkerWorkspaceTerminalForIntervention,
  isWritableWorkerWorkspaceSnapshot,
  taskGraphSubtaskKeys,
  unaffectedSourceResultIds,
} from "../../src/e2e/basicCapabilityBEvidence";
import {
  decodedLastPathSegment,
  realBasicScenarioDeadlineMs,
  realBasicBrowserTimeoutMs,
  requireRealAcceptanceGroup,
  type RealBasicManifest,
} from "../../src/e2e/basicCapabilityRealBoundary";
import {
  assertClosureSampleIntegrationSpec,
  assertClosureSampleProposal,
  closureSampleWorkerItemSourceKey,
} from "../../src/e2e/closureSampleContract";
import { waitForProposalOrPlanningFailure } from "./support/planning-proposal-gate";
import { revisionResultLink } from "./support/revision-result-link";

const REAL_RUN = process.env.SIDERA_RUN_REAL_BASIC === "1";
const SECRET_PATTERN = /(?:authorization|token|api[_-]?key|secret|credential)\s*[:=]/i;
const INPUT_DELIVERY_FAILURES = new Set(["blocked", "failed", "cancelled", "superseded"]);

type JsonRecord = Record<string, unknown>;

type WorkerItem = {
  id: string;
  stableKey: string;
  title: string;
  responsibility: string;
  subtaskKey: string;
  subtaskTitle: string;
};

type WorkerLiveFacts = {
  workspace: JsonRecord;
  context: JsonRecord;
  runtime: JsonRecord;
};

type CandidateFacts = {
  overview: JsonRecord;
  candidate: JsonRecord;
  detail: JsonRecord;
  sourceResults: JsonRecord[];
};

test.describe("R6 basic real B intervention and revision journey", () => {
  test("intervenes in one WorkerItem, repairs its failed result, then revises the plan in the same Main Agent thread", async ({
    page,
    request,
  }) => {
    test.skip(!REAL_RUN, "SIDERA_RUN_REAL_BASIC is not enabled");
    test.skip(
      process.env.SIDERA_BASIC_ACCEPTANCE_GROUP !== "B",
      "B journey runs only when SIDERA_BASIC_ACCEPTANCE_GROUP=B",
    );

    const group = requireRealAcceptanceGroup(process.env.SIDERA_BASIC_ACCEPTANCE_GROUP, "B");
    const workspaceId = required("SIDERA_BASIC_WORKSPACE_ID");
    const gatewayUrl = required("SIDERA_MAIN_AGENT_GATEWAY_URL").replace(/\/$/, "");
    const manifest = readRealManifest();
    expect(manifest.workspace_id).toBe(workspaceId);
    expect(process.env.SIDERA_BASIC_ACCEPTANCE_GROUP).toBe(group);
    test.setTimeout(realBasicBrowserTimeoutMs(manifest));
    const deadline = Date.now() + realBasicScenarioDeadlineMs(manifest);
    const remainingBudget = () => Math.max(1, deadline - Date.now());
    for (const name of ["intervention", "revision", "group-B"]) {
      if (existsSync(resolve(manifest.run_root, "observations", `${name}.json`))) {
        throw new Error(`real_observation_already_exists:${name}`);
      }
    }

    const groupA = readJson(resolve(manifest.run_root, "observations", "group-A.json"));
    expect(stringField(groupA, "run_id", "group A observation")).toBe(manifest.run_id);
    expect(stringField(groupA, "workspace_id", "group A observation")).toBe(workspaceId);
    expect(stringField(groupA, "implementation_fingerprint", "group A observation"))
      .toBe(manifest.implementation_fingerprint);
    expect(stringField(groupA, "runtime_fingerprint", "group A observation"))
      .toBe(manifest.runtime_fingerprint);

    const browserViolations: string[] = [];
    page.on("request", (browserRequest) => {
      const headers = Object.keys(browserRequest.headers()).join(" ");
      const body = browserRequest.postData() ?? "";
      if (SECRET_PATTERN.test(headers) || SECRET_PATTERN.test(body)) {
        browserViolations.push("credential-like field crossed the browser boundary");
      }
    });

    const health = await request.get(`${gatewayUrl}/main-agent/v1/health`);
    expect(health.ok()).toBeTruthy();
    expect((await health.json() as JsonRecord).provider).toBe("available");

    const marker = `SIDERA-R6-B-${Date.now().toString(36)}`;
    const created = await createTaskThroughMainAgent(page, request, {
      workspaceId,
      marker,
      remainingBudget,
    });
    const rootTaskId = created.rootTaskId;
    expect(rootTaskId).not.toBe(stringField(groupA, "root_task_id", "group A observation"));

    const oldPlan = await readActivePlan(request, workspaceId, rootTaskId);
    const initialWorkerItems = await readWorkerItems(request, workspaceId, rootTaskId);
    const target = initialWorkerItems.find((item) => closureSampleWorkerItemSourceKey(item) === "source-a");
    expect(target, "confirmed plan must contain the exact source-a WorkerItem").toBeDefined();
    expect(initialWorkerItems.some((item) => item.id !== target?.id)).toBeTruthy();
    const sourceWorkers = initialWorkerItems.filter((item) => closureSampleWorkerItemSourceKey(item) !== null);
    const integrationWorker = initialWorkerItems.find((item) => closureSampleWorkerItemSourceKey(item) === null);
    expect(sourceWorkers, "confirmed plan must contain both source WorkerItems").toHaveLength(2);
    expect(integrationWorker, "confirmed plan must contain one integration WorkerItem").toBeDefined();
    const selected = target!;

    await page.goto(`/workspaces/${workspaceId}/worker-items/${selected.id}`);
    await expect(page).toHaveURL(new RegExp(`/workspaces/${escapeRegExp(workspaceId)}/worker-items/${selected.id}$`));
    await expect(page.locator("#worker-item-title")).toHaveText(selected.title);

    const live = await waitForLiveWorker(request, workspaceId, selected.id, remainingBudget());
    expect(stringField(live.context, "worker_item_id", "intervention context")).toBe(selected.id);
    expect(numberField(live.context, "plan_generation", "intervention context"))
      .toBe(numberField(oldPlan, "active_generation", "active plan"));
    const executionId = stringField(live.context, "execution_id", "intervention context");
    const sessionId = stringField(live.context, "session_id", "intervention context");
    const generationBefore = numberField(live.context, "plan_generation", "intervention context");
    const siblingWorkerItemIds = initialWorkerItems
      .filter((item) => item.id !== selected.id)
      .map((item) => item.id)
      .sort();
    const siblingInputsBefore = await readSiblingInputIds(
      request,
      workspaceId,
      initialWorkerItems.filter((item) => item.id !== selected.id),
    );
    const interventionText = `局部介入 ${marker}：保留 source-a.json 中的 first_round 字段，不要自行修复；只在该文件增加 intervention_marker=${marker}，不要创建或修改其他文件。`;
    const traceBefore = await readTrace(request, workspaceId, rootTaskId, executionId, sessionId);
    const sequenceBeforeInput = Math.max(
      numberFieldOr(traceBefore, "checkpoint_sequence", 0),
      ...arrayField(traceBefore, "events", "worker trace").map((event) => numberField(event, "sequence", "trace event")),
    );

    const latestWorkspace = await getJson(
      request,
      `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/worker-items/${encodeURIComponent(selected.id)}/workspace`,
      "selected WorkerItem workspace immediately before intervention",
    );
    expect(
      isWritableWorkerWorkspaceSnapshot(latestWorkspace),
      "the same WorkerItem Session must still be active and writable before Web intervention",
    ).toBe(true);
    expect(recordField(latestWorkspace, "intervention_context", "latest WorkerItem workspace"))
      .toMatchObject(live.context);

    const sendResponsePromise = page.waitForResponse((response) => (
      response.request().method() === "POST"
      && new URL(response.url()).pathname === "/api/v1/commands"
      && commandType(response.request()) === "SendExecutionInput"
    ));
    const composer = page.getByRole("textbox", { name: "介入这个 Worker" });
    await expect(composer).toBeVisible();
    await expect(composer).toBeEnabled({ timeout: Math.min(remainingBudget(), 5_000) });
    await composer.fill(interventionText);
    const sendButton = page.getByRole("button", { name: "发送到当前 Session" });
    await expect(sendButton).toBeEnabled();
    await sendButton.click();
    const sendResponse = await sendResponsePromise;
    expect(sendResponse.ok()).toBeTruthy();
    const inputCommandBody = await requestBody(sendResponse.request());
    const inputCommandResponse = await objectJson(sendResponse, "SendExecutionInput command");
    const inputCommandId = stringField(inputCommandBody, "command_id", "SendExecutionInput command");
    expect(stringField(inputCommandResponse, "command_id", "SendExecutionInput receipt")).toBe(inputCommandId);
    expect(inputCommandBody.target_type).toBe("execution_session");
    expect(inputCommandBody.target_id).toBe(sessionId);
    const commandPayload = recordField(inputCommandBody, "payload", "SendExecutionInput command");
    const inputIdempotencyKey = stringField(commandPayload, "client_idempotency_key", "SendExecutionInput payload");
    expect(commandPayload.text).toBe(interventionText);
    expect(commandPayload.context).toMatchObject(live.context);
    await expect(page.getByText(interventionText, { exact: true })).toBeVisible();

    const commandStatus = await getJson(
      request,
      `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/commands/${encodeURIComponent(inputCommandId)}`,
      "SendExecutionInput command status",
    );
    expect(commandStatus.status).toBe("applied");

    const deliveredInput = await waitForObserved(
      async () => {
        const pageValue = await getJson(
          request,
          `/api/v1/gateway/inputs?workspace_id=${encodeURIComponent(workspaceId)}&session_id=${encodeURIComponent(sessionId)}&limit=100`,
          "execution inputs",
        );
        const input = findExactExecutionInput(arrayField(pageValue, "items", "execution inputs"), {
          clientIdempotencyKey: inputIdempotencyKey,
          executionId,
          sessionId,
        });
        if (input === null) return null;
        const status = stringField(input, "status", "execution input");
        if (INPUT_DELIVERY_FAILURES.has(status)) {
          throw new Error(`worker_input_delivery_failed:${status}`);
        }
        return status === "delivered" ? input : null;
      },
      remainingBudget(),
      "exact Worker input delivery acknowledgement",
    );
    const inputId = stringField(deliveredInput, "id", "delivered execution input");
    const inputOperationId = nullableString(deliveredInput.operation_id);
    expect(inputOperationId, "delivered input must have a persisted dispatch operation receipt").toBeTruthy();

    const postInputTraceSequence = await waitForObserved(
      async () => findPostInputTurnCompletionSequence(
        arrayField(
          await readTrace(request, workspaceId, rootTaskId, executionId, sessionId),
          "events",
          "worker trace",
        ),
        sequenceBeforeInput,
      ),
      remainingBudget(),
      "post-input Worker turn completion",
    );

    const failedCandidate = await waitForFailedCandidate(
      request,
      workspaceId,
      rootTaskId,
      remainingBudget(),
      { sourceWorkers, integrationWorker: integrationWorker! },
    );
    const interventionSourceResult = failedCandidate.sourceResults.find(
      (source) => source.worker_item_id === selected.id,
    );
    expect(interventionSourceResult, "failed candidate must preserve the intervened WorkerItem result")
      .toBeDefined();
    const outputArtifactId = arrayField(
      interventionSourceResult!,
      "artifact_ids",
      "intervention source result",
    ).find((value): value is string => typeof value === "string");
    expect(outputArtifactId, "persisted WorkerItem result must include an inspectable output artifact")
      .toBeTruthy();
    const outputDescriptor = await getJson(
      request,
      `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/artifacts/${encodeURIComponent(outputArtifactId!)}/descriptor`,
      "intervention output artifact descriptor",
    );
    expect(booleanField(outputDescriptor, "content_available", "intervention output artifact descriptor"))
      .toBeTruthy();
    const outputHash = stringField(outputDescriptor, "content_hash", "intervention output artifact descriptor");
    expect(outputHash).toMatch(/^[0-9a-f]{64}$/);

    expect(failedCandidate.sourceResults.some((source) => source.worker_item_id === selected.id)).toBeTruthy();
    const failedVerification = failedCandidate.detail.verifications instanceof Array
      ? failedCandidate.detail.verifications.find((item) => isRecord(item) && item.status === "failed")
      : undefined;
    expect(failedVerification, "result candidate must expose a persisted failed verification").toBeDefined();
    const failedCheckId = stringField(failedVerification!, "verification_id", "failed verification");
    const failedCheckCandidateId = stringField(failedVerification!, "candidate_id", "failed verification");
    const failedCheckKey = stringField(failedVerification!, "check_key", "failed verification");
    expect(failedCheckCandidateId).toBe(stringField(failedCandidate.candidate, "candidate_id", "failed candidate"));
    const failedCriterion = failedCandidate.overview.criteria instanceof Array
      ? failedCandidate.overview.criteria.find((item) => isRecord(item)
        && item.observed_status === "failed"
        && Array.isArray(item.check_keys)
        && item.check_keys.includes(failedCheckKey))
      : undefined;
    expect(failedCriterion, "failed verification must belong to an observed failed criterion").toBeDefined();
    const failedCriterionKey = stringField(failedCriterion!, "criterion_key", "failed criterion");

    const resultOperation = page.getByRole("button", { name: "局部返工" });
    await expect(resultOperation).toBeVisible({ timeout: remainingBudget() });
    await resultOperation.click();
    const reworkForm = page.getByRole("form", { name: "局部返工" });
    await expect(reworkForm).toBeVisible();
    const reworkCommandPromise = page.waitForResponse((response) => (
      response.request().method() === "POST"
      && new URL(response.url()).pathname === "/api/v1/commands"
      && commandType(response.request()) === "RequestTaskRework"
    ));
    await reworkForm.getByLabel("返工说明").fill(
      `只修复 WorkerItem ${selected.id} 的失败检查 ${failedCheckKey}：将 out/source-a.json 精确恢复为 closure-sample-v1 基准对象，删除 first_round 与 intervention_marker 两个额外字段；不要改 source-b.json 或 summary.json。`,
    );
    const reworkStartedAt = Date.now();
    await reworkForm.getByRole("button", { name: "提交局部返工" }).click();
    const reworkResponse = await reworkCommandPromise;
    expect(reworkResponse.ok()).toBeTruthy();
    const reworkCommandBody = await requestBody(reworkResponse.request());
    const reworkCommandId = stringField(reworkCommandBody, "command_id", "RequestTaskRework command");
    const reworkPayload = recordField(reworkCommandBody, "payload", "RequestTaskRework command");
    expect(reworkPayload.candidate_id).toBe(stringField(failedCandidate.candidate, "candidate_id", "failed candidate"));
    expect(reworkPayload.candidate_hash).toBe(stringField(failedCandidate.candidate, "candidate_hash", "failed candidate"));
    expect(reworkPayload.issue_keys).toContain(failedCriterionKey);
    expect(reworkPayload.target_worker_item_ids).toEqual([selected.id]);
    expect(reworkPayload.impact_worker_item_ids).toEqual([selected.id]);
    const reworkCommandReceipt = await objectJson(reworkResponse, "RequestTaskRework command");
    expect(stringField(reworkCommandReceipt, "command_id", "RequestTaskRework receipt")).toBe(reworkCommandId);

    const reworkRecord = await waitForReworkRecord(
      request,
      workspaceId,
      rootTaskId,
      stringField(failedCandidate.candidate, "candidate_id", "failed candidate"),
      selected.id,
      remainingBudget(),
    );
    const reworkMode = stringField(reworkRecord, "mode", "rework record");
    expect(reworkMode).toBe("user");
    expect(reworkRecord).toHaveProperty("round_number", null);

    const reintegrateAction = page.getByRole("button", { name: "重新集成", exact: true });
    await expect(reintegrateAction).toBeVisible({ timeout: remainingBudget() });
    await reintegrateAction.click();
    const reintegrateForm = page.getByRole("form", { name: "重新集成", exact: true });
    const reintegrateCommandPromise = page.waitForResponse((response) => (
      response.request().method() === "POST"
      && new URL(response.url()).pathname === "/api/v1/commands"
      && commandType(response.request()) === "ReintegrateTaskResult"
    ));
    await reintegrateForm.getByLabel("操作原因", { exact: true }).fill(
      "将局部返工后的 source-a 与未修改的 sibling 结果重新集成",
    );
    await reintegrateForm.getByRole("button", { name: "提交重新集成", exact: true }).click();
    const reintegrateResponse = await reintegrateCommandPromise;
    expect(reintegrateResponse.ok()).toBeTruthy();
    const reintegrateBody = await requestBody(reintegrateResponse.request());
    const reintegrateCommandId = stringField(
      reintegrateBody,
      "command_id",
      "ReintegrateTaskResult command",
    );
    const reintegratePayload = recordField(
      reintegrateBody,
      "payload",
      "ReintegrateTaskResult command",
    );
    expect(reintegratePayload.source_candidate_id).toBe(
      stringField(failedCandidate.candidate, "candidate_id", "failed candidate"),
    );
    expect(reintegratePayload.source_candidate_hash).toBe(
      stringField(failedCandidate.candidate, "candidate_hash", "failed candidate"),
    );
    expect(reintegratePayload.expected_result_version).toBe(
      numberField(failedCandidate.overview, "result_version", "failed result overview"),
    );
    const reintegratedResultIds = arrayField(
      reintegratePayload,
      "replacement_result_ids",
      "ReintegrateTaskResult command",
    );
    expect(reintegratedResultIds).toHaveLength(1);
    const reintegrateReceipt = await objectJson(reintegrateResponse, "ReintegrateTaskResult receipt");
    expect(stringField(reintegrateReceipt, "command_id", "ReintegrateTaskResult receipt"))
      .toBe(reintegrateCommandId);

    const replacementCandidate = await waitForReplacementCandidate(
      request,
      workspaceId,
      rootTaskId,
      stringField(failedCandidate.candidate, "candidate_id", "failed candidate"),
      remainingBudget(),
    );
    const reworkSource = replacementCandidate.sourceResults.find((source) => source.worker_item_id === selected.id);
    expect(reworkSource, "replacement candidate must have a source result for the selected WorkerItem").toBeDefined();
    const reworkExecutionId = stringField(reworkSource!, "execution_id", "replacement source result");
    const selectedWorkspaceAfterRework = await waitForExecutionSession(
      request,
      workspaceId,
      selected.id,
      reworkExecutionId,
      remainingBudget(),
    );
    const reworkSessionId = selectedWorkspaceAfterRework.sessionId;
    const reworkGeneration = numberField(
      recordField(selectedWorkspaceAfterRework.workspace, "current_placement", "WorkerItem workspace"),
      "plan_generation",
      "WorkerItem placement",
    );
    expect(reworkGeneration).toBe(generationBefore);

    const siblingInputsAfter = await readSiblingInputIds(
      request,
      workspaceId,
      initialWorkerItems.filter((item) => item.id !== selected.id),
    );
    const unaffectedBefore = unaffectedSourceResultIds(failedCandidate.sourceResults, selected.id);
    const unaffectedAfter = unaffectedSourceResultIds(replacementCandidate.sourceResults, selected.id);
    const reworkElapsedSeconds = Math.max(0, Math.floor((Date.now() - reworkStartedAt) / 1_000));

    const interventionSnapshot = {
      schema_version: 1,
      run_id: manifest.run_id,
      root_task_id: rootTaskId,
      worker_item_id: selected.id,
      sibling_worker_item_ids: siblingWorkerItemIds,
      plan_generation_before: generationBefore,
      plan_generation_after: numberField(
        await readActivePlan(request, workspaceId, rootTaskId),
        "active_generation",
        "active plan after local intervention",
      ),
      input_command_id: inputCommandId,
      input_id: inputId,
      input_target_worker_item_id: stringField(
        recordField(commandPayload, "context", "SendExecutionInput payload"),
        "worker_item_id",
        "SendExecutionInput context",
      ),
      input_status: stringField(deliveredInput, "status", "delivered execution input"),
      adapter_acknowledged: inputOperationId !== null,
      post_input_trace_sequence: postInputTraceSequence,
      observed_output_id: outputArtifactId,
      observed_output_hash: outputHash,
      sibling_input_ids_before: siblingInputsBefore,
      sibling_input_ids_after: siblingInputsAfter,
      source_candidate_id: stringField(failedCandidate.candidate, "candidate_id", "failed candidate"),
      source_candidate_hash: stringField(failedCandidate.candidate, "candidate_hash", "failed candidate"),
      failed_check_id: failedCheckId,
      failed_check_candidate_id: failedCheckCandidateId,
      failed_check_candidate_hash: stringField(failedCandidate.candidate, "candidate_hash", "failed candidate"),
      rework_record_id: stringField(reworkRecord, "rework_id", "rework record"),
      rework_execution_id: reworkExecutionId,
      rework_session_id: reworkSessionId,
      rework_worker_item_id: oneWorkerId(reworkRecord.target_worker_item_ids, selected.id, "rework record target"),
      rework_generation: reworkGeneration,
      replacement_candidate_id: stringField(replacementCandidate.candidate, "candidate_id", "replacement candidate"),
      replacement_candidate_hash: stringField(replacementCandidate.candidate, "candidate_hash", "replacement candidate"),
      rework_mode: reworkMode,
      rework_round: reworkRecord.round_number,
      rework_round_limit: null,
      rework_elapsed_seconds: reworkElapsedSeconds,
      rework_time_limit_seconds: null,
      unaffected_result_ids_before: unaffectedBefore,
      unaffected_result_ids_after: unaffectedAfter,
    };
    expect(interventionSnapshot.plan_generation_after).toBe(generationBefore);
    expect(siblingInputsAfter).toEqual(siblingInputsBefore);
    expect(unaffectedAfter).toEqual(unaffectedBefore);
    recordScenarioObservation(manifest, "intervention", interventionSnapshot);

    const preRevisionWorkspace = await getJson(
      request,
      `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/worker-items/${encodeURIComponent(selected.id)}/workspace`,
      "WorkerItem workspace before revision",
    );
    const oldInterventionContext = recordField(preRevisionWorkspace, "intervention_context", "WorkerItem workspace before revision");
    const oldGeneration = numberField(oldPlan, "active_generation", "active plan before revision");
    const oldPlanId = stringField(oldPlan, "active_snapshot_id", "active plan before revision");
    const oldPlanHash = stringField(oldPlan, "active_snapshot_hash", "active plan before revision");
    const oldWorkers = await readWorkerItems(request, workspaceId, rootTaskId);
    const beforeRevisionCandidate = await readCandidateById(
      request,
      workspaceId,
      rootTaskId,
      stringField(replacementCandidate.candidate, "candidate_id", "replacement candidate"),
    );
    const beforeRevisionSourceIds = beforeRevisionCandidate.sourceResults.map((item) => stringField(item, "snapshot_id", "source result"));
    const mainAgentTab = page.getByRole("navigation", { name: "已打开的任务" })
      .getByRole("link", { name: "Main Agent" });
    await expect(mainAgentTab).toBeVisible();
    await mainAgentTab.click();
    await expect(page).toHaveURL(new RegExp(
      `/workspaces/${escapeRegExp(workspaceId)}/chat/${escapeRegExp(encodeURIComponent(created.threadId))}$`,
    ));
    const mainComposer = page.getByRole("textbox", { name: "给 Main Agent 的消息" });
    await expect(mainComposer).toBeEnabled();
    await mainComposer.fill(
      `需求整体变更，任务 ${rootTaskId} 的唯一变更标记是 ${marker}-REV：新增独立的安全审查子任务和一个专属 WorkerItem，审查 source-a/source-b/integration 的边界与结果；保留所有未受影响 WorkerItem 的身份和上下文，只对受影响项生成必要 transition。请直接调用 sidera_create_plan_revision 创建待审阅的 Revision；不要调用 sidera_confirm_plan_revision，不要自行确认或执行新计划。`,
    );
    await page.getByRole("button", { name: "发送消息" }).click();
    const revisionLink = revisionResultLink(page, rootTaskId);
    await expect(revisionLink).toHaveCount(1, { timeout: remainingBudget() });
    const revisionHref = await revisionLink.getAttribute("href");
    expect(revisionHref).toBeTruthy();
    const revisionUrl = new URL(revisionHref!, page.url());
    const revisionMatch = revisionUrl.pathname.match(/\/tasks\/([^/]+)\/revisions\/([^/]+)$/);
    expect(revisionMatch?.[1]).toBe(rootTaskId);
    const planningRequestId = revisionMatch![2]!;

    const revisionToolCallId = await waitForRevisionToolCallId(
      request,
      gatewayUrl,
      created.threadId,
      remainingBudget(),
    );
    await revisionLink.click();
    await expect(page).toHaveURL(new RegExp(`/workspaces/${escapeRegExp(workspaceId)}/tasks/${rootTaskId}/revisions/${planningRequestId}$`));
    await waitForProposalOrPlanningFailure(page, remainingBudget());
    const revisionDiff = await getJson(
      request,
      `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/planning-requests/${encodeURIComponent(planningRequestId)}/diff`,
      "plan revision diff",
    );
    expect(stringField(revisionDiff, "root_task_id", "plan revision diff")).toBe(rootTaskId);
    expect(numberField(recordField(revisionDiff, "base", "plan revision diff"), "plan_generation", "revision base"))
      .toBe(oldGeneration);
    const revisionId = stringField(revisionDiff, "proposal_revision_id", "plan revision diff");
    const revisionHash = stringField(revisionDiff, "proposal_content_hash", "plan revision diff");

    await expect(page.getByRole("heading", { name: "需求理解与风险" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "任务层级变化" })).toBeVisible();
    const confirmRevision = page.getByRole("button", { name: "确认并启用新计划" });
    await expect(confirmRevision).toBeEnabled();
    const revisionConfirmationResponsePromise = page.waitForResponse((response) => (
      response.request().method() === "POST"
      && new URL(response.url()).pathname === "/api/v1/commands"
      && commandType(response.request()) === "ConfirmPlanRevision"
    ));
    await confirmRevision.click();
    const revisionConfirmationResponse = await revisionConfirmationResponsePromise;
    expect(revisionConfirmationResponse.ok()).toBeTruthy();
    const revisionConfirmationBody = await requestBody(revisionConfirmationResponse.request());
    const revisionConfirmationCommandId = stringField(revisionConfirmationBody, "command_id", "revision confirmation");
    const revisionConfirmationReceipt = await objectJson(revisionConfirmationResponse, "revision confirmation");
    expect(stringField(revisionConfirmationReceipt, "command_id", "revision confirmation receipt"))
      .toBe(revisionConfirmationCommandId);

    const newPlan = await waitForObserved(
      async () => {
        const current = await readActivePlan(request, workspaceId, rootTaskId);
        return numberField(current, "active_generation", "active plan") === oldGeneration + 1
          ? current
          : null;
      },
      remainingBudget(),
      "one-generation plan activation",
    );
    const newPlanId = stringField(newPlan, "active_snapshot_id", "new active plan");
    const newPlanHash = stringField(newPlan, "active_snapshot_hash", "new active plan");
    const oldGenerationRead = await getJson(
      request,
      `/api/v1/tasks/${encodeURIComponent(rootTaskId)}/plan/generations/${oldGeneration}?workspace_id=${encodeURIComponent(workspaceId)}`,
      "old plan generation",
    );
    const oldPlanStillQueryable = stringField(oldGenerationRead, "id", "old plan generation") === oldPlanId;
    const newWorkers = await waitForObserved(
      async () => {
        const values = await readWorkerItems(request, workspaceId, rootTaskId);
        return values.length > 0 ? values : null;
      },
      remainingBudget(),
      "revised WorkerItem identities",
    );
    const transition = await waitForPlanTransition(
      request,
      workspaceId,
      rootTaskId,
      oldGeneration + 1,
      remainingBudget(),
    );
    const transitionDetail = await getJson(
      request,
      `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/plan-transitions/${encodeURIComponent(stringField(transition, "plan_transition_id", "plan transition"))}`,
      "plan transition detail",
    );
    const transitionWorkerIds = arrayField(transitionDetail, "workers", "plan transition")
      .filter(isRecord)
      .map((worker) => nullableRecord(worker.worker_item)?.id)
      .map(nullableString)
      .filter((value): value is string => value !== null)
      .sort();

    const staleInputRejected = await rejectInputFromOldPlan(
      request,
      workspaceId,
      selected.id,
      oldInterventionContext,
      oldGeneration + 1,
      marker,
      remainingBudget(),
    );
    const threadAfter = await getJson(
      request,
      `${gatewayUrl}/main-agent/v1/threads/${encodeURIComponent(created.threadId)}`,
      "Main Agent thread after revision",
    );
    const threadAfterId = stringField(threadAfter, "id", "Main Agent thread");
    const changedIds = deriveChangedWorkerIds(revisionDiff);
    const oldWorkerIds = oldWorkers.map((item) => item.id).sort();
    const newWorkerIds = newWorkers.map((item) => item.id).sort();
    const oldIdSet = new Set(oldWorkerIds);
    const newIdSet = new Set(newWorkerIds);
    const changedWorkerItemIds = [...changedIds].filter((id) => oldIdSet.has(id)).sort();
    const addedWorkerItemIds = newWorkerIds.filter((id) => !oldIdSet.has(id));
    expect(addedWorkerItemIds, "revision must add exactly its one requested review WorkerItem").toHaveLength(1);
    expect(changedWorkerItemIds, "revision must preserve every original WorkerItem unchanged").toEqual([]);
    const reusedWorkerItemIds = oldWorkerIds
      .filter((id) => newIdSet.has(id) && !changedWorkerItemIds.includes(id))
      .sort();
    const afterRevisionCandidate = await readCandidateById(
      request,
      workspaceId,
      rootTaskId,
      stringField(replacementCandidate.candidate, "candidate_id", "replacement candidate"),
    );
    const afterRevisionSourceIds = afterRevisionCandidate.sourceResults
      .map((item) => stringField(item, "snapshot_id", "source result"));
    const beforeSourceById = new Map(beforeRevisionCandidate.sourceResults.map((item) => [
      stringField(item, "snapshot_id", "source result"),
      stringField(item, "worker_item_id", "source result"),
    ]));
    const afterSourceById = new Map(afterRevisionCandidate.sourceResults.map((item) => [
      stringField(item, "snapshot_id", "source result"),
      stringField(item, "worker_item_id", "source result"),
    ]));
    const reusableIds = new Set(reusedWorkerItemIds);
    const unimpactedResultIds = [...beforeSourceById.keys()]
      .filter((id) => afterSourceById.has(id)
        && beforeSourceById.get(id) === afterSourceById.get(id)
        && reusableIds.has(beforeSourceById.get(id)!))
      .sort();
    const revisionSnapshot = {
      schema_version: 1,
      run_id: manifest.run_id,
      root_task_id: rootTaskId,
      main_agent_thread_id_before: created.threadId,
      main_agent_thread_id_after: threadAfterId,
      main_agent_tool_call_observed: revisionToolCallId.length > 0,
      tool_call_id: revisionToolCallId,
      old_plan_id: oldPlanId,
      old_plan_hash: oldPlanHash,
      old_generation: oldGeneration,
      new_plan_id: newPlanId,
      new_plan_hash: newPlanHash,
      new_generation: numberField(newPlan, "active_generation", "new active plan"),
      revision_id: revisionId,
      revision_hash: revisionHash,
      confirmation_command_id: revisionConfirmationCommandId,
      transition_id: stringField(transition, "plan_transition_id", "plan transition"),
      old_plan_still_queryable: oldPlanStillQueryable,
      reused_worker_item_ids: reusedWorkerItemIds,
      changed_worker_item_ids: changedWorkerItemIds,
      added_worker_item_ids: addedWorkerItemIds,
      old_worker_item_ids: oldWorkerIds,
      source_result_ids_before: beforeRevisionSourceIds,
      source_result_ids_after: afterRevisionSourceIds,
      unimpacted_result_ids: unimpactedResultIds,
      transition_worker_item_ids: transitionWorkerIds,
      old_inputs_rejected_for_new_plan: staleInputRejected,
      transition_status: classifyPlanTransitionStatus(transitionDetail),
    };
    expect(threadAfterId).toBe(created.threadId);
    expect(numberField(newPlan, "active_generation", "new active plan")).toBe(oldGeneration + 1);
    expect(oldPlanStillQueryable).toBeTruthy();
    expect(staleInputRejected).toBeTruthy();
    recordScenarioObservation(manifest, "revision", revisionSnapshot);
    recordGroupObservation(manifest, {
      rootTaskId,
      commandIds: [created.confirmationCommandId, revisionConfirmationCommandId],
      resourceIds: [
        `planning_request:${created.planningRequestId}`,
        `proposal:${created.proposalId}`,
        `revision:${planningRequestId}`,
        `root_task:${rootTaskId}`,
      ],
      sourceExecutionIds: [...new Set(replacementCandidate.sourceResults.map((item) => stringField(item, "execution_id", "source result")))],
      integrationExecutionId: stringField(replacementCandidate.candidate, "integration_execution_id", "replacement candidate"),
      resultCandidateId: stringField(replacementCandidate.candidate, "candidate_id", "replacement candidate"),
    });

    expect(browserViolations).toEqual([]);
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

async function createTaskThroughMainAgent(
  page: Page,
  request: APIRequestContext,
  input: { workspaceId: string; marker: string; remainingBudget: () => number },
): Promise<{
  threadId: string;
  rootTaskId: string;
  planningRequestId: string;
  proposalId: string;
  confirmationCommandId: string;
}> {
  const { workspaceId, marker } = input;
  await page.goto(`/workspaces/${workspaceId}/chat`);
  await expect(page.getByText("Gateway ready", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "新建对话" }).first().click();
  await expect(page).toHaveURL(new RegExp(`/workspaces/${escapeRegExp(workspaceId)}/chat/[^/?]+$`));
  const threadId = decodedLastPathSegment(page.url());
  const composer = page.getByRole("textbox", { name: "给 Main Agent 的消息" });
  await composer.fill(
    `请创建一个 closure-sample-v1 真实任务，唯一标记 ${marker}。任务最终要求严格满足基准：source-a 只能生成 out/source-a.json，内容为 `
    + '`{"marker":"SIDERA_CLOSURE_SOURCE_A_V1","items":[{"id":"api","status":"done"},{"id":"ui","status":"queued"}]}`'
    + "；source-b 只能生成 out/source-b.json，内容为 "
    + '`{"marker":"SIDERA_CLOSURE_SOURCE_B_V1","items":[{"id":"tests","status":"running"},{"id":"docs","status":"blocked"}]}`'
    + "；integration 只读取上述两个选定成果并生成 out/summary.json，严格包含 4 条记录及四种状态各一条。"
    + "必须拆成 implementation 与 integration 两个 Subtask，source-a/source-b 并行，integration 等待两者；integration 输入组只能要求 source-a/source-b 执行意图声明会产出的成果或证据，不能要求 integration 自己的 acceptance evidence 才启动；不要联网、安装依赖或修改其他文件。"
    + "source-a 第一轮必须保持 marker 和 items 与基准完全一致，但额外保留字段 first_round=needs-rework，供用户局部返工时删除。integration 必须生成且只生成 out/summary.json，内容精确为 "
    + '`{"total":4,"counts":{"done":1,"queued":1,"running":1,"blocked":1},"source_markers":["SIDERA_CLOSURE_SOURCE_A_V1","SIDERA_CLOSURE_SOURCE_B_V1"]}`'
    + "。integration_spec 必须选择系统提供的 closure 检查作为唯一 check，并将其关联到 automatic integration criterion；不要另造或修改检查命令。最终验收必须精确回到上述三个样例文件的基准内容。"
    + "信息充分，不要询问澄清。请直接调用 sidera_submit_brief 创建 Proposal，创建后等待我从 Web 审阅并确认，不要直接创建 Task。",
  );
  await page.getByRole("button", { name: "发送消息" }).click();
  const briefCard = page.locator(".tool-result-card").filter({ hasText: "sidera_submit_brief" });
  await expect(briefCard).toBeVisible({ timeout: input.remainingBudget() });
  const planningLink = briefCard.getByRole("link", { name: /查看对象/ });
  await expect(planningLink).toBeVisible();
  const planningHref = await planningLink.getAttribute("href");
  expect(planningHref).toBeTruthy();
  const planningMatch = new URL(planningHref!, page.url()).pathname.match(/\/planning\/([^/]+)$/);
  expect(planningMatch?.[1]).toBeTruthy();
  const planningRequestId = planningMatch![1]!;

  await planningLink.click();
  await expect(page.locator("#planning-request-title")).toHaveText("规划请求");
  await waitForProposalOrPlanningFailure(page, input.remainingBudget());
  const proposal = await readCurrentProposal(request, workspaceId, planningRequestId);
  const confirmResponsePromise = page.waitForResponse((response) => (
    response.request().method() === "POST"
    && new URL(response.url()).pathname === "/api/v1/commands"
  ));
  await page.getByRole("button", { name: "确认并创建任务" }).click();
  const confirmResponse = await confirmResponsePromise;
  expect(confirmResponse.ok()).toBeTruthy();
  const confirmBody = await requestBody(confirmResponse.request());
  const confirmationCommandId = stringField(confirmBody, "command_id", "initial plan confirmation");
  const confirmation = await objectJson(confirmResponse, "initial plan confirmation");
  expect(stringField(confirmation, "command_id", "initial plan confirmation receipt")).toBe(confirmationCommandId);
  await expect(page).toHaveURL(new RegExp(`/workspaces/${escapeRegExp(workspaceId)}/tasks/[0-9a-f-]{36}$`));
  const rootTaskId = decodedLastPathSegment(page.url());
  await expect(page.locator("#task-detail-title")).toBeVisible();
  const commandStatus = await getJson(
    request,
    `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/commands/${encodeURIComponent(confirmationCommandId)}`,
    "initial plan confirmation status",
  );
  expect(commandStatus.status).toBe("applied");
  const commandResult = recordField(commandStatus, "result", "initial plan confirmation status");
  const resultData = recordField(commandResult, "data", "initial plan confirmation result");
  expect(findUuidField(resultData, "root_task_id")).toBe(rootTaskId);
  return {
    threadId,
    rootTaskId,
    planningRequestId,
    proposalId: stringField(proposal, "id", "initial plan proposal"),
    confirmationCommandId,
  };
}

async function readCurrentProposal(
  request: APIRequestContext,
  workspaceId: string,
  planningRequestId: string,
): Promise<JsonRecord> {
  const detail = await getJson(
    request,
    `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/planning-requests/${encodeURIComponent(planningRequestId)}`,
    "initial planning request",
  );
  const revision = numberField(detail, "current_proposal_revision", "initial planning request");
  const proposal = await getJson(
    request,
    `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/planning-requests/${encodeURIComponent(planningRequestId)}/proposals/${revision}`,
    "initial planning proposal",
  );
  assertClosureSampleProposal(proposal);
  assertClosureSampleIntegrationSpec(proposal, "closure");
  return proposal;
}

async function readActivePlan(
  request: APIRequestContext,
  workspaceId: string,
  rootTaskId: string,
): Promise<JsonRecord> {
  return getJson(
    request,
    `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/tasks/${encodeURIComponent(rootTaskId)}/active-plan`,
    "active task plan",
  );
}

async function readWorkerItems(
  request: APIRequestContext,
  workspaceId: string,
  rootTaskId: string,
): Promise<WorkerItem[]> {
  const graph = await getJson(
    request,
    `/api/v1/tasks/${encodeURIComponent(rootTaskId)}/graph?workspace_id=${encodeURIComponent(workspaceId)}`,
    "task graph",
  );
  const values: WorkerItem[] = [];
  const subtaskKeys = taskGraphSubtaskKeys(arrayField(graph, "nodes", "task graph"));
  const subtasks = await Promise.all(subtaskKeys.map((subtaskKey) => getJson(
    request,
    `/api/v1/tasks/${encodeURIComponent(rootTaskId)}/subtasks/${encodeURIComponent(subtaskKey)}?workspace_id=${encodeURIComponent(workspaceId)}`,
    "subtask workspace",
  )));
  for (const [index, subtask] of subtasks.entries()) {
    const subtaskKey = subtaskKeys[index]!;
    const subtaskRecord = recordField(subtask, "subtask", "subtask workspace");
    const subtaskTitle = stringField(subtaskRecord, "title", "subtask workspace");
    for (const worker of arrayField(subtask, "worker_items", "subtask workspace").filter(isRecord)) {
      values.push({
        id: stringField(worker, "worker_item_id", "subtask worker item"),
        stableKey: stringField(worker, "stable_key", "subtask worker item"),
        title: stringField(worker, "title", "subtask worker item"),
        responsibility: typeof worker.responsibility === "string" ? worker.responsibility : "",
        subtaskKey,
        subtaskTitle,
      });
    }
  }
  return values;
}

async function waitForLiveWorker(
  request: APIRequestContext,
  workspaceId: string,
  workerItemId: string,
  timeoutMs: number,
): Promise<WorkerLiveFacts> {
  return waitForObserved(async () => {
    const workspace = await getJson(
      request,
      `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/worker-items/${encodeURIComponent(workerItemId)}/workspace`,
      "selected WorkerItem workspace",
    );
    if (!isWritableWorkerWorkspaceSnapshot(workspace)) {
      if (isWorkerWorkspaceTerminalForIntervention(workspace)) {
        throw new Error(
          `worker_session_unavailable_for_intervention:${String(workspace.read_only_reason ?? "terminal_runtime")}`,
        );
      }
      return null;
    }
    return {
      workspace,
      context: workspace.intervention_context,
      runtime: workspace.current_runtime,
    };
  }, timeoutMs, "live selected WorkerItem session");
}

async function readSiblingInputIds(
  request: APIRequestContext,
  workspaceId: string,
  siblings: readonly WorkerItem[],
): Promise<string[]> {
  const sessionIds = new Set<string>();
  for (const sibling of siblings) {
    const workspace = await getJson(
      request,
      `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/worker-items/${encodeURIComponent(sibling.id)}/workspace`,
      "sibling WorkerItem workspace",
    );
    const runtime = nullableRecord(workspace.current_runtime);
    if (runtime !== null) {
      const currentSessionId = nullableString(runtime.session_id);
      if (currentSessionId !== null) sessionIds.add(currentSessionId);
    }
    for (const execution of arrayField(workspace, "execution_history", "sibling WorkerItem workspace").filter(isRecord)) {
      const sessionId = nullableString(execution.session_id);
      if (sessionId !== null) sessionIds.add(sessionId);
    }
  }
  const inputIds = new Set<string>();
  for (const sessionId of [...sessionIds].sort()) {
    const page = await getJson(
      request,
      `/api/v1/gateway/inputs?workspace_id=${encodeURIComponent(workspaceId)}&session_id=${encodeURIComponent(sessionId)}&limit=100`,
      "sibling execution inputs",
    );
    for (const item of arrayField(page, "items", "sibling execution inputs").filter(isRecord)) {
      inputIds.add(stringField(item, "id", "sibling execution input"));
    }
  }
  return [...inputIds].sort();
}

async function readTrace(
  request: APIRequestContext,
  workspaceId: string,
  rootTaskId: string,
  executionId: string,
  sessionId: string,
): Promise<JsonRecord> {
  return getJson(
    request,
    `/api/v1/tasks/${encodeURIComponent(rootTaskId)}/executions/${encodeURIComponent(executionId)}/sessions/${encodeURIComponent(sessionId)}/trace?workspace_id=${encodeURIComponent(workspaceId)}&limit=100`,
    "Worker Trace",
  );
}

async function waitForFailedCandidate(
  request: APIRequestContext,
  workspaceId: string,
  rootTaskId: string,
  timeoutMs: number,
  dispatchWorkers: { sourceWorkers: readonly WorkerItem[]; integrationWorker: WorkerItem },
): Promise<CandidateFacts> {
  let dispatchStallStartedAt: number | null = null;
  let nextDispatchCheckAt = 0;
  return waitForObserved(async () => {
    const overview = await getJson(
      request,
      `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/tasks/${encodeURIComponent(rootTaskId)}/results`,
      "task result overview",
    );
    const candidate = nullableRecord(overview.current_candidate);
    if (candidate !== null) {
      const candidateId = stringField(candidate, "candidate_id", "current result candidate");
      const detailResponse = await getOptionalJson(
        request,
        `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/tasks/${encodeURIComponent(rootTaskId)}/results/${encodeURIComponent(candidateId)}`,
        "result candidate detail",
      );
      if (detailResponse !== null) {
        const verifications = arrayField(detailResponse, "verifications", "result candidate detail").filter(isRecord);
        const hasFailedVerification = verifications.some((item) => item.status === "failed");
        const hasFailedCriterion = Array.isArray(overview.criteria)
          && overview.criteria.some((item) => isRecord(item) && item.observed_status === "failed");
        if (hasFailedVerification && hasFailedCriterion) {
          return {
            overview,
            candidate,
            detail: detailResponse,
            sourceResults: arrayField(detailResponse, "source_results", "result candidate detail").filter(isRecord),
          };
        }
      }
    }

    const now = Date.now();
    if (now < nextDispatchCheckAt) return null;
    nextDispatchCheckAt = now + 5_000;
    const [sourceWorkspaces, integrationWorkspace] = await Promise.all([
      Promise.all(dispatchWorkers.sourceWorkers.map((worker) => readWorkerWorkspace(
        request,
        workspaceId,
        worker.id,
        "source WorkerItem dispatch observation",
      ))),
      readWorkerWorkspace(
        request,
        workspaceId,
        dispatchWorkers.integrationWorker.id,
        "integration WorkerItem dispatch observation",
      ),
    ]);
    const sourceStatuses = sourceWorkspaces.map(workspaceExecutionStatus);
    if (!sourceStatuses.every((status) => status === "succeeded")) {
      dispatchStallStartedAt = null;
      return null;
    }
    dispatchStallStartedAt ??= now;
    const stalledForMs = now - dispatchStallStartedAt;
    if (!isIntegrationDispatchStalled({
      sourceStatuses,
      integrationHasExecution: workspaceHasExecution(integrationWorkspace),
      stalledForMs,
    })) return null;

    const subtaskWorkspace = await getJson(
      request,
      `/api/v1/tasks/${encodeURIComponent(rootTaskId)}/subtasks/${encodeURIComponent(dispatchWorkers.integrationWorker.subtaskKey)}?workspace_id=${encodeURIComponent(workspaceId)}`,
      "integration Subtask dispatch observation",
    );
    const integrationSubtask = recordField(subtaskWorkspace, "subtask", "integration Subtask dispatch observation");
    throw new Error(JSON.stringify({
      code: "integration_dispatch_stalled",
      root_task_id: rootTaskId,
      source_workers: dispatchWorkers.sourceWorkers.map((worker, index) => ({
        worker_item_id: worker.id,
        status: sourceStatuses[index],
      })),
      integration_worker_item_id: dispatchWorkers.integrationWorker.id,
      integration_subtask_state: stringField(integrationSubtask, "state", "integration Subtask dispatch observation"),
      stalled_for_ms: stalledForMs,
    }));
  }, timeoutMs, "failed result candidate and verification");
}

async function readWorkerWorkspace(
  request: APIRequestContext,
  workspaceId: string,
  workerItemId: string,
  label: string,
): Promise<JsonRecord> {
  return getJson(
    request,
    `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/worker-items/${encodeURIComponent(workerItemId)}/workspace`,
    label,
  );
}

function workspaceExecutionStatus(workspace: JsonRecord): string {
  const runtime = nullableRecord(workspace.current_runtime);
  const runtimeStatus = runtime === null ? null : nullableString(runtime.execution_status);
  if (runtimeStatus !== null) return runtimeStatus;
  const history = arrayField(workspace, "execution_history", "WorkerItem workspace").filter(isRecord);
  return history[0] === undefined
    ? "missing"
    : stringField(history[0], "status", "WorkerItem execution history");
}

function workspaceHasExecution(workspace: JsonRecord): boolean {
  if (arrayField(workspace, "execution_history", "WorkerItem workspace").length > 0) return true;
  const runtime = nullableRecord(workspace.current_runtime);
  return runtime !== null && nullableString(runtime.execution_id) !== null;
}

async function readCandidateById(
  request: APIRequestContext,
  workspaceId: string,
  rootTaskId: string,
  candidateId: string,
): Promise<CandidateFacts> {
  const [overview, detail] = await Promise.all([
    getJson(
      request,
      `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/tasks/${encodeURIComponent(rootTaskId)}/results`,
      "task result overview",
    ),
    getJson(
      request,
      `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/tasks/${encodeURIComponent(rootTaskId)}/results/${encodeURIComponent(candidateId)}`,
      "result candidate detail",
    ),
  ]);
  const candidates = candidateHistoryItems(overview.candidate_history);
  const candidate = isRecord(overview.current_candidate) && overview.current_candidate.candidate_id === candidateId
    ? overview.current_candidate
    : candidates.find((item) => item.candidate_id === candidateId);
  if (candidate === null || candidate === undefined) throw new Error("result_candidate_not_addressable");
  return {
    overview,
    candidate,
    detail,
    sourceResults: arrayField(detail, "source_results", "result candidate detail").filter(isRecord),
  };
}

async function waitForReworkRecord(
  request: APIRequestContext,
  workspaceId: string,
  rootTaskId: string,
  candidateId: string,
  workerItemId: string,
  timeoutMs: number,
): Promise<JsonRecord> {
  return waitForObserved(async () => {
    const page = await getJson(
      request,
      `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/tasks/${encodeURIComponent(rootTaskId)}/results/${encodeURIComponent(candidateId)}/rework?limit=100`,
      "candidate rework records",
    );
    const items = arrayField(page, "items", "candidate rework records").filter(isRecord);
    const found = items.find((item) => item.candidate_id === candidateId
      && Array.isArray(item.target_worker_item_ids)
      && item.target_worker_item_ids.length === 1
      && item.target_worker_item_ids[0] === workerItemId);
    return found ?? null;
  }, timeoutMs, "explicit WorkerItem-scoped result rework record");
}

async function waitForReplacementCandidate(
  request: APIRequestContext,
  workspaceId: string,
  rootTaskId: string,
  sourceCandidateId: string,
  timeoutMs: number,
): Promise<CandidateFacts> {
  return waitForObserved(async () => {
    const overview = await getJson(
      request,
      `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/tasks/${encodeURIComponent(rootTaskId)}/results`,
      "task result overview after rework",
    );
    const candidate = nullableRecord(overview.current_candidate);
    if (candidate === null || candidate.candidate_id === sourceCandidateId) return null;
    const candidateId = stringField(candidate, "candidate_id", "replacement result candidate");
    const detail = await getOptionalJson(
      request,
      `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/tasks/${encodeURIComponent(rootTaskId)}/results/${encodeURIComponent(candidateId)}`,
      "replacement result detail",
    );
    if (detail === null) return null;
    return {
      overview,
      candidate,
      detail,
      sourceResults: arrayField(detail, "source_results", "replacement result detail").filter(isRecord),
    };
  }, timeoutMs, "replacement result candidate after local rework");
}

async function waitForExecutionSession(
  request: APIRequestContext,
  workspaceId: string,
  workerItemId: string,
  executionId: string,
  timeoutMs: number,
): Promise<{ workspace: JsonRecord; sessionId: string }> {
  return waitForObserved(async () => {
    const workspace = await getJson(
      request,
      `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/worker-items/${encodeURIComponent(workerItemId)}/workspace`,
      "WorkerItem workspace after rework",
    );
    const runtime = nullableRecord(workspace.current_runtime);
    const runtimeSession = runtime !== null && runtime.execution_id === executionId
      ? nullableString(runtime.session_id)
      : null;
    const execution = arrayField(workspace, "execution_history", "WorkerItem workspace after rework")
      .filter(isRecord)
      .find((item) => item.execution_id === executionId);
    const historySession = execution === undefined ? null : nullableString(execution.session_id);
    const sessionId = runtimeSession ?? historySession;
    return sessionId === null ? null : { workspace, sessionId };
  }, timeoutMs, "rework execution and session identity");
}

async function waitForRevisionToolCallId(
  request: APIRequestContext,
  gatewayUrl: string,
  threadId: string,
  timeoutMs: number,
): Promise<string> {
  return waitForObserved(async () => {
    const thread = await getJson(
      request,
      `${gatewayUrl}/main-agent/v1/threads/${encodeURIComponent(threadId)}`,
      "Main Agent thread",
    );
    const turns = arrayField(thread, "turns", "Main Agent thread").filter(isRecord);
    const calls = turns.flatMap((turn) => arrayField(turn, "items", "Main Agent turn")
      .filter(isRecord)
      .filter((item) => {
        if (item.type !== "toolCall" || !isRecord(item.data)) return false;
        return item.data.name === "sidera_create_plan_revision"
          || item.data.toolName === "sidera_create_plan_revision"
          || item.data.tool === "sidera_create_plan_revision";
      }));
    if (calls.length === 0) return null;
    if (calls.length !== 1) throw new Error("main_agent_revision_tool_call_ambiguous");
    return stringField(calls[0]!, "id", "Main Agent revision tool call");
  }, timeoutMs, "Main Agent sidera_create_plan_revision tool call");
}

async function waitForPlanTransition(
  request: APIRequestContext,
  workspaceId: string,
  rootTaskId: string,
  toGeneration: number,
  timeoutMs: number,
): Promise<JsonRecord> {
  return waitForObserved(async () => {
    const response = await request.get(
      `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/tasks/${encodeURIComponent(rootTaskId)}/plan-transitions`,
    );
    if (!response.ok()) throw new Error(`plan transitions read failed: HTTP ${response.status()}`);
    const values = await response.json() as unknown;
    if (!Array.isArray(values)) throw new Error("plan_transitions_invalid");
    const matching = values.filter((item): item is JsonRecord => (
      isRecord(item) && item.to_generation === toGeneration
    ));
    if (matching.length > 1) throw new Error("plan_transition_ambiguous");
    const transition = matching[0];
    if (transition === undefined) return null;
    if (transition.status === "converged" || transition.status === "degraded") return transition;
    if (transition.status !== "converging") throw new Error("plan_transition_status_invalid");
    return null;
  }, timeoutMs, "terminal plan transition for the confirmed generation");
}

async function rejectInputFromOldPlan(
  request: APIRequestContext,
  workspaceId: string,
  workerItemId: string,
  oldContext: JsonRecord,
  newGeneration: number,
  marker: string,
  timeoutMs: number,
): Promise<boolean> {
  await waitForObserved(async () => {
    const workspace = await getJson(
      request,
      `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/worker-items/${encodeURIComponent(workerItemId)}/workspace`,
      "WorkerItem workspace after plan revision",
    );
    const placement = recordField(workspace, "current_placement", "WorkerItem workspace after plan revision");
    return numberField(placement, "plan_generation", "current WorkerItem placement") === newGeneration
      ? true
      : null;
  }, timeoutMs, "WorkerItem placement on the revised generation");
  const sessionId = stringField(oldContext, "session_id", "pre-revision Worker context");
  const expectedVersion = numberField(oldContext, "session_version", "pre-revision Worker context");
  const identity = randomUUID();
  const response = await request.post("/api/v1/commands", {
    data: {
      command_id: randomUUID(),
      workspace_id: workspaceId,
      command_type: "SendExecutionInput",
      actor: { type: "local", id: "aitejiu" },
      target_type: "execution_session",
      target_id: sessionId,
      expected_version: expectedVersion,
      idempotency_key: `r6-b-stale-${identity}`,
      correlation_id: randomUUID(),
      payload: {
        context: oldContext,
        client_idempotency_key: `r6-b-stale-input-${identity}`,
        text: `stale-revision-probe-${marker}`,
        attachment_refs: [],
      },
    },
  });
  if (response.status() !== 409) return false;
  const body = await response.json() as unknown;
  return isRecord(body)
    && isRecord(body.error)
    && ["execution_context_changed", "invalid_transition"].includes(String(body.error.code));
}

function deriveChangedWorkerIds(diff: JsonRecord): Set<string> {
  const changed = new Set<string>();
  for (const item of arrayField(diff, "worker_item_changes", "plan revision diff").filter(isRecord)) {
    const existingId = nullableString(item.existing_worker_item_id);
    if (existingId === null) continue;
    const strategy = stringField(item, "strategy", "revision WorkerItem change");
    const fieldChanges = arrayField(item, "field_changes", "revision WorkerItem change");
    if (strategy !== "reuse" || fieldChanges.length > 0) changed.add(existingId);
  }
  return changed;
}

function recordScenarioObservation(
  manifest: RealBasicManifest,
  scenario: "intervention" | "revision",
  snapshot: JsonRecord,
): void {
  recordAtomicObservation(manifest, scenario, {
    schema_version: 1,
    run_id: manifest.run_id,
    workspace_id: manifest.workspace_id,
    implementation_fingerprint: manifest.implementation_fingerprint,
    runtime_fingerprint: manifest.runtime_fingerprint,
    scenario,
    snapshot,
  });
}

function recordGroupObservation(
  manifest: RealBasicManifest,
  facts: {
    rootTaskId: string;
    commandIds: string[];
    resourceIds: string[];
    sourceExecutionIds: string[];
    integrationExecutionId: string;
    resultCandidateId: string;
  },
): void {
  recordAtomicObservation(manifest, "group-B", {
    schema_version: 1,
    run_id: manifest.run_id,
    workspace_id: manifest.workspace_id,
    implementation_fingerprint: manifest.implementation_fingerprint,
    runtime_fingerprint: manifest.runtime_fingerprint,
    group: "B",
    root_task_id: facts.rootTaskId,
    command_ids: facts.commandIds,
    resource_ids: facts.resourceIds,
    source_execution_ids: facts.sourceExecutionIds,
    integration_execution_id: facts.integrationExecutionId,
    result_candidate_id: facts.resultCandidateId,
    result_accepted: false,
    status: "completed",
  });
}

function recordAtomicObservation(
  manifest: RealBasicManifest,
  name: string,
  payload: JsonRecord,
): void {
  const observationPath = resolve(manifest.run_root, "observations", `${name}.json`);
  if (existsSync(observationPath)) throw new Error(`real_observation_already_exists:${name}`);
  mkdirSync(dirname(observationPath), { recursive: true, mode: 0o700 });
  chmodSync(dirname(observationPath), 0o700);
  const temporaryPath = `${observationPath}.${process.pid}.${randomUUID()}.tmp`;
  writeFileSync(temporaryPath, `${JSON.stringify(payload, null, 2)}\n`, {
    encoding: "utf8",
    mode: 0o600,
  });
  chmodSync(temporaryPath, 0o600);
  renameSync(temporaryPath, observationPath);
}

function readRealManifest(): RealBasicManifest {
  const path = required("SIDERA_BASIC_REAL_MANIFEST");
  const value = readJson(path);
  const runRoot = stringField(value, "run_root", "real manifest");
  if (!runRoot.startsWith("/")) throw new Error("real manifest run_root must be absolute");
  return value as unknown as RealBasicManifest;
}

function readJson(path: string): JsonRecord {
  let parsed: unknown;
  try {
    parsed = JSON.parse(readFileSync(path, "utf8")) as unknown;
  } catch (error) {
    throw new Error("real acceptance JSON unreadable", { cause: error });
  }
  if (!isRecord(parsed)) throw new Error("real acceptance JSON must be an object");
  return parsed;
}

async function waitForObserved<T>(
  read: () => Promise<T | null>,
  timeoutMs: number,
  label: string,
): Promise<T> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const value = await read();
    if (value !== null) return value;
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 1_000));
  }
  throw new Error(`real observation timed out: ${label}`);
}

async function getJson(
  request: APIRequestContext,
  path: string,
  label: string,
): Promise<JsonRecord> {
  const response = await request.get(path);
  if (!response.ok()) throw new Error(`${label} read failed: HTTP ${response.status()}`);
  return objectJson(response, label);
}

async function getOptionalJson(
  request: APIRequestContext,
  path: string,
  label: string,
): Promise<JsonRecord | null> {
  const response = await request.get(path);
  if (response.status() === 404) return null;
  if (!response.ok()) throw new Error(`${label} read failed: HTTP ${response.status()}`);
  return objectJson(response, label);
}

async function objectJson(response: { json(): Promise<unknown> }, label: string): Promise<JsonRecord> {
  const value = await response.json();
  if (!isRecord(value)) throw new Error(`${label} response must be an object`);
  return value;
}

async function requestBody(request: { postDataJSON(): unknown }): Promise<JsonRecord> {
  const body = request.postDataJSON();
  if (!isRecord(body)) throw new Error("command request body must be an object");
  return body;
}

function commandType(request: { postDataJSON(): unknown }): unknown {
  try {
    const body = request.postDataJSON();
    return isRecord(body) ? body.command_type : undefined;
  } catch {
    return undefined;
  }
}

function recordField(value: JsonRecord, key: string, label: string): JsonRecord {
  const field = value[key];
  if (!isRecord(field)) throw new Error(`${label}.${key} must be an object`);
  return field;
}

function nullableRecord(value: unknown): JsonRecord | null {
  return isRecord(value) ? value : null;
}

function arrayField(value: JsonRecord, key: string, label: string): unknown[] {
  const field = value[key];
  if (!Array.isArray(field)) throw new Error(`${label}.${key} must be an array`);
  return field;
}

function stringField(value: JsonRecord, key: string, label: string): string {
  const field = value[key];
  if (typeof field !== "string" || field.length === 0) throw new Error(`${label}.${key} must be a string`);
  return field;
}

function nullableString(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function numberField(value: JsonRecord, key: string, label: string): number {
  const field = value[key];
  if (typeof field !== "number" || !Number.isFinite(field)) throw new Error(`${label}.${key} must be a number`);
  return field;
}

function numberFieldOr(value: JsonRecord, key: string, fallback: number): number {
  const field = value[key];
  return typeof field === "number" && Number.isFinite(field) ? field : fallback;
}

function booleanField(value: JsonRecord, key: string, label: string): boolean {
  const field = value[key];
  if (typeof field !== "boolean") throw new Error(`${label}.${key} must be a boolean`);
  return field;
}

function oneWorkerId(value: unknown, expected: string, label: string): string {
  if (!Array.isArray(value) || value.length !== 1 || value[0] !== expected) {
    throw new Error(`${label} must target exactly the selected WorkerItem`);
  }
  return expected;
}

function findUuidField(value: unknown, key: string): string | null {
  if (!isRecord(value)) return null;
  const field = value[key];
  return typeof field === "string" && /^[0-9a-f-]{36}$/i.test(field) ? field : null;
}

function required(name: string): string {
  const value = process.env[name];
  if (value === undefined || value.trim() === "") throw new Error(`${name} is required for the real B journey`);
  return value;
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function isRecord(value: unknown): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
