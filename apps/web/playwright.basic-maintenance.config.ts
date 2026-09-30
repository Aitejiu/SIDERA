import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { defineConfig, devices } from "@playwright/test";

type MaintenanceManifest = {
  mode: string;
  schema_version: number;
  run_id: string;
  workspace_id: string;
  run_root: string;
  repository: string;
  runtime_root: string;
  database_name: string;
  knowledge_database_name: string;
  restore_mode: string;
  endpoints: {
    control_plane: string;
    knowledge: string;
    main_agent: string;
    web: string;
  };
};

const webRoot = resolve(import.meta.dirname);
const repositoryRoot = resolve(webRoot, "../..");
const controlPlaneRoot = resolve(repositoryRoot, "services/control-plane");
const python = resolve(controlPlaneRoot, ".venv/bin/python");
const maintenanceScript = resolve(
  controlPlaneRoot,
  "tests/e2e/maintenance_readonly_runtime.py",
);
const manifestPath = resolveMaintenanceManifest();
const manifest = readMaintenanceManifest(manifestPath);

assertMaintenanceInvocationBoundary(manifest);
process.env.SIDERA_BASIC_MAINTENANCE_MANIFEST = manifestPath;
process.env.SIDERA_BASIC_MAINTENANCE_WORKSPACE_ID = manifest.workspace_id;
process.env.SIDERA_E2E_CONTROL_PLANE_PORT = String(endpointPort(manifest.endpoints.control_plane));
process.env.SIDERA_E2E_KNOWLEDGE_SERVICE_PORT = String(endpointPort(manifest.endpoints.knowledge));
process.env.SIDERA_E2E_MAIN_AGENT_PORT = String(endpointPort(manifest.endpoints.main_agent));
process.env.SIDERA_E2E_WEB_PORT = String(endpointPort(manifest.endpoints.web));

const shellManifest = shellQuote(manifestPath);
const shellPython = shellQuote(python);
const safePath = shellQuote(process.env.PATH ?? "/opt/homebrew/bin:/usr/bin:/bin");
const stackEnv = [
  `env -i PATH=${safePath} LANG=C LC_ALL=C`,
  `SIDERA_E2E_CONTROL_PLANE_PORT=${endpointPort(manifest.endpoints.control_plane)}`,
  `SIDERA_E2E_KNOWLEDGE_SERVICE_PORT=${endpointPort(manifest.endpoints.knowledge)}`,
  `SIDERA_E2E_MAIN_AGENT_PORT=${endpointPort(manifest.endpoints.main_agent)}`,
  `SIDERA_E2E_WEB_PORT=${endpointPort(manifest.endpoints.web)}`,
].join(" ");
const startCommand = `${stackEnv} ${shellPython} -m sidera_control_plane.demo.cli present --manifest ${shellManifest}`;
const stopCommand = `${stackEnv} ${shellPython} -m sidera_control_plane.demo.cli stop --manifest ${shellManifest}`;

export default defineConfig({
  testDir: "./tests/e2e",
  testMatch: /basic-maintenance-readonly\.spec\.ts/,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 150_000,
  expect: { timeout: 20_000 },
  reporter: [["list"]],
  globalTeardown: resolve(
    webRoot,
    "tests/e2e/support/cleanup-maintenance-runtime.ts",
  ),
  outputDir: "test-results/basic-maintenance",
  use: {
    baseURL: manifest.endpoints.web,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
  },
  projects: [
    {
      name: "basic-maintenance-desktop",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } },
    },
    {
      name: "basic-maintenance-tablet",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1024, height: 768 } },
    },
    {
      name: "basic-maintenance-mobile",
      use: { ...devices["Pixel 5"], viewport: { width: 390, height: 844 } },
    },
  ],
  webServer: {
    command: [
      "set -eu",
      `cleanup() { ${stopCommand} >/dev/null 2>&1 || true; }`,
      "trap cleanup EXIT INT TERM",
      startCommand,
      "while :; do sleep 1; done",
    ].join("; "),
    url: `${manifest.endpoints.main_agent}/main-agent/v1/health`,
    timeout: 120_000,
    reuseExistingServer: false,
  },
});

function resolveMaintenanceManifest(): string {
  const configured = process.env.SIDERA_BASIC_MAINTENANCE_MANIFEST;
  if (configured !== undefined && configured.trim() !== "") {
    return resolve(configured);
  }
  assertNoProviderCredentials();
  const output = execFileSync(python, [maintenanceScript, "prepare"], {
    cwd: controlPlaneRoot,
    encoding: "utf8",
    env: safePreparationEnvironment(),
    stdio: ["ignore", "pipe", "pipe"],
  });
  const line = output.trim().split(/\r?\n/).at(-1);
  if (line === undefined || line.length === 0) {
    throw new Error("maintenance preparation did not return a manifest path");
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(line);
  } catch (error) {
    throw new Error("maintenance preparation returned invalid JSON", { cause: error });
  }
  if (!isRecord(parsed) || typeof parsed.manifest_path !== "string") {
    throw new Error("maintenance preparation did not return manifest_path");
  }
  return resolve(parsed.manifest_path);
}

