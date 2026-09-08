import { randomUUID } from "node:crypto";
import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { join, resolve } from "node:path";
import { tmpdir } from "node:os";

import { test as base, type Page } from "@playwright/test";

export const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
export const R5_WORKSPACE_ID = "20000000-0000-0000-0000-000000000002";
export const R6_1_WORKSPACE_ID = "20000000-0000-0000-0000-000000000003";
export const R6_2_WORKSPACE_ID = "20000000-0000-0000-0000-000000000004";
export const R6_3_1_WORKSPACE_ID = "20000000-0000-0000-0000-000000000005";
export const R6_3_2_WORKSPACE_ID = "20000000-0000-0000-0000-000000000006";
export const ROOT_TASK_ID = "30000000-0000-0000-0000-000000000001";
export const SUBTASK_TASK_ID = "30000000-0000-0000-0000-000000000002";
export const SECOND_ROOT_TASK_ID = "30000000-0000-0000-0000-000000000005";
export const IMPLEMENTER_WORKER_ITEM_ID = "a1000000-0000-0000-0000-000000000001";
export const TESTER_WORKER_ITEM_ID = "a1000000-0000-0000-0000-000000000002";
export const REVIEWER_WORKER_ITEM_ID = "a1000000-0000-0000-0000-000000000003";
export const EXECUTION_ID = "60000000-0000-0000-0000-000000000001";
export const SCALE_EXECUTION_ID = "60000000-0000-0000-0000-000000000002";
export const RUNTIME_EXECUTION_ID = "60000000-0000-0000-0000-000000000006";
export const SCALE_WORKER_ITEM_ID = "a1000000-0000-0000-0000-000000000101";
export const RUNTIME_WORKER_ID = "80000000-0000-0000-0000-000000000003";
export const SESSION_ID = "70000000-0000-0000-0000-000000000001";
export const SCALE_SESSION_ID = "70000000-0000-0000-0000-000000000002";
export const CONTEXT_EXECUTION_PACKAGE_ID = "b0000000-0000-0000-0000-000000000005";
export const CONTEXT_PLANNING_PACKAGE_ID = "b0000000-0000-0000-0000-000000000007";
export const CONTEXT_PLANNING_RUN_ID = "b0000000-0000-0000-0000-000000000008";
export const CONTEXT_PLANNING_REQUEST_ID = "b0000000-0000-0000-0000-000000000001";
export const KNOWLEDGE_PAGE_ID = "c0000000-0000-0000-0000-000000000002";
export const KNOWLEDGE_REVISION_ONE_ID = "c0000000-0000-0000-0000-000000000003";
export const KNOWLEDGE_REVISION_TWO_ID = "c0000000-0000-0000-0000-000000000004";
export const KNOWLEDGE_PENDING_CANDIDATE_ID = "c0000000-0000-0000-0000-000000000005";
export const KNOWLEDGE_STALE_CANDIDATE_ID = "c0000000-0000-0000-0000-000000000006";
export const NOTIFICATION_THREAD_ID = "60000000-0000-0000-0000-000000000001";
export const NOTIFICATION_FACT_ID = "70000000-0000-0000-0000-000000000001";
export const NOTIFICATION_DELIVERY_ID = "80000000-0000-0000-0000-000000000001";

export type SeedScenario = "task-first" | "scale" | "runtime-routing" | "context-knowledge";
export type PlanningSeedScenario = "proposal" | "clarification" | "scale" | "real";
export type HandoffSeedScenario = "normal" | "target-start-unknown";
export type R6HandoffSeed = {
  workspace_id: string;
  root_task_id: string;
  source_execution_id: string;
  source_session_id: string;
  worker_item_ids: Record<"a" | "b" | "c", string>;
  target_worker_ids: string[];
  scenario: HandoffSeedScenario;
};
export type R6CollaborationSeed = {
  workspace_id: string;
  root_task_id: string;
  plan_snapshot_id: string;
  planning_request_id: string;
  proposal_id: string;
  runtime_definition_id: string;
  runtime_instance_id: string;
  subtask_ids: Record<string, string>;
  worker_item_ids: Record<string, string>;
  request_ids: Record<string, string>;
  worker_ids: Record<string, string>;
};
export type R6ResultInputSeed = {
  workspace_id: string;
  root_task_id: string;
  summary_worker_item_id: string;
  source_worker_item_ids: string[];
};
export type R6ResultInputAction =
  | "seed"
  | "prepare-browser"
  | "finish-late-source"
  | "recover-body"
  | "inspect";
