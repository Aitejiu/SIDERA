import { execFileSync } from "node:child_process";
import { resolve } from "node:path";

import { test as base } from "@playwright/test";

export const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
export const R5_WORKSPACE_ID = "20000000-0000-0000-0000-000000000002";
export const R6_1_WORKSPACE_ID = "20000000-0000-0000-0000-000000000003";
export const R6_2_WORKSPACE_ID = "20000000-0000-0000-0000-000000000004";
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

const webRoot = resolve(import.meta.dirname, "../..");
const repositoryRoot = resolve(webRoot, "../..");
const controlPlaneRoot = resolve(repositoryRoot, "services/control-plane");
const seedScript = resolve(controlPlaneRoot, "tests/e2e/seed_r4_workspace.py");
const planningSeedScript = resolve(controlPlaneRoot, "tests/e2e/seed_r5_planning.py");
const revisionSeedScript = resolve(controlPlaneRoot, "tests/e2e/seed_r6_1_revision.py");
const handoffSeedScript = resolve(controlPlaneRoot, "tests/e2e/seed_r6_2_handoff.py");
const handoffRuntimeScript = resolve(controlPlaneRoot, "tests/e2e/r6_2_fake_runtime.py");
const defaultPython = resolve(controlPlaneRoot, ".venv/bin/python");
const knowledgeServiceRoot = resolve(repositoryRoot, "services/knowledge-service");
const knowledgeSeedScript = resolve(knowledgeServiceRoot, "tests/e2e/seed_r5_3_knowledge.py");
const knowledgeDefaultPython = resolve(knowledgeServiceRoot, ".venv/bin/python");
const defaultContextPackageCutoverAt = "2026-08-27T00:00:00+08:00";

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

type Fixtures = {
  seedR4: (scenario?: SeedScenario) => void;
  seedR5: (scenario?: PlanningSeedScenario) => void;
  seedR6Revision: () => void;
  seedR6Handoff: (scenario?: HandoffSeedScenario) => R6HandoffSeed;
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
  seedKnowledge: async ({ browserName }, use) => {
    void browserName;
    // Playwright fixture callbacks use `use`; the React Hooks rule does not apply here.
    // eslint-disable-next-line react-hooks/rules-of-hooks
    await use(seedKnowledge);
  },
});

export { expect } from "@playwright/test";
