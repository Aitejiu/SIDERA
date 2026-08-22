import { defineConfig, devices } from "@playwright/test";
import { resolve } from "node:path";

const webRoot = resolve(import.meta.dirname);
const repositoryRoot = resolve(webRoot, "../..");
const controlPlaneRoot = resolve(repositoryRoot, "services/control-plane");
const databaseUrl = process.env.SIDERA_DATABASE_URL
  ?? "postgresql+psycopg://sidera:sidera@127.0.0.1:55432/sidera_test";

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: false,
  workers: 1,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  timeout: 30_000,
  expect: { timeout: 8_000 },
  reporter: [["list"], ["html", { open: "never", outputFolder: "playwright-report" }]],
  outputDir: "test-results",
  snapshotPathTemplate: "{testDir}/snapshots/{projectName}/{arg}{ext}",
  use: {
    baseURL: "http://127.0.0.1:4173",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
    actionTimeout: 8_000,
    navigationTimeout: 15_000,
  },
  projects: [
    {
      name: "desktop-chromium",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } },
    },
    {
      name: "tablet-chromium",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1024, height: 768 } },
    },
    {
      name: "mobile-chromium",
      use: { ...devices["Pixel 5"], viewport: { width: 390, height: 844 } },
    },
    {
      name: "real-opencode",
      testMatch: /real-opencode\.spec\.ts/,
      use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } },
    },
  ],
  webServer: [
    {
      command: `${resolve(controlPlaneRoot, ".venv/bin/uvicorn")} sidera_control_plane.api.app:create_app --factory --host 127.0.0.1 --port 8000`,
      cwd: controlPlaneRoot,
      url: "http://127.0.0.1:8000/healthz",
      timeout: 30_000,
      reuseExistingServer: !process.env.CI,
      env: {
        SIDERA_DATABASE_URL: databaseUrl,
        SIDERA_WEB_ORIGIN: "http://127.0.0.1:4173",
        SIDERA_MAIN_AGENT_ENTRY_URI: "http://127.0.0.1:18080/main-agent",
      },
    },
    {
      command: "npm run dev -- --port 4173",
      cwd: webRoot,
      url: "http://127.0.0.1:4173/",
      timeout: 30_000,
      reuseExistingServer: !process.env.CI,
    },
  ],
});
