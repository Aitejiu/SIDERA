import { execFileSync } from "node:child_process";
import { realpathSync } from "node:fs";

import type { Page } from "@playwright/test";

import { expect, test } from "./fixtures";

const REAL_RUN = process.env.SIDERA_RUN_REAL_MAIN_AGENT === "1";
const DEFAULT_TIMEOUT_SECONDS = 300;
const SECRET_FIELD_PATTERN = /(?:akashic|workspace[_-]?token|provider[_-]?token|authorization)\s*[:=]/i;

type RealConfig = {
  workspaceId: string;
  gatewayUrl: string;
  controlPlaneUrl: string;
  timeoutMs: number;
  helperPath: string;
};

test.describe("real Main Agent Web journey", () => {
  test("completes Chat -> Planning -> Task -> WorkerItem -> Chat without provider leakage", async ({ page, request }) => {
    test.skip(!REAL_RUN, "SIDERA_RUN_REAL_MAIN_AGENT is not enabled");
    test.setTimeout(12 * 60_000);

    const config = readRealConfig();
    const guard = installBrowserNetworkGuard(page);
    const marker = `SIDERA-R5-2-WEB-${Date.now().toString(36)}`;
    const health = await request.get(`${config.gatewayUrl}/main-agent/v1/health`);
    expect(health.ok()).toBeTruthy();
    expect(((await health.json()) as { provider?: string }).provider).toBe("available");

    await page.goto(`/workspaces/${config.workspaceId}/chat`);
    await assertBrowserBoundary(page, guard);
    await expect(page.getByText("Gateway ready", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "新建对话" }).first().click();
    await expect(page).toHaveURL(new RegExp(`/workspaces/${escapeRegExp(config.workspaceId)}/chat/[^/?]+$`));

    const composer = page.getByRole("textbox", { name: "给 Main Agent 的消息" });
    await composer.fill(
      `请创建一个真实的 SIDERA 任务，唯一标记是 ${marker}。目标是交付一个小型可验证功能；`
      + "约束是当前工作区内的最小执行架构；验收标准是结果、测试和回滚说明；"
      + "请先形成 Proposal，得到确认后再物化 Task。",
    );
    await page.getByRole("button", { name: "发送消息" }).click();

    const briefCard = page.locator(".tool-result-card").filter({ hasText: "sidera_submit_brief" });
    await expect(briefCard).toBeVisible({ timeout: config.timeoutMs });
    const planningLink = briefCard.getByRole("link", { name: /查看对象/ });
    await expect(planningLink).toBeVisible();
    await expect(planningLink).toHaveAttribute("href", /\/workspaces\/[^/]+\/planning\/[^/]+$/);
    await planningLink.click();

    await expect(page.locator("#planning-request-title")).toHaveText("规划请求", { timeout: config.timeoutMs });
    const tabs = page.getByRole("navigation", { name: "已打开的任务" });
    await expect(tabs.getByRole("link", { name: "Main Agent" })).toBeVisible();
    await completePlanningReview(page, config.timeoutMs);

    const rootTaskMatch = new URL(page.url()).pathname.match(/\/tasks\/([^/]+)$/);
    expect(rootTaskMatch?.[1]).toMatch(/^[0-9a-f-]{36}$/i);
    const rootTaskId = rootTaskMatch![1]!;
    await expect(page.locator("#task-detail-title")).toBeVisible({ timeout: config.timeoutMs });
    await expect(page.getByRole("button", { name: /打开子任务：/ }).first()).toBeVisible({ timeout: config.timeoutMs });
    await page.getByRole("button", { name: /打开子任务：/ }).first().click();
    await expect(page.locator("#subtask-title")).toBeVisible({ timeout: config.timeoutMs });

    const workerButton = page.getByRole("button", { name: /打开 Worker：/ }).first();
    await expect(workerButton).toBeVisible({ timeout: config.timeoutMs });
    await workerButton.click();
    await expect(page.locator("#worker-item-title")).toBeVisible({ timeout: config.timeoutMs });
    await expect(tabs.getByText(/WorkerItem/).first()).toBeVisible();

    const traceButton = page.getByRole("button", { name: "查看 Agent Trace" }).first();
    if (await traceButton.count() > 0 && await traceButton.isVisible()) {
      await traceButton.click();
      const traceDialog = page.getByRole("dialog", { name: "Agent Trace" });
      await expect(traceDialog).toBeVisible({ timeout: config.timeoutMs });
      await expect(traceDialog.locator(".trace-row")).not.toHaveCount(0, { timeout: config.timeoutMs });
      await traceDialog.getByRole("button", { name: "关闭 Agent Trace" }).click();
      await expect(traceDialog).toHaveCount(0);
    }

    await tabs.getByRole("link", { name: "Main Agent" }).click();
    const mainAgentComposer = page.getByRole("textbox", { name: "给 Main Agent 的消息" });
    await expect(mainAgentComposer).toBeVisible();
    await expect(mainAgentComposer).toBeEnabled({ timeout: config.timeoutMs });
    await mainAgentComposer.fill(
      `请读取任务 ${rootTaskId} 当前状态，并返回下一步。`,
    );
    await page.getByRole("button", { name: "发送消息" }).click();
    const statusCard = page.locator(".tool-result-card").filter({ hasText: "sidera_get_task_status" });
    await expect(statusCard).toBeVisible({ timeout: config.timeoutMs });
    await expect(statusCard).toContainText(rootTaskId);
    await assertBrowserBoundary(page, guard);
  });

  test("recovers an active turn after a Gateway restart without replaying the input", async ({ page }) => {
    test.skip(!REAL_RUN, "SIDERA_RUN_REAL_MAIN_AGENT is not enabled");
    test.setTimeout(8 * 60_000);

    const config = readRealConfig();
    const guard = installBrowserNetworkGuard(page);
    const turnRequests: string[] = [];
    page.on("request", (request) => {
      if (request.method() === "POST" && /\/main-agent\/v1\/threads\/[^/]+\/turns$/.test(new URL(request.url()).pathname)) {
        turnRequests.push(request.postData() ?? "");
      }
    });

    await page.goto(`/workspaces/${config.workspaceId}/chat`);
    await page.getByRole("button", { name: "新建对话" }).first().click();
    await expect(page).toHaveURL(new RegExp(`/workspaces/${escapeRegExp(config.workspaceId)}/chat/[^/?]+$`));
    await page.getByRole("textbox", { name: "给 Main Agent 的消息" }).fill(
      `请处理一个需要较长执行时间的真实任务，唯一标记是 SIDERA-R5-2-RESTART-${Date.now().toString(36)}；`
      + "先分析需求和控制面状态，再返回结果，不要重复创建任务。",
    );
    await page.getByRole("button", { name: "发送消息" }).click();
    await expect(page.getByText("TURN ACTIVE", { exact: true })).toBeVisible({ timeout: config.timeoutMs });
    expect(turnRequests).toHaveLength(1);

    execFileSync(config.helperPath, ["restart-gateway"], { stdio: "pipe", timeout: 30_000 });
    await page.reload();
    const recoveryNotice = page.getByRole("status", { name: "对话恢复状态" });
    const terminalTurn = page.locator(".chat-turn").last();
    let sawRecoveryNotice = false;
    await expect.poll(
      async () => {
        if (await recoveryNotice.isVisible().catch(() => false)) {
          sawRecoveryNotice = true;
          return "recovering";
        }
        const turnClass = (await terminalTurn.getAttribute("class")) ?? "";
        return /chat-turn--(?:completed|failed|interrupted|cancelled)/.test(turnClass)
          ? "terminal"
          : "waiting";
      },
      { timeout: config.timeoutMs, intervals: [1_000, 2_000, 5_000] },
    ).toMatch(/recovering|terminal/);
    if (sawRecoveryNotice) {
      await expect(recoveryNotice).toContainText("不会自动重发");
    }
    await expect.poll(
      async () => (await terminalTurn.getAttribute("class")) ?? "",
      { timeout: config.timeoutMs, intervals: [1_000, 2_000, 5_000] },
    ).toMatch(/chat-turn--(?:completed|failed|interrupted|cancelled)/);
    const terminalStatus = await terminalTurn.locator(".chat-turn__status").textContent();
    if (!sawRecoveryNotice && terminalStatus !== "已完成") {
      await expect(page.getByRole("button", { name: "重试" })).toBeVisible();
    }
    expect(turnRequests).toHaveLength(1);
    await assertBrowserBoundary(page, guard);
  });
});

