import { mkdirSync } from "node:fs";

import { test as base, expect } from "@playwright/test";

import {
  InterviewDriver,
  prepareInterviewRun,
  readInterviewManifest,
  startInterviewRun,
  stopInterviewRun,
  type Scenario,
} from "./driver";

export type { InterviewFacts, InterviewManifest, Scenario } from "./driver";

type InterviewOptions = {
  demoScenario: Scenario;
};

type InterviewFixtures = {
  interview: InterviewDriver;
};

export const test = base.extend<InterviewFixtures & InterviewOptions>({
  demoScenario: ["mainline", { option: true }],
  interview: async ({ page, demoScenario }, provide, testInfo) => {
    const manifestPath = prepareInterviewRun(demoScenario);
    const manifest = readInterviewManifest(manifestPath);
    testInfo.annotations.push({ type: "interview-run", description: manifestPath });
    mkdirSync(testInfo.outputPath("evidence"), { recursive: true });
    let started = false;
    try {
      startInterviewRun(manifestPath);
      started = true;
      await page.goto(manifest.endpoints.web);
      const status = await page.evaluate(async (controlPlane) => {
        const response = await fetch(`${controlPlane}/api/v1/demo/status`);
        return response.ok ? response.json() as Promise<Record<string, unknown>> : null;
      }, manifest.endpoints.control_plane);
      if (status?.run_id !== manifest.run_id || status.workspace_id !== manifest.workspace_id) {
        throw new Error("interview child run identity mismatch");
      }
      await provide(new InterviewDriver(page, manifest));
    } finally {
      if (started) {
        stopInterviewRun(manifestPath);
      }
    }
  },
});

export { expect };
