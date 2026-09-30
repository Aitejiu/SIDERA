import { defineConfig, devices } from "@playwright/test";

const parentManifest = process.env.SIDERA_INTERVIEW_DEMO_MANIFEST;
if (parentManifest === undefined || parentManifest.trim() === "") {
  throw new Error("SIDERA_INTERVIEW_DEMO_MANIFEST is required for interview tests");
}

export default defineConfig({
  testDir: "./tests/interview",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 180_000,
  expect: { timeout: 15_000 },
  reporter: [["./tests/interview/coverage-reporter.ts"], ["list"]],
  outputDir: "test-results-interview",
  use: {
    baseURL: "http://127.0.0.1",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
  },
  projects: [
    {
      name: "desktop",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } },
    },
    {
      name: "tablet",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1024, height: 768 } },
    },
    {
      name: "mobile",
      use: { ...devices["Pixel 5"], viewport: { width: 390, height: 844 } },
    },
  ],
});

void parentManifest;