async function completePlanningReview(page: Page, timeoutMs: number): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const taskUrl = /\/workspaces\/[^/]+\/tasks\/[0-9a-f-]{36}$/i.test(new URL(page.url()).pathname);
    if (taskUrl) return;

    const submitClarifications = page.getByRole("button", { name: "提交澄清答复" });
    if (await submitClarifications.count() > 0 && await submitClarifications.isVisible()) {
      const answerFields = page.locator('textarea[aria-label="澄清答复"]');
      for (let index = 0; index < await answerFields.count(); index += 1) {
        await answerFields.nth(index).fill("采用最小风险默认值，保持现有 Worker 上下文并继续规划。");
      }
      const choiceFields = page.locator(".planning-clarification-field select");
      for (let index = 0; index < await choiceFields.count(); index += 1) {
        if (await choiceFields.nth(index).locator("option").count() > 1) {
          await choiceFields.nth(index).selectOption({ index: 1 });
        }
      }
      await expect(submitClarifications).toBeEnabled();
      await submitClarifications.click();
      await page.waitForTimeout(500);
      continue;
    }

    const confirm = page.getByRole("button", { name: "确认并创建任务" });
    if (await confirm.count() > 0 && await confirm.isVisible()) {
      await confirm.click();
      await expect(page).toHaveURL(/\/workspaces\/[^/]+\/tasks\/[0-9a-f-]{36}$/i, { timeout: timeoutMs });
      return;
    }

    await page.waitForTimeout(1_000);
  }
  throw new Error("real planning review did not reach the confirmation gate");
}

