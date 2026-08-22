import { execFileSync } from "node:child_process";
import { resolve } from "node:path";

import { test as base } from "@playwright/test";

export const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
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

const webRoot = resolve(import.meta.dirname, "../..");
const repositoryRoot = resolve(webRoot, "../..");
const controlPlaneRoot = resolve(repositoryRoot, "services/control-plane");
const seedScript = resolve(controlPlaneRoot, "tests/e2e/seed_r4_workspace.py");
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

type Fixtures = {
  seedR4: (scenario?: SeedScenario) => void;
};

export const test = base.extend<Fixtures>({
  seedR4: async ({ browserName }, use) => {
    void browserName;
    // Playwright fixture callbacks use `use`; the React Hooks rule does not apply here.
    // eslint-disable-next-line react-hooks/rules-of-hooks
    await use(seedR4Workspace);
  },
});

export { expect } from "@playwright/test";
