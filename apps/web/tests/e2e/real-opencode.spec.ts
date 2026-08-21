import { execFileSync } from "node:child_process";
import { resolve } from "node:path";

import { test, expect } from "./fixtures";

const defaultDatabaseUrl = "postgresql+psycopg://sidera:sidera@127.0.0.1:55432/sidera_test";

test("shows a real OpenCode execution in the browser control room", async ({ page, request }) => {
  test.setTimeout(120_000);
  test.skip(process.env.SIDERA_RUN_OPENCODE_E2E !== "1", "set SIDERA_RUN_OPENCODE_E2E=1 to run the real OpenCode path");

  const repositoryRoot = resolve(import.meta.dirname, "../../../..");
  const controlPlaneRoot = resolve(repositoryRoot, "services/control-plane");
  const python = process.env.SIDERA_E2E_PYTHON ?? resolve(controlPlaneRoot, ".venv/bin/python");
  const databaseUrl = process.env.SIDERA_DATABASE_URL ?? defaultDatabaseUrl;
  const providerEnv = {
    ...process.env,
    SIDERA_ALLOW_E2E_SEED: "1",
    SIDERA_DATABASE_URL: databaseUrl,
    SIDERA_OPENCODE_EXECUTABLE: process.env.SIDERA_OPENCODE_EXECUTABLE ?? "opencode",
    SIDERA_OPENCODE_EXPECTED_VERSION: process.env.SIDERA_OPENCODE_EXPECTED_VERSION ?? "1.18.19",
    SIDERA_RUN_OPENCODE_E2E: "1",
    SIDERA_KEEP_OPENCODE_E2E: "1",
  };
  const report = JSON.parse(execFileSync(python, ["-m", "sidera_control_plane.opencode_preflight"], {
    cwd: controlPlaneRoot,
    env: providerEnv,
    encoding: "utf8",
  })) as { status: string; capabilities?: { status?: string } };
  expect(report.status).toBe("ok");
  expect(report.capabilities?.status).toBe("available");

  const capabilities = await request.get("/api/v1/workspaces/00000000-0000-0000-0000-000000000001/capabilities");
  expect(capabilities.ok()).toBeTruthy();
  const body = await capabilities.json() as { main_agent?: { entry_uri?: unknown } };
  expect(body.main_agent?.entry_uri).toEqual(expect.any(String));

  const output = execFileSync(
    python,
    ["-m", "pytest", "-s", "tests/integration/test_opencode_end_to_end.py", "-q"],
    { cwd: controlPlaneRoot, env: providerEnv, encoding: "utf8" },
  );
  const ids = parseProviderIds(output);
  try {
    const taskPath = `/workspaces/${ids.workspace_id}/tasks/${ids.root_task_id}`;
    await page.goto(`/workspaces/${ids.workspace_id}/tasks`);
    await expect(page.getByRole("link", { name: "打开任务 OpenCode R3 E2E" })).toBeVisible();
    await page.getByRole("link", { name: "打开任务 OpenCode R3 E2E" }).click();
    await expect(page.locator("#task-detail-title")).toHaveText("OpenCode R3 E2E");
    await expect(page.locator(".inspector-evidence strong", { hasText: "the execution produces a Git diff" })).toBeVisible();
    await expect(
      page.locator(`a[href*="/executions/${ids.execution_id}/sessions/${ids.session_id}/trace"]`),
    ).toBeVisible();
    const artifactLink = page.getByRole("link", { name: "diff · text/x-diff" });
    await expect(artifactLink).toBeVisible();

    const traceResponse = await request.get(
      `/api/v1/tasks/${ids.root_task_id}/executions/${ids.execution_id}/sessions/${ids.session_id}/trace?workspace_id=${ids.workspace_id}&limit=100`,
    );
    expect(traceResponse.ok()).toBeTruthy();
    const traceBody = await traceResponse.json() as { events?: Array<{ title?: string }> };
    expect(traceBody.events?.length ?? 0).toBeGreaterThan(0);

    await artifactLink.click();
    await expect(page.locator("#artifact-page-title")).toHaveText("Artifact 查看器");
    await expect(page.locator(".artifact-meta")).toBeVisible();

    await page.goto(`${taskPath}/executions/${ids.execution_id}/sessions/${ids.session_id}/trace`);
    await expect(page.locator("#worker-trace-title")).toHaveText("Worker 轨迹");
    await expect(page.locator(".trace-row")).not.toHaveCount(0);
    await page.locator(".trace-row").last().click();
    await expect(page.locator("#trace-details-title")).toBeVisible();
  } finally {
    execFileSync(
      python,
      ["tests/e2e/cleanup_real_opencode_workspace.py"],
      { cwd: controlPlaneRoot, env: providerEnv, encoding: "utf8" },
    );
  }
});

function parseProviderIds(output: string): {
  workspace_id: string;
  root_task_id: string;
  execution_id: string;
  session_id: string;
} {
  for (const line of output.split("\n").reverse()) {
    try {
      const parsed = JSON.parse(line) as { sidera_real_opencode?: Record<string, unknown> };
      const value = parsed.sidera_real_opencode;
      if (
        value !== undefined
        && typeof value.workspace_id === "string"
        && typeof value.root_task_id === "string"
        && typeof value.execution_id === "string"
        && typeof value.session_id === "string"
      ) {
        return value as {
          workspace_id: string;
          root_task_id: string;
          execution_id: string;
          session_id: string;
        };
      }
    } catch {
      // Pytest emits progress lines around the JSON handoff record.
    }
  }
  throw new Error(`real OpenCode integration did not emit IDs:\n${output}`);
}