function readRealConfig(): RealConfig {
  const required = [
    "SIDERA_WORKSPACE_ID",
    "SIDERA_MAIN_AGENT_GATEWAY_URL",
    "SIDERA_CONTROL_PLANE_URL",
    "SIDERA_REAL_GATE_HELPER",
  ] as const;
  const missing = required.filter((key) => !process.env[key]);
  if (missing.length > 0) {
    throw new Error(`real Web gate configuration is missing: ${missing.join(", ")}`);
  }
  const timeoutSeconds = Number(process.env.SIDERA_REAL_MAIN_AGENT_TIMEOUT_SECONDS ?? DEFAULT_TIMEOUT_SECONDS);
  if (!Number.isFinite(timeoutSeconds) || timeoutSeconds <= 0) {
    throw new Error("SIDERA_REAL_MAIN_AGENT_TIMEOUT_SECONDS must be a positive number");
  }
  const helperPath = realpathSync(process.env.SIDERA_REAL_GATE_HELPER!);
  return {
    workspaceId: process.env.SIDERA_WORKSPACE_ID!,
    gatewayUrl: process.env.SIDERA_MAIN_AGENT_GATEWAY_URL!.replace(/\/$/, ""),
    controlPlaneUrl: process.env.SIDERA_CONTROL_PLANE_URL!.replace(/\/$/, ""),
    timeoutMs: timeoutSeconds * 1_000,
    helperPath,
  };
}

function installBrowserNetworkGuard(page: Page): { violations: string[] } {
  const guard = { violations: [] as string[] };
  page.on("request", (request) => {
    if (!request.url().includes("/main-agent/")) return;
    const headerNames = Object.keys(request.headers()).join(" ");
    const body = request.postData() ?? "";
    if (SECRET_FIELD_PATTERN.test(headerNames) || SECRET_FIELD_PATTERN.test(body)) {
      guard.violations.push("provider credential field in browser request");
    }
  });
  return guard;
}

async function assertBrowserBoundary(page: Page, guard: { violations: string[] }): Promise<void> {
  const storage = await page.evaluate(() => {
    const entries = (source: Storage) => Array.from({ length: source.length }, (_, index) => {
      const key = source.key(index) ?? "";
      return `${key}=${source.getItem(key) ?? ""}`;
    });
    return [...entries(window.localStorage), ...entries(window.sessionStorage)].join("\n");
  });
  expect(guard.violations).toEqual([]);
  expect(SECRET_FIELD_PATTERN.test(storage)).toBeFalsy();
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