export type R6ResultIntegrationAction = "repair" | "accept";
export type R6ResultIntegrationSeed = {
  workspace_id: string;
  root_task_id: string;
  worker_item_ids: Record<string, string>;
  current_candidate_id: string | null;
  phase: string;
};

const webRoot = resolve(import.meta.dirname, "../..");
const repositoryRoot = resolve(webRoot, "../..");
const controlPlaneRoot = resolve(repositoryRoot, "services/control-plane");
const seedScript = resolve(controlPlaneRoot, "tests/e2e/seed_r4_workspace.py");
const planningSeedScript = resolve(controlPlaneRoot, "tests/e2e/seed_r5_planning.py");
const revisionSeedScript = resolve(controlPlaneRoot, "tests/e2e/seed_r6_1_revision.py");
const handoffSeedScript = resolve(controlPlaneRoot, "tests/e2e/seed_r6_2_handoff.py");
const handoffRuntimeScript = resolve(controlPlaneRoot, "tests/e2e/r6_2_fake_runtime.py");
const collaborationSeedScript = resolve(controlPlaneRoot, "tests/e2e/seed_r6_3_1_collaboration.py");
const collaborationRuntimeScript = resolve(controlPlaneRoot, "tests/e2e/r6_3_1_fake_runtime.py");
const governanceSeedScript = resolve(controlPlaneRoot, "tests/e2e/seed_r6_3_2_governance.py");
const governanceRuntimeScript = resolve(controlPlaneRoot, "tests/e2e/r6_3_2_governance_runtime.py");
const resultInputRuntimeModule = "tests.e2e.r6_3_4_fake_inputs";
const defaultPython = resolve(controlPlaneRoot, ".venv/bin/python");
const knowledgeServiceRoot = resolve(repositoryRoot, "services/knowledge-service");
const knowledgeSeedScript = resolve(knowledgeServiceRoot, "tests/e2e/seed_r5_3_knowledge.py");
const knowledgeDefaultPython = resolve(knowledgeServiceRoot, ".venv/bin/python");
const defaultContextPackageCutoverAt = "2026-08-27T00:00:00+08:00";
const resultInputRoots = new Map<string, string>();
const resultIntegrationRuntimeModule = "tests.e2e.result_integration_seed";
const resultIntegrationRoots = new Map<string, string>();

export function seedR4Workspace(scenario: SeedScenario = "task-first"): void {
  execFileSync(
    process.env.SIDERA_E2E_PYTHON ?? defaultPython,
    [seedScript, "--scenario", scenario, "--workspace-id", WORKSPACE_ID],
    {
      cwd: controlPlaneRoot,
      stdio: "pipe",
      env: {
        ...process.env,
        SIDERA_ALLOW_E2E_SEED: "1",
        SIDERA_DATABASE_URL: process.env.SIDERA_DATABASE_URL
          ?? "postgresql+psycopg://sidera:sidera@127.0.0.1:55432/sidera_test",
      },
    },
  );
}

export function seedR5Planning(scenario: PlanningSeedScenario = "proposal"): void {
  execFileSync(
    process.env.SIDERA_E2E_PYTHON ?? defaultPython,
    [planningSeedScript, "--scenario", scenario, "--workspace-id", R5_WORKSPACE_ID],
    {
      cwd: controlPlaneRoot,
      stdio: "pipe",
      env: e2eEnvironment(),
    },
  );
}

export function seedR6Revision(): void {
  execFileSync(
    process.env.SIDERA_E2E_PYTHON ?? defaultPython,
    [revisionSeedScript, "--workspace-id", R6_1_WORKSPACE_ID, "--reset"],
    {
      cwd: controlPlaneRoot,
      stdio: "pipe",
      env: e2eEnvironment(),
    },
  );
}

export function seedR6Handoff(scenario: HandoffSeedScenario = "normal"): R6HandoffSeed {
  const output = execFileSync(
    process.env.SIDERA_E2E_PYTHON ?? defaultPython,
    [
      handoffSeedScript,
      "--workspace-id",
      R6_2_WORKSPACE_ID,
      "--scenario",
      scenario,
      "--reset",
    ],
    {
      cwd: controlPlaneRoot,
      stdio: "pipe",
      env: e2eEnvironment(),
      encoding: "utf8",
    },
  );
  const lastLine = output.trim().split(/\r?\n/).at(-1);
  if (lastLine === undefined || lastLine.length === 0) {
    throw new Error("R6.2 handoff seed did not return a JSON summary");
  }
  const parsed: unknown = JSON.parse(lastLine);
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error("R6.2 handoff seed returned a non-object summary");
  }
  return parsed as R6HandoffSeed;
}

