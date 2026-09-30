import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { defineConfig, devices } from "@playwright/test";

type FakeManifest = {
  mode: string;
  schema_version: number;
  run_id: string;
  workspace_id: string;
  run_root: string;
  database_name: string;
  pg_host: string;
  pg_port: number;
  pg_user: string;
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
const manifestPath = resolveFakeManifest();
const manifest = readFakeManifest(manifestPath);
const batchSuite = process.env.SIDERA_BASIC_FAKE_SUITE === "batch";
const projectPrefix = batchSuite ? "basic-batch" : "basic-fake";
const batchId = batchSuite ? manifest.run_id : null;

assertFakeInvocationBoundary(manifest);
bindFakeDatabase(manifest);
process.env.SIDERA_BASIC_FAKE_MANIFEST = manifestPath;
process.env.SIDERA_BASIC_FAKE_WORKSPACE_ID = manifest.workspace_id;
if (batchId !== null) {
  process.env.SIDERA_BASIC_BATCH_ID = batchId;
  process.env.SIDERA_BASIC_BATCH_STATE_FILE = resolve(
    manifest.run_root,
    "reports",
    `basic-capability-batch-${batchId}.json`,
  );
}

const shellManifest = shellQuote(manifestPath);
const shellPython = shellQuote(python);
const safePath = shellQuote(process.env.PATH ?? "/opt/homebrew/bin:/usr/bin:/bin");
const stackEnv = `env -i PATH=${safePath} LANG=C LC_ALL=C`;
const startCommand = `${stackEnv} ${shellPython} -m sidera_control_plane.demo.cli present --manifest ${shellManifest}`;
const stopCommand = `${stackEnv} ${shellPython} -m sidera_control_plane.demo.cli stop --manifest ${shellManifest}`;

export default defineConfig({
  testDir: "./tests/e2e",
  testMatch: batchSuite
    ? /basic-capability-batch\.spec\.ts/
    : /basic-capability-fake\.spec\.ts/,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 150_000,
  expect: { timeout: 20_000 },
  reporter: [["list"]],
  globalTeardown: resolve(webRoot, "tests/e2e/support/stop-basic-fake-stack.ts"),
  outputDir: `test-results/${projectPrefix}`,
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
      name: `${projectPrefix}-desktop`,
      use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } },
    },
    {
      name: `${projectPrefix}-tablet`,
      use: { ...devices["Desktop Chrome"], viewport: { width: 1024, height: 768 } },
      ...(batchSuite ? { dependencies: [`${projectPrefix}-desktop`] } : {}),
    },
    {
      name: `${projectPrefix}-mobile`,
      use: { ...devices["Pixel 5"], viewport: { width: 390, height: 844 } },
      ...(batchSuite ? { dependencies: [`${projectPrefix}-desktop`] } : {}),
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

function resolveFakeManifest(): string {
  const configured = process.env.SIDERA_BASIC_FAKE_MANIFEST;
  if (configured !== undefined && configured.trim() !== "") {
    return resolve(configured);
  }
  assertNoProviderCredentials();
  const output = execFileSync(
    python,
    ["-m", "sidera_control_plane.demo.cli", "prepare", "--scenario", "mainline", "--print-manifest-path"],
    {
      cwd: controlPlaneRoot,
      encoding: "utf8",
      env: safePreparationEnvironment(),
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  const path = output.trim().split(/\r?\n/).at(-1);
  if (path === undefined || path.length === 0) {
    throw new Error("provider-free fake preparation did not return a manifest path");
  }
  return resolve(path);
}

function safePreparationEnvironment(): NodeJS.ProcessEnv {
  const environment: NodeJS.ProcessEnv = {
    PATH: process.env.PATH ?? "/opt/homebrew/bin:/usr/bin:/bin",
    LANG: process.env.LANG ?? "C",
    LC_ALL: process.env.LC_ALL ?? "C",
    TMPDIR: process.env.TMPDIR,
    PGPASSFILE: process.env.PGPASSFILE,
    SIDERA_INTERVIEW_DEMO_ADMIN_DATABASE_URL:
      process.env.SIDERA_INTERVIEW_DEMO_ADMIN_DATABASE_URL,
  };
  return environment;
}

function readFakeManifest(path: string): FakeManifest {
  let parsed: unknown;
  try {
    parsed = JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    throw new Error("provider-free fake manifest is unreadable", { cause: error });
  }
  if (!isRecord(parsed)) {
    throw new Error("provider-free fake manifest is not an object");
  }
  if (Object.keys(parsed).some((key) => /api[_-]?key|secret|password|credential|authorization/i.test(key))) {
    throw new Error("provider-free fake manifest refuses credential-shaped fields");
  }
  const endpoints = parsed.endpoints;
  if (
    typeof parsed.mode !== "string"
    || typeof parsed.schema_version !== "number"
    || typeof parsed.run_id !== "string"
    || typeof parsed.workspace_id !== "string"
    || typeof parsed.run_root !== "string"
    || typeof parsed.database_name !== "string"
    || typeof parsed.pg_host !== "string"
    || typeof parsed.pg_port !== "number"
    || typeof parsed.pg_user !== "string"
    || !isRecord(endpoints)
    || !isStringRecord(endpoints)
  ) {
    throw new Error("provider-free fake manifest has an invalid shape");
  }
  return {
    mode: parsed.mode,
    schema_version: parsed.schema_version,
    run_id: parsed.run_id,
    workspace_id: parsed.workspace_id,
    run_root: parsed.run_root,
    database_name: parsed.database_name,
    pg_host: parsed.pg_host,
    pg_port: parsed.pg_port,
    pg_user: parsed.pg_user,
    endpoints: {
      control_plane: endpoints.control_plane,
      knowledge: endpoints.knowledge,
      main_agent: endpoints.main_agent,
      web: endpoints.web,
    },
  };
}

function bindFakeDatabase(manifest: FakeManifest): void {
  const expected = `postgresql+psycopg://${encodeURIComponent(manifest.pg_user)}@${manifest.pg_host}:${manifest.pg_port}/${manifest.database_name}`;
  const configured = process.env.SIDERA_DATABASE_URL;
  if (configured !== undefined && configured !== expected) {
    throw new Error("provider-free fake config refuses a database outside the manifest");
  }
  process.env.SIDERA_DATABASE_URL = expected;
  process.env.SIDERA_ALLOW_E2E_SEED = "1";
}

function assertFakeInvocationBoundary(manifest: FakeManifest): void {
  assertNoProviderCredentials();
  if (process.env.SIDERA_RUN_REAL_BASIC === "1") {
    throw new Error("provider-free fake config refuses SIDERA_RUN_REAL_BASIC=1");
  }
  if (manifest.mode !== "interview-demo" || manifest.schema_version !== 2) {
    throw new Error("provider-free fake config requires an interview-demo schema 2 manifest");
  }
  for (const endpoint of Object.values(manifest.endpoints)) {
    if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(endpoint)) {
      throw new Error("provider-free fake config requires loopback test endpoints");
    }
  }
  if (!manifest.run_root.includes(`${resolve(repositoryRoot, ".sidera/interview-demo")}/`)) {
    throw new Error("provider-free fake config requires a run-scoped local manifest");
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
    throw new Error(`provider-free fake config refuses real credentials/config: ${present.join(",")}`);
  }
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
