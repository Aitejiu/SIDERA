import { defineConfig, devices } from "@playwright/test";
import { randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import { dirname, join } from "node:path";

import { loadOwnedManifest, readProtectedJson, realScenario, realNavigationRecovery } from "./tests/e2e/native-harness-fixtures.js";

const path = process.env.SIDERA_NATIVE_MANIFEST;
if (!path) throw new Error("native_owned_manifest_required");
const manifest = loadOwnedManifest(path, realNavigationRecovery());
if (manifest.mode !== "real") throw new Error("native_real_mode_required");
realScenario(manifest);
const parent = dirname(manifest.transport_path);
const invocation = randomUUID();
const session = join(parent, "session.json");
if (existsSync(session)) readProtectedJson(session);
process.umask(0o077);
process.env.SIDERA_NATIVE_REAL_ENTRY = "1";

export default defineConfig({
  testDir: "./tests/e2e",
  testMatch: ["native-harness-real.spec.ts"],
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 7200_000,
  expect: { timeout: 15_000 },
  outputDir: join(parent, `native-real-output-${invocation}`),
  reporter: [["json", { outputFile: join(parent, `native-real-results-${invocation}.json`) }]],
  use: {
    baseURL: manifest.base_url,
    actionTimeout: 120_000,
    navigationTimeout: 120_000,
    trace: "off",
    video: "off",
    screenshot: "off",
    ...(existsSync(session) ? { storageState: session } : {}),
  },
  projects: [{ name: "native-real-chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 960 } } }],
});