export function seedR6Collaboration(): R6CollaborationSeed {
  const output = execFileSync(
    process.env.SIDERA_E2E_PYTHON ?? defaultPython,
    [
      collaborationSeedScript,
      "--workspace-id",
      R6_3_1_WORKSPACE_ID,
      "--reset",
    ],
    {
      cwd: controlPlaneRoot,
      stdio: "pipe",
      env: e2eEnvironment(),
      encoding: "utf8",
    },
  );
  const lastLine = output.trim().split(/\r?\n/).at(-1);
  if (lastLine === undefined || lastLine.length === 0) {
    throw new Error("R6.3.1 collaboration seed did not return a JSON summary");
  }
  const parsed: unknown = JSON.parse(lastLine);
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error("R6.3.1 collaboration seed returned a non-object summary");
  }
  return parsed as R6CollaborationSeed;
}

export function seedR6Governance(): R6CollaborationSeed {
  const output = execFileSync(
    process.env.SIDERA_E2E_PYTHON ?? defaultPython,
    [
      governanceSeedScript,
      "--workspace-id",
      R6_3_2_WORKSPACE_ID,
      "--reset",
    ],
    {
      cwd: controlPlaneRoot,
      stdio: "pipe",
      env: e2eEnvironment(),
      encoding: "utf8",
    },
  );
  const lastLine = output.trim().split(/\r?\n/).at(-1);
  if (lastLine === undefined || lastLine.length === 0) {
    throw new Error("R6.3.2 governance seed did not return a JSON summary");
  }
  const parsed: unknown = JSON.parse(lastLine);
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error("R6.3.2 governance seed returned a non-object summary");
  }
  return parsed as R6CollaborationSeed;
}

export function runR6CollaborationOnce(
  workspaceId: string = R6_3_1_WORKSPACE_ID,
): Record<string, unknown> {
  return runFakeCollaborationRuntime(workspaceId);
}

export function prepareR6CollaborationBrowser(
  workspaceId: string = R6_3_1_WORKSPACE_ID,
): Record<string, unknown> {
  return runFakeCollaborationRuntime(workspaceId, true);
}

export function runR6GovernanceOnce(
  workspaceId: string = R6_3_2_WORKSPACE_ID,
): Record<string, unknown> {
  return runFakeCollaborationRuntime(workspaceId);
}

export function prepareR6GovernanceBrowser(
  workspaceId: string = R6_3_2_WORKSPACE_ID,
): Record<string, unknown> {
  return runGovernanceRuntime(workspaceId, "--start-sibling");
}

export function runR6GovernanceAfterPause(
  workspaceId: string = R6_3_2_WORKSPACE_ID,
): Record<string, unknown> {
  return runGovernanceRuntime(workspaceId);
}

export function seedR6ResultInputs(
  workspaceId: string,
  root: string,
): R6ResultInputSeed {
  runR6ResultInputAction(workspaceId, root, "seed");
  const state = runR6ResultInputAction(workspaceId, root, "prepare-browser");
  const rootTaskId = requiredStateString(state, "root_task_id");
  const workerItemIds = requiredStateRecord(state, "worker_item_ids");
  const summaryWorkerItemId = requiredRecordString(workerItemIds, "summary");
  const sourceWorkerItemIds = ["a", "b", "c"].map((key) => requiredRecordString(workerItemIds, key));
  return {
    workspace_id: workspaceId,
    root_task_id: rootTaskId,
    summary_worker_item_id: summaryWorkerItemId,
    source_worker_item_ids: sourceWorkerItemIds,
  };
}

export function prepareR6ResultInputsBrowser(workspaceId: string): Record<string, unknown> {
  return runR6ResultInputActionForWorkspace(workspaceId, "prepare-browser");
}

export function runR6ResultInputActionForWorkspace(
  workspaceId: string,
  action: R6ResultInputAction,
): Record<string, unknown> {
  const root = resultInputRoots.get(workspaceId);
  if (root === undefined) {
    throw new Error(`R6.3.4 result-input workspace is not registered: ${workspaceId}`);
  }
  return runR6ResultInputAction(workspaceId, root, action);
}

export function seedResultIntegration(
  workspaceId: string,
  root: string,
): R6ResultIntegrationSeed {
  runResultIntegrationAction(workspaceId, root, "seed");
  const state = runResultIntegrationAction(workspaceId, root, "prepare");
  return parseResultIntegrationSeed(state);
}

