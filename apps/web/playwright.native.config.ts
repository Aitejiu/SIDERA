import { defineConfig, devices } from "@playwright/test";
import { existsSync } from "node:fs";
import { dirname, join } from "node:path";

import { loadOwnedManifest, readProtectedJson } from "./tests/e2e/native-harness-fixtures.js";

const path = process.env.SIDERA_NATIVE_MANIFEST;
if (!path) throw new Error("native_owned_manifest_required");
const manifest = loadOwnedManifest(path, process.env.SIDERA_NATIVE_RESUME === "1");
if (manifest.mode !== "fake") throw new Error("native_provider_mode_not_authorized");
const session = join(dirname(manifest.transport_path), "session.json");
if (existsSync(session)) readProtectedJson(session);

export default defineConfig({
  testDir: "./tests/e2e",
  testMatch: ["native-harness.spec.ts", "native-harness-resume.spec.ts"],
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 3600_000,
  expect: { timeout: 15_000 },
  outputDir: join(dirname(manifest.transport_path), "desktop-output"),
  reporter: [["line"]],
  use: {
    baseURL: manifest.base_url,
    actionTimeout: 120_000,
    trace: "off",
    video: "off",
    screenshot: "off",
    ...(existsSync(session) ? { storageState: session } : {}),
  },
  projects: [{ name: "native-chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } }],
});
