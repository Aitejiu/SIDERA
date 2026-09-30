import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { defineConfig, devices } from "@playwright/test";

import {
  type RealBasicGroup,
  type RealBasicManifest,
  realBasicBrowserTimeoutMs,
  validateRealBasicBrowserBoundary,
} from "./src/e2e/basicCapabilityRealBoundary.js";

const realBrowserRun = process.env.SIDERA_RUN_REAL_BASIC === "1"
  || process.env.SIDERA_RUN_REAL_BASIC_BROWSER === "1";
const realManifest = realBrowserRun ? loadRealManifest() : null;
const webURL = process.env.SIDERA_BASIC_WEB_URL
  ?? realManifest?.web_url
  ?? "http://127.0.0.1:5173";

if (realManifest !== null) {
  process.env.SIDERA_BASIC_WORKSPACE_ID ??= realManifest.workspace_id;
  process.env.SIDERA_MAIN_AGENT_GATEWAY_URL ??= realManifest.main_agent_gateway_url;
  process.env.SIDERA_BASIC_WEB_URL ??= realManifest.web_url;
}

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: realManifest === null ? 12 * 60_000 : realBasicBrowserTimeoutMs(realManifest),
  expect: { timeout: 15_000 },
  reporter: [["list"]],
  use: {
    baseURL: webURL.replace(/\/$/, ""),
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  projects: [
    {
      name: "basic-proposal-gate-local",
      testMatch: /planning-proposal-gate\.spec\.ts/,
      use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } },
    },
    {
      name: "basic-real-desktop",
      testMatch: /basic-capability-(?:b-)?real\.spec\.ts/,
      use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } },
    },
    {
      name: "basic-readonly-desktop",
      testMatch: /basic-capability-readonly\.spec\.ts/,
      use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } },
    },
    {
      name: "basic-readonly-tablet",
      testMatch: /basic-capability-readonly\.spec\.ts/,
      use: { ...devices["Desktop Chrome"], viewport: { width: 1024, height: 768 } },
    },
    {
      name: "basic-readonly-mobile",
      testMatch: /basic-capability-readonly\.spec\.ts/,
      use: { ...devices["Pixel 5"], viewport: { width: 390, height: 844 } },
    },
  ],
});

function loadRealManifest(): RealBasicManifest {
  const path = process.env.SIDERA_BASIC_REAL_MANIFEST;
  if (path === undefined || path.trim() === "") {
    throw new Error("real_manifest_required");
  }
  const manifest = readJson(path, "real_manifest_unreadable");
  const runRoot = stringField(manifest, "run_root", "real_manifest_invalid");
  const approvalRef = stringField(manifest, "approval_ref", "real_manifest_invalid");
  const approval = readJson(approvalRef.startsWith("/") ? approvalRef : resolve(runRoot, approvalRef), "real_approval_unreadable");
  const selectedGroup = process.env.SIDERA_BASIC_ACCEPTANCE_GROUP;
  const requiredGroups = requiredObservationGroups(selectedGroup);
  const groups = Object.fromEntries(
    requiredGroups.map((group) => [
      group,
      readJson(resolve(runRoot, "observations", `group-${group}.json`), "real_group_observation_unreadable"),
    ]),
  );
  return validateRealBasicBrowserBoundary({
    env: process.env,
    manifest,
    approval,
    groups,
    requiredGroups,
    selectedGroup: selectedGroup as RealBasicGroup | undefined,
  });
}

function requiredObservationGroups(value: string | undefined): readonly RealBasicGroup[] {
  if (value === undefined || value.trim() === "") {
    return ["A", "B", "C"];
  }
  if (value === "A") return [];
  if (value === "B") return ["A"];
  if (value === "C") return ["A", "B"];
  throw new Error("real_group_selection_invalid");
}

function readJson(path: string, errorCode: string): unknown {
  try {
    return JSON.parse(readFileSync(path, "utf8")) as unknown;
  } catch (error) {
    throw new Error(errorCode, { cause: error });
  }
}

function stringField(value: unknown, key: string, errorCode: string): string {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error(errorCode);
  }
  const field = (value as Record<string, unknown>)[key];
  if (typeof field !== "string" || field === "") {
    throw new Error(errorCode);
  }
  return field;
}