export function prepareResultIntegrationBrowser(workspaceId: string): R6ResultIntegrationSeed {
  const root = resultIntegrationRoots.get(workspaceId);
  if (root === undefined) {
    throw new Error(`R6 result-integration workspace is not registered: ${workspaceId}`);
  }
  return parseResultIntegrationSeed(runResultIntegrationAction(workspaceId, root, "prepare"));
}

export function advanceResultIntegration(
  workspaceId: string,
  action: R6ResultIntegrationAction,
): R6ResultIntegrationSeed {
  const root = resultIntegrationRoots.get(workspaceId);
  if (root === undefined) {
    throw new Error(`R6 result-integration workspace is not registered: ${workspaceId}`);
  }
  return parseResultIntegrationSeed(runResultIntegrationAction(workspaceId, root, "advance", action));
}

export function seedKnowledge(workspaceId: string = WORKSPACE_ID): void {
  execFileSync(
    process.env.SIDERA_E2E_KNOWLEDGE_PYTHON ?? knowledgeDefaultPython,
    [knowledgeSeedScript, "--workspace-id", workspaceId],
    {
      cwd: knowledgeServiceRoot,
      stdio: "pipe",
      env: {
        ...process.env,
        SIDERA_ALLOW_E2E_SEED: "1",
        SIDERA_KNOWLEDGE_DATABASE_URL: process.env.SIDERA_KNOWLEDGE_DATABASE_URL
          ?? "postgresql+psycopg://sidera:sidera@127.0.0.1:55432/sidera_knowledge_test",
      },
    },
  );
}

export function runPlanningWorkerOnce(): Record<string, unknown> {
  runContextPackageWorkerOnce();
  return runControlPlaneOnce("sidera_control_plane.planning_worker");
}

export function runContextPackageWorkerOnce(): Record<string, unknown> {
  return runControlPlaneOnce("sidera_control_plane.context_package_worker");
}

export function runActivationWorkerOnce(): Record<string, unknown> {
  return runControlPlaneOnce("sidera_control_plane.activation_worker");
}

export function runSchedulerOnce(): Record<string, unknown> {
  return runControlPlaneOnce("sidera_control_plane.scheduler_worker");
}

export function runPlanTransitionWorkerOnce(): Record<string, unknown> {
  return runControlPlaneOnce("sidera_control_plane.plan_transition_worker");
}

export function runHandoffWorkerOnce(
  workspaceId: string = R6_2_WORKSPACE_ID,
  scenario: HandoffSeedScenario = "normal",
): Record<string, unknown> {
  return runFakeHandoffRuntime(workspaceId, scenario, "--until-converged");
}

export function runHandoffUntilUnknown(
  workspaceId: string = R6_2_WORKSPACE_ID,
  scenario: HandoffSeedScenario = "target-start-unknown",
): Record<string, unknown> {
  return runFakeHandoffRuntime(workspaceId, scenario, "--until-unknown");
}

function runFakeHandoffRuntime(
  workspaceId: string,
  scenario: HandoffSeedScenario,
  mode: "--until-converged" | "--until-unknown",
): Record<string, unknown> {
  const output = execFileSync(
    process.env.SIDERA_E2E_PYTHON ?? defaultPython,
    [
      handoffRuntimeScript,
      "--workspace-id",
      workspaceId,
      "--scenario",
      scenario,
      mode,
    ],
    {
      cwd: controlPlaneRoot,
      stdio: "pipe",
      env: e2eEnvironment(),
      encoding: "utf8",
    },
  );
  const lastLine = output.trim().split(/\r?\n/).at(-1);
  if (lastLine === undefined || lastLine.length === 0) {
    throw new Error("R6.2 fake runtime did not return a JSON summary");
  }
  const parsed: unknown = JSON.parse(lastLine);
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error("R6.2 fake runtime returned a non-object summary");
  }
  return parsed as Record<string, unknown>;
}

function runFakeCollaborationRuntime(
  workspaceId: string,
  browserState = false,
): Record<string, unknown> {
  const output = execFileSync(
    process.env.SIDERA_E2E_PYTHON ?? defaultPython,
    [
      collaborationRuntimeScript,
      "--workspace-id",
      workspaceId,
      ...(browserState ? ["--browser-state"] : []),
    ],
    {
      cwd: controlPlaneRoot,
      stdio: "pipe",
      env: e2eEnvironment(),
      encoding: "utf8",
    },
  );
  const lastLine = output.trim().split(/\r?\n/).at(-1);
  if (lastLine === undefined || lastLine.length === 0) {
    throw new Error("R6.3.1 fake runtime did not return a JSON summary");
  }
  const parsed: unknown = JSON.parse(lastLine);
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error("R6.3.1 fake runtime returned a non-object summary");
  }
  return parsed as Record<string, unknown>;
}