function safePreparationEnvironment(): NodeJS.ProcessEnv {
  return {
    PATH: process.env.PATH ?? "/opt/homebrew/bin:/usr/bin:/bin",
    LANG: process.env.LANG ?? "C",
    LC_ALL: process.env.LC_ALL ?? "C",
    TMPDIR: process.env.TMPDIR,
    PGPASSFILE: process.env.PGPASSFILE,
    SIDERA_INTERVIEW_DEMO_ADMIN_DATABASE_URL:
      process.env.SIDERA_INTERVIEW_DEMO_ADMIN_DATABASE_URL,
  };
}

function readMaintenanceManifest(path: string): MaintenanceManifest {
  let parsed: unknown;
  try {
    parsed = JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    throw new Error("maintenance manifest is unreadable", { cause: error });
  }
  if (!isRecord(parsed)) {
    throw new Error("maintenance manifest is not an object");
  }
  if (Object.keys(parsed).some((key) => /api[_-]?key|secret|password|credential|authorization/i.test(key))) {
    throw new Error("maintenance manifest refuses credential-shaped fields");
  }
  const endpoints = parsed.endpoints;
  if (
    typeof parsed.mode !== "string"
    || typeof parsed.schema_version !== "number"
    || typeof parsed.run_id !== "string"
    || typeof parsed.workspace_id !== "string"
    || typeof parsed.run_root !== "string"
    || typeof parsed.repository !== "string"
    || typeof parsed.runtime_root !== "string"
    || typeof parsed.database_name !== "string"
    || typeof parsed.knowledge_database_name !== "string"
    || typeof parsed.restore_mode !== "string"
    || !isRecord(endpoints)
    || !isStringRecord(endpoints)
  ) {
    throw new Error("maintenance manifest has an invalid shape");
  }
  return {
    mode: parsed.mode,
    schema_version: parsed.schema_version,
    run_id: parsed.run_id,
    workspace_id: parsed.workspace_id,
    run_root: parsed.run_root,
    repository: parsed.repository,
    runtime_root: parsed.runtime_root,
    database_name: parsed.database_name,
    knowledge_database_name: parsed.knowledge_database_name,
    restore_mode: parsed.restore_mode,
    endpoints: {
      control_plane: endpoints.control_plane,
      knowledge: endpoints.knowledge,
      main_agent: endpoints.main_agent,
      web: endpoints.web,
    },
  };
}

function assertMaintenanceInvocationBoundary(manifest: MaintenanceManifest): void {
  assertNoProviderCredentials();
  if (manifest.mode !== "interview-demo" || manifest.schema_version !== 2) {
    throw new Error("maintenance config requires an interview-demo schema 2 manifest");
  }
  if (manifest.restore_mode !== "read_only") {
    throw new Error("maintenance config requires a read-only restored manifest");
  }
  if (isInside(repositoryRoot, manifest.run_root) || isInside(repositoryRoot, manifest.runtime_root)) {
    throw new Error("maintenance config requires restored roots outside the repository");
  }
  for (const endpoint of Object.values(manifest.endpoints)) {
    if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(endpoint)) {
      throw new Error("maintenance config requires loopback test endpoints");
    }
  }
}

function assertNoProviderCredentials(): void {
  const forbidden = [
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "DEEPSEEK_API_KEY",
    "GOOGLE_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENROUTER_API_KEY",
    "OPENCODE_API_KEY",
    "OPENCODE_CONFIG_CONTENT",
    "SIDERA_OPENCODE_CONFIG_CONTENT",
    "SIDERA_OPENCODE_EXECUTABLE",
    "SIDERA_PLANNING_OPENCODE_EXECUTABLE",
  ];
  const present = forbidden.filter((name) => {
    const value = process.env[name];
    return value !== undefined && value !== "";
  });
  if (present.length > 0) {
    throw new Error(`maintenance config refuses real credentials/config: ${present.join(",")}`);
  }
}

function isInside(parent: string, candidate: string): boolean {
  const normalizedParent = resolve(parent);
  const normalizedCandidate = resolve(candidate);
  return normalizedCandidate === normalizedParent
    || normalizedCandidate.startsWith(`${normalizedParent}/`);
}

function endpointPort(endpoint: string): number {
  const port = Number(new URL(endpoint).port);
  if (!Number.isInteger(port) || port < 1024 || port > 65_535) {
    throw new Error("maintenance endpoint port is invalid");
  }
  return port;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isStringRecord(value: Record<string, unknown>): value is Record<string, string> {
  return ["control_plane", "knowledge", "main_agent", "web"].every(
    (key) => typeof value[key] === "string" && value[key] !== "",
  );
}

function shellQuote(value: string): string {
  return `'${value.replaceAll("'", "'\\''")}'`;
}
