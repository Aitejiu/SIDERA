import { execFileSync } from "node:child_process";
import { resolve } from "node:path";

import { test as base } from "@playwright/test";

export const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
export const R5_WORKSPACE_ID = "20000000-0000-0000-0000-000000000002";
export const ROOT_TASK_ID = "30000000-0000-0000-0000-000000000001";
export const SECOND_ROOT_TASK_ID = "30000000-0000-0000-0000-000000000005";
export const IMPLEMENTER_WORKER_ITEM_ID = "a1000000-0000-0000-0000-000000000001";
export const TESTER_WORKER_ITEM_ID = "a1000000-0000-0000-0000-000000000002";
export const REVIEWER_WORKER_ITEM_ID = "a1000000-0000-0000-0000-000000000003";
export const EXECUTION_ID = "60000000-0000-0000-0000-000000000001";
export const SCALE_EXECUTION_ID = "60000000-0000-0000-0000-000000000002";
export const SCALE_WORKER_ITEM_ID = "a1000000-0000-0000-0000-000000000101";
export const SESSION_ID = "70000000-0000-0000-0000-000000000001";
export const SCALE_SESSION_ID = "70000000-0000-0000-0000-000000000002";

export type SeedScenario = "task-first" | "scale";
export type PlanningSeedScenario = "proposal" | "clarification" | "scale" | "real";

const webRoot = resolve(import.meta.dirname, "../..");
const repositoryRoot = resolve(webRoot, "../..");
const controlPlaneRoot = resolve(repositoryRoot, "services/control-plane");
const seedScript = resolve(controlPlaneRoot, "tests/e2e/seed_r4_workspace.py");
const planningSeedScript = resolve(controlPlaneRoot, "tests/e2e/seed_r5_planning.py");
const defaultPython = resolve(controlPlaneRoot, ".venv/bin/python");

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

export function runPlanningWorkerOnce(): Record<string, unknown> {
  return runControlPlaneOnce("sidera_control_plane.planning_worker");
}

export function runActivationWorkerOnce(): Record<string, unknown> {
  return runControlPlaneOnce("sidera_control_plane.activation_worker");
}

export function runSchedulerOnce(): Record<string, unknown> {
  return runControlPlaneOnce("sidera_control_plane.scheduler_worker");
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
  return {
    ...process.env,
    SIDERA_ALLOW_E2E_SEED: "1",
    SIDERA_DATABASE_URL: process.env.SIDERA_DATABASE_URL
      ?? "postgresql+psycopg://sidera:sidera@127.0.0.1:55432/sidera_test",
  };
}

type Fixtures = {
  seedR4: (scenario?: SeedScenario) => void;
  seedR5: (scenario?: PlanningSeedScenario) => void;
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
});

export { expect } from "@playwright/test";