function runGovernanceRuntime(
  workspaceId: string,
  mode?: "--start-sibling",
): Record<string, unknown> {
  const output = execFileSync(
    process.env.SIDERA_E2E_PYTHON ?? defaultPython,
    [
      governanceRuntimeScript,
      "--workspace-id",
      workspaceId,
      ...(mode === undefined ? [] : [mode]),
    ],
    {
      cwd: controlPlaneRoot,
      stdio: "pipe",
      env: e2eEnvironment(),
      encoding: "utf8",
    },
  );
  const lastLine = output.trim().split(/\r?\n/).at(-1);
  if (lastLine === undefined || lastLine.length === 0) {
    throw new Error("R6.3.2 governance runtime did not return a JSON summary");
  }
  const parsed: unknown = JSON.parse(lastLine);
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error("R6.3.2 governance runtime returned a non-object summary");
  }
  return parsed as Record<string, unknown>;
}

function runR6ResultInputAction(
  workspaceId: string,
  root: string,
  action: R6ResultInputAction,
): Record<string, unknown> {
  const output = execFileSync(
    process.env.SIDERA_E2E_PYTHON ?? defaultPython,
    ["-m", resultInputRuntimeModule, "--workspace-id", workspaceId, "--action", action],
    {
      cwd: controlPlaneRoot,
      stdio: "pipe",
      env: {
        ...e2eEnvironment(),
        SIDERA_R6_3_4_TEST_MODE: "1",
        SIDERA_R6_3_4_BROWSER_STATE: "1",
        SIDERA_R6_3_4_TEST_ROOT: root,
      },
      encoding: "utf8",
    },
  );
  const lastLine = output.trim().split(/\r?\n/).at(-1);
  if (lastLine === undefined || lastLine.length === 0) {
    throw new Error(`R6.3.4 result-input action ${action} did not return a JSON summary`);
  }
  const parsed: unknown = JSON.parse(lastLine);
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error(`R6.3.4 result-input action ${action} returned a non-object summary`);
  }
  return parsed as Record<string, unknown>;
}

function runResultIntegrationAction(
  workspaceId: string,
  root: string,
  mode: "seed" | "prepare" | "advance",
  action?: R6ResultIntegrationAction,
): Record<string, unknown> {
  const output = execFileSync(
    process.env.SIDERA_E2E_PYTHON ?? defaultPython,
    [
      "-m",
      resultIntegrationRuntimeModule,
      "--workspace-id",
      workspaceId,
      "--mode",
      mode,
      ...(action === undefined ? [] : ["--action", action]),
    ],
    {
      cwd: controlPlaneRoot,
      stdio: "pipe",
      env: {
        ...e2eEnvironment(),
        SIDERA_R6_RESULT_INTEGRATION_TEST_MODE: "1",
        SIDERA_R6_RESULT_INTEGRATION_SCOPE: "provider-free-result-integration",
        SIDERA_R6_RESULT_INTEGRATION_TEST_ROOT: root,
        SIDERA_RESULT_INTEGRATION_ENABLED: "1",
        SIDERA_RESULT_INPUTS_ENABLED: "1",
        SIDERA_RUN_REAL_RESULT_INTEGRATION: "0",
        SIDERA_RUN_REAL_RESULT_INPUTS: "0",
      },
      encoding: "utf8",
    },
  );
  const lastLine = output.trim().split(/\r?\n/).at(-1);
  if (lastLine === undefined || lastLine.length === 0) {
    throw new Error(`R6 result-integration action ${mode} did not return a JSON summary`);
  }
  const parsed: unknown = JSON.parse(lastLine);
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error(`R6 result-integration action ${mode} returned a non-object summary`);
  }
  return parsed as Record<string, unknown>;
}

function parseResultIntegrationSeed(state: Record<string, unknown>): R6ResultIntegrationSeed {
  const workerItemIds = requiredStateRecord(state, "worker_item_ids");
  return {
    workspace_id: requiredStateString(state, "workspace_id"),
    root_task_id: requiredStateString(state, "root_task_id"),
    worker_item_ids: Object.fromEntries(
      Object.entries(workerItemIds).map(([key]) => [key, requiredRecordString(workerItemIds, key)]),
    ),
    current_candidate_id: state.current_candidate_id === null
      ? null
      : requiredStateString(state, "current_candidate_id"),
    phase: requiredStateString(state, "phase"),
  };
}

function requiredStateString(state: Record<string, unknown>, key: string): string {
  const value = state[key];
  if (typeof value !== "string" || value.length === 0) {
    throw new Error(`R6.3.4 result-input state is missing ${key}`);
  }
  return value;
}

function requiredStateRecord(
  state: Record<string, unknown>,
  key: string,
): Record<string, unknown> {
  const value = state[key];
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error(`R6.3.4 result-input state is missing record ${key}`);
  }
  return value as Record<string, unknown>;
}

function requiredRecordString(record: Record<string, unknown>, key: string): string {
  const value = record[key];
  if (typeof value !== "string" || value.length === 0) {
    throw new Error(`R6.3.4 result-input state is missing worker ${key}`);
  }
  return value;
}

function runControlPlaneOnce(moduleName: string): Record<string, unknown> {
  const output = execFileSync(
    process.env.SIDERA_E2E_PYTHON ?? defaultPython,
    ["-m", moduleName, "--once"],
    {
      cwd: controlPlaneRoot,
      stdio: "pipe",
      env: e2eEnvironment(),
      encoding: "utf8",
    },
  );
  const lastLine = output.trim().split(/\r?\n/).at(-1);
  if (lastLine === undefined || lastLine.length === 0) {
    throw new Error(`${moduleName} did not return a JSON summary`);
  }
  const parsed: unknown = JSON.parse(lastLine);
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error(`${moduleName} returned a non-object summary`);
  }
  return parsed as Record<string, unknown>;
}

function e2eEnvironment(): NodeJS.ProcessEnv {
  const knowledgeBaseUrl = process.env.SIDERA_E2E_KNOWLEDGE_BASE_URL
    ?? `http://127.0.0.1:${process.env.SIDERA_E2E_KNOWLEDGE_SERVICE_PORT ?? "8020"}`;
  return {
    ...process.env,
    SIDERA_ALLOW_E2E_SEED: "1",
    SIDERA_DATABASE_URL: process.env.SIDERA_DATABASE_URL
      ?? "postgresql+psycopg://sidera:sidera@127.0.0.1:55432/sidera_test",
    SIDERA_KNOWLEDGE_BASE_URL: knowledgeBaseUrl,
    SIDERA_KNOWLEDGE_CONTROL_PLANE_TOKEN: process.env.SIDERA_E2E_CONTROL_PLANE_TOKEN
      ?? "r5-e2e-control-plane-token",
    SIDERA_CONTEXT_PACKAGE_CUTOVER_AT: process.env.SIDERA_CONTEXT_PACKAGE_CUTOVER_AT
      ?? defaultContextPackageCutoverAt,
  };
}

export async function mockNotificationInbox(page: Page): Promise<void> {
  const deepLink = `/workspaces/${WORKSPACE_ID}/worker-items/${IMPLEMENTER_WORKER_ITEM_ID}?view=governance`;
  const thread = {
    id: NOTIFICATION_THREAD_ID,
    workspace_id: WORKSPACE_ID,
    recipient_type: "workspace_operator",
    recipient_id: "aitejiu",
    category: "action",
    business_status: "open",
    organization_status: "active",
    priority: "action",
    root_task_id: ROOT_TASK_ID,
    root_task_title: "支付重构",
    subtask_id: SUBTASK_TASK_ID,
    subtask_title: "通知网关",
    worker_item_id: IMPLEMENTER_WORKER_ITEM_ID,
    worker_item_title: "实现者",
    title: "需要确认执行分支",
    summary: "当前 WorkerItem 等待你的确认。",
    action: "确认恢复路径",
    topic: "worker.attention",
    notification_ref: "notification-ref-fixture",
    latest_fact_id: NOTIFICATION_FACT_ID,
    latest_fact_occurred_at: "2026-09-04T08:30:00Z",
    last_read_fact_id: null,
    unread_count: 1,
    version: 1,
    deep_link: deepLink,
    opened_at: "2026-09-04T08:00:00Z",
    resolved_at: null,
    archived_at: null,
    updated_at: "2026-09-04T08:30:00Z",
  };
  await page.route(`**/api/v1/workspaces/${WORKSPACE_ID}/notification-summary`, async (route) => {
    await route.fulfill({ status: 200, contentType: "application/json", json: {
      open_count: 1,
      unread_count: 1,
      progress_count: 1,
      channel_issue_count: 0,
      last_change_cursor: 42,
      recipient_type: "workspace_operator",
      recipient_id: "aitejiu",
    } });
  });
  await page.route(`**/api/v1/workspaces/${WORKSPACE_ID}/notification-threads/${NOTIFICATION_THREAD_ID}*`, async (route) => {
    await route.fulfill({ status: 200, contentType: "application/json", json: {
      thread,
      facts: {
        items: [{
          id: NOTIFICATION_FACT_ID,
          thread_id: NOTIFICATION_THREAD_ID,
          policy_version: 1,
          kind: "execution_attention",
          priority: "action",
          transition: "opened",
          topic: "worker.attention",
          root_task_id: ROOT_TASK_ID,
          subtask_id: SUBTASK_TASK_ID,
          worker_item_id: IMPLEMENTER_WORKER_ITEM_ID,
          source_projection_cursor: 40,
          source_projection_type: "worker_item_workspace",
          source_resource_id: IMPLEMENTER_WORKER_ITEM_ID,
          source_resource_version: 1,
          title: thread.title,
          summary: thread.summary,
          action: thread.action,
          deep_link: deepLink,
          correlation_id: "90000000-0000-0000-0000-000000000001",
          baseline_import: false,
          occurred_at: thread.latest_fact_occurred_at,
          created_at: thread.latest_fact_occurred_at,
        }],
        next_cursor: null,
      },
      deliveries: [],
      notification_ref: thread.notification_ref,
      available_commands: ["mark_read"],
      deep_link: deepLink,
      last_change_cursor: 42,
    } });
  });
  await page.route(`**/api/v1/workspaces/${WORKSPACE_ID}/notification-threads*`, async (route) => {
    await route.fulfill({ status: 200, contentType: "application/json", json: {
      items: [thread],
      next_cursor: null,
      last_change_cursor: 42,
    } });
  });
  await page.route(`**/api/v1/workspaces/${WORKSPACE_ID}/projection-changes*`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body: ": fixture stream\n\n",
    });
  });
}

type Fixtures = {
  seedR4: (scenario?: SeedScenario) => void;
  seedR5: (scenario?: PlanningSeedScenario) => void;
  seedR6Revision: () => void;
  seedR6Handoff: (scenario?: HandoffSeedScenario) => R6HandoffSeed;
  seedR6Collaboration: () => R6CollaborationSeed;
  seedR6Governance: () => R6CollaborationSeed;
  runR6CollaborationOnce: (workspaceId?: string) => Record<string, unknown>;
  prepareR6CollaborationBrowser: (workspaceId?: string) => Record<string, unknown>;
  runR6GovernanceOnce: (workspaceId?: string) => Record<string, unknown>;
  prepareR6GovernanceBrowser: (workspaceId?: string) => Record<string, unknown>;
  runR6GovernanceAfterPause: (workspaceId?: string) => Record<string, unknown>;
  seedR6ResultInputs: () => R6ResultInputSeed;
  prepareR6ResultInputsBrowser: (workspaceId: string) => Record<string, unknown>;
  runR6ResultInputAction: (
    workspaceId: string,
    action: R6ResultInputAction,
  ) => Record<string, unknown>;
  seedResultIntegration: () => R6ResultIntegrationSeed;
  prepareResultIntegrationBrowser: (workspaceId: string) => R6ResultIntegrationSeed;
  advanceResultIntegration: (
    workspaceId: string,
    action: R6ResultIntegrationAction,
  ) => R6ResultIntegrationSeed;
  seedKnowledge: (workspaceId?: string) => void;
};

export const test = base.extend<Fixtures>({
  seedR4: async ({ browserName }, use) => {
    void browserName;
    // Playwright fixture callbacks use `use`; the React Hooks rule does not apply here.
    // eslint-disable-next-line react-hooks/rules-of-hooks
    await use(seedR4Workspace);
  },
  seedR5: async ({ browserName }, use) => {
    void browserName;
    // Playwright fixture callbacks use `use`; the React Hooks rule does not apply here.
    // eslint-disable-next-line react-hooks/rules-of-hooks
    await use(seedR5Planning);
  },
  seedR6Revision: async ({ browserName }, use) => {
    void browserName;
    // Playwright fixture callbacks use `use`; the React Hooks rule does not apply here.
    // eslint-disable-next-line react-hooks/rules-of-hooks
    await use(seedR6Revision);
  },
  seedR6Handoff: async ({ browserName }, use) => {
    void browserName;
    // Playwright fixture callbacks use `use`; the React Hooks rule does not apply here.
    // eslint-disable-next-line react-hooks/rules-of-hooks
    await use(seedR6Handoff);
  },
  seedR6Collaboration: async ({ browserName }, use) => {
    void browserName;
    // Playwright fixture callbacks use `use`; the React Hooks rule does not apply here.
    // eslint-disable-next-line react-hooks/rules-of-hooks
    await use(seedR6Collaboration);
  },
  seedR6Governance: async ({ browserName }, use) => {
    void browserName;
    // Playwright fixture callbacks use `use`; the React Hooks rule does not apply here.
    // eslint-disable-next-line react-hooks/rules-of-hooks
    await use(seedR6Governance);
  },
  runR6CollaborationOnce: async ({ browserName }, use) => {
    void browserName;
    // Playwright fixture callbacks use `use`; the React Hooks rule does not apply here.
    // eslint-disable-next-line react-hooks/rules-of-hooks
    await use(runR6CollaborationOnce);
  },
  prepareR6CollaborationBrowser: async ({ browserName }, use) => {
    void browserName;
    // Playwright fixture callbacks use `use`; the React Hooks rule does not apply here.
    // eslint-disable-next-line react-hooks/rules-of-hooks
    await use(prepareR6CollaborationBrowser);
  },
  runR6GovernanceOnce: async ({ browserName }, use) => {
    void browserName;
    // Playwright fixture callbacks use `use`; the React Hooks rule does not apply here.
    // eslint-disable-next-line react-hooks/rules-of-hooks
    await use(runR6GovernanceOnce);
  },
  prepareR6GovernanceBrowser: async ({ browserName }, use) => {
    void browserName;
    // Playwright fixture callbacks use `use`; the React Hooks rule does not apply here.
    // eslint-disable-next-line react-hooks/rules-of-hooks
    await use(prepareR6GovernanceBrowser);
  },
  runR6GovernanceAfterPause: async ({ browserName }, use) => {
    void browserName;
    // Playwright fixture callbacks use `use`; the React Hooks rule does not apply here.
    // eslint-disable-next-line react-hooks/rules-of-hooks
    await use(runR6GovernanceAfterPause);
  },
  seedR6ResultInputs: async ({ browserName }, use) => {
    void browserName;
    const workspaceId = randomUUID();
    const root = mkdtempSync(join(tmpdir(), "sidera-r6-3-4-result-inputs-"));
    resultInputRoots.set(workspaceId, root);
    try {
      // Playwright fixture callbacks use `use`; the React Hooks rule does not apply here.
      // eslint-disable-next-line react-hooks/rules-of-hooks
      await use(() => seedR6ResultInputs(workspaceId, root));
    } finally {
      resultInputRoots.delete(workspaceId);
      rmSync(root, { recursive: true, force: true });
    }
  },
  prepareR6ResultInputsBrowser: async ({ browserName }, use) => {
    void browserName;
    // Playwright fixture callbacks use `use`; the React Hooks rule does not apply here.
    // eslint-disable-next-line react-hooks/rules-of-hooks
    await use(prepareR6ResultInputsBrowser);
  },
  runR6ResultInputAction: async ({ browserName }, use) => {
    void browserName;
    // Playwright fixture callbacks use `use`; the React Hooks rule does not apply here.
    // eslint-disable-next-line react-hooks/rules-of-hooks
    await use(runR6ResultInputActionForWorkspace);
  },
  seedResultIntegration: async ({ browserName }, use) => {
    void browserName;
    const workspaceId = randomUUID();
    const root = mkdtempSync(join(tmpdir(), "sidera-r6-result-integration-"));
    resultIntegrationRoots.set(workspaceId, root);
    try {
      // Playwright fixture callbacks use `use`; the React Hooks rule does not apply here.
      // eslint-disable-next-line react-hooks/rules-of-hooks
      await use(() => seedResultIntegration(workspaceId, root));
    } finally {
      resultIntegrationRoots.delete(workspaceId);
      rmSync(root, { recursive: true, force: true });
    }
  },
  prepareResultIntegrationBrowser: async ({ browserName }, use) => {
    void browserName;
    // Playwright fixture callbacks use `use`; the React Hooks rule does not apply here.
    // eslint-disable-next-line react-hooks/rules-of-hooks
    await use(prepareResultIntegrationBrowser);
  },
  advanceResultIntegration: async ({ browserName }, use) => {
    void browserName;
    // Playwright fixture callbacks use `use`; the React Hooks rule does not apply here.
    // eslint-disable-next-line react-hooks/rules-of-hooks
    await use(advanceResultIntegration);
  },
  seedKnowledge: async ({ browserName }, use) => {
    void browserName;
    // Playwright fixture callbacks use `use`; the React Hooks rule does not apply here.
    // eslint-disable-next-line react-hooks/rules-of-hooks
    await use(seedKnowledge);
  },
});

export { expect } from "@playwright/test";
