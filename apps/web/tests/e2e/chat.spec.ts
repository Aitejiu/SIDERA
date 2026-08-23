import { AxeBuilder } from "@axe-core/playwright";
import type { Page } from "@playwright/test";

import {
  expect,
  ROOT_TASK_ID,
  seedR4Workspace,
  test,
  WORKSPACE_ID,
} from "./fixtures";

const THREAD_ID = "main-agent-e2e-thread";
const PLANNING_REQUEST_ID = "40000000-0000-0000-0000-000000000001";
const PROFILE_ID = "50000000-0000-0000-0000-000000000001";

test.describe("Main Agent chat workspace", () => {
  test.beforeEach(() => {
    seedR4Workspace();
  });

  test("follows a task context into a closeable chat tab", async ({ page }) => {
    const gateway = await installGatewayMock(page);

    await page.goto(`/workspaces/${WORKSPACE_ID}/tasks`);
    await page.getByRole("link", { name: "打开任务 支付重构" }).click();
    await expect(page.locator("#task-detail-title")).toHaveText("支付重构");

    await page.getByRole("link", { name: "与 Main Agent 讨论" }).click();
    await expect(page).toHaveURL(
      new RegExp(`/workspaces/${WORKSPACE_ID}/chat\\?context_ref=task:${ROOT_TASK_ID}$`),
    );
    const composer = page.getByRole("textbox", { name: "给 Main Agent 的消息" });
    await expect(composer).toHaveValue(`task:${ROOT_TASK_ID}`);
    expect(gateway.createdCount()).toBe(0);

    await composer.fill("请检查支付重构当前状态");
    await page.getByRole("button", { name: "发送消息" }).click();
    await expect(page.getByText("任务状态已从控制面读取", { exact: true }).first()).toBeVisible();
    await expect(page.getByRole("link", { name: "Main Agent" })).toBeVisible();
    expect(gateway.createdCount()).toBe(1);
    expect(gateway.createdContextRefs()).toEqual([`task:${ROOT_TASK_ID}`]);

    await auditChatSurface(page);

    const tabs = page.getByRole("navigation", { name: "已打开的任务" });
    await tabs.locator(".workspace-tab").filter({ has: page.locator(".workspace-tab__close") }).first().getByRole("link").click();
    await expect(page.locator("#task-detail-title")).toHaveText("支付重构");
    await tabs.getByRole("link", { name: "Main Agent" }).click();
    await expect(page.getByText("任务状态已从控制面读取", { exact: true }).first()).toBeVisible();
    await tabs.getByRole("button", { name: "关闭Main Agent" }).click();
    await expect(page).toHaveURL(new RegExp(`/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}$`));
    await expect(page.locator("#task-detail-title")).toHaveText("支付重构");
  });

  test("keeps Planning and Chat tabs navigable after a planning discussion", async ({ page }) => {
    await installGatewayMock(page);
    await installPlanningMock(page);

    await page.goto(`/workspaces/${WORKSPACE_ID}/planning/${PLANNING_REQUEST_ID}`);
    await expect(page.locator("#planning-request-title")).toHaveText("规划请求");
    await page.getByRole("link", { name: "与 Main Agent 讨论" }).click();
    await expect(page.getByRole("textbox", { name: "给 Main Agent 的消息" })).toHaveValue(
      `planning_request:${PLANNING_REQUEST_ID}`,
    );
    await page.getByRole("textbox", { name: "给 Main Agent 的消息" }).fill("请解释当前规划状态");
    await page.getByRole("button", { name: "发送消息" }).click();
    await expect(page.getByText("规划状态已从控制面读取", { exact: true }).first()).toBeVisible();

    await auditChatSurface(page);

    const tabs = page.getByRole("navigation", { name: "已打开的任务" });
    await tabs.getByRole("link", { name: /规划 40000000/ }).click();
    await expect(page.locator("#planning-request-title")).toHaveText("规划请求");
    await tabs.getByRole("link", { name: "Main Agent" }).click();
    await expect(page.getByText("规划状态已从控制面读取", { exact: true }).first()).toBeVisible();
    await tabs.getByRole("button", { name: "关闭Main Agent" }).click();
    await expect(page).toHaveURL(new RegExp(`/workspaces/${WORKSPACE_ID}/planning/${PLANNING_REQUEST_ID}$`));
    await expect(page.locator("#planning-request-title")).toHaveText("规划请求");
  });

  test("keeps the task control plane usable when Main Agent is offline", async ({ page }) => {
    await installGatewayMock(page, "unavailable");

    await page.goto(`/workspaces/${WORKSPACE_ID}/chat`);
    await expect(page.getByRole("alert")).toContainText("Main Agent 暂不可用");
    await page.getByRole("link", { name: "返回任务" }).click();
    await expect(page.locator("#workspace-title")).toHaveText("任务");
    await expect(page.getByRole("link", { name: "打开任务 支付重构" })).toBeVisible();
  });
});

type GatewayMock = {
  createdCount: () => number;
  createdContextRefs: () => string[];
};

async function installGatewayMock(page: Page, provider: "available" | "unavailable" = "available"): Promise<GatewayMock> {
  let created = 0;
  let contextRefs: string[] = [];
  let lastTurn: ReturnType<typeof turn> | null = null;
  await page.route("**/main-agent/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());

    if (url.pathname.endsWith("/health")) {
      await json(route, { status: "ok", service: "sidera-main-agent-gateway", provider });
      return;
    }
    if (url.pathname.endsWith("/capabilities")) {
      await json(route, {
        thread_list: true,
        thread_create: true,
        thread_read: true,
        thread_rename: false,
        turn_stream: true,
        turn_interrupt: true,
        stream_resume_after_gateway_restart: false,
      });
      return;
    }
    if (url.pathname.endsWith("/threads") && request.method() === "GET") {
      await json(route, { data: created === 0 ? [] : [thread(contextRefs)], nextCursor: null });
      return;
    }
    if (url.pathname.endsWith("/threads") && request.method() === "POST") {
      const body = request.postDataJSON() as { contextRefs?: string[]; context_refs?: string[] };
      contextRefs = body.contextRefs ?? body.context_refs ?? [];
      created += 1;
      await json(route, thread(contextRefs), 201);
      return;
    }
    if (url.pathname.endsWith(`/threads/${THREAD_ID}`) && request.method() === "GET") {
      await json(route, thread(contextRefs, lastTurn === null ? [] : [lastTurn]));
      return;
    }
    if (url.pathname.endsWith(`/threads/${THREAD_ID}/turns/main-agent-e2e-turn`) && request.method() === "GET") {
      const isPlanning = contextRefs[0]?.startsWith("planning_request:") === true;
      await json(route, turn(isPlanning ? "规划状态已从控制面读取" : "任务状态已从控制面读取"));
      return;
    }
    if (url.pathname.endsWith(`/threads/${THREAD_ID}/turns`) && request.method() === "POST") {
      const isPlanning = contextRefs[0]?.startsWith("planning_request:") === true;
      lastTurn = turn(isPlanning ? "规划状态已从控制面读取" : "任务状态已从控制面读取");
      await json(route, lastTurn, 202);
      return;
    }
    await route.continue();
  });
  return {
    createdCount: () => created,
    createdContextRefs: () => contextRefs,
  };
}

async function installPlanningMock(page: Page): Promise<void> {
  await page.route(`**/api/v1/workspaces/${WORKSPACE_ID}/planning-requests/${PLANNING_REQUEST_ID}/**`, async (route) => {
    const suffix = new URL(route.request().url()).pathname.split(`/planning-requests/${PLANNING_REQUEST_ID}/`)[1];
    if (suffix === "runs") {
      await json(route, { items: [] });
      return;
    }
    if (suffix === "briefs") {
      await json(route, {
        items: [{
          planning_request_id: PLANNING_REQUEST_ID,
          revision: 1,
          objective: "验证规划请求与 Main Agent 的上下文跳转",
          constraints: [],
          acceptance_criteria: ["能够返回规划请求页面"],
          context_refs: [],
          requested_outputs: ["状态解释"],
          user_feedback: null,
          source_correlation: "60000000-0000-0000-0000-000000000001",
          created_by: "e2e",
          created_at: "2026-08-24T04:00:00Z",
        }],
      });
      return;
    }
    if (suffix === "clarifications") {
      await json(route, { items: [] });
      return;
    }
    await route.continue();
  });
  await page.route(`**/api/v1/workspaces/${WORKSPACE_ID}/planning-requests/${PLANNING_REQUEST_ID}`, async (route) => {
    await json(route, planningRequest());
  });
}

function thread(contextRefs: string[], turns: ReturnType<typeof turn>[] = []) {
  return {
    id: THREAD_ID,
    title: "Main Agent 讨论",
    createdAt: "2026-08-24T04:00:00Z",
    updatedAt: "2026-08-24T04:00:03Z",
    contextRefs,
    turns,
  };
}

function turn(summary: string) {
  return {
    id: "main-agent-e2e-turn",
    threadId: THREAD_ID,
    status: "completed",
    createdAt: "2026-08-24T04:00:01Z",
    startedAt: "2026-08-24T04:00:01Z",
    completedAt: "2026-08-24T04:00:03Z",
    finalResponse: summary,
    items: [
      { id: "e2e-user", type: "userMessage", data: { content: "用户请求" } },
      {
        id: "e2e-tool",
        type: "toolCall",
        data: {
          name: "sidera_get_context_status",
          resultPreview: JSON.stringify({
            outcome: "ok",
            summary,
            resource_ref: `task:${ROOT_TASK_ID}`,
            web_url: `/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}`,
            data: {},
          }),
        },
      },
      { id: "e2e-assistant", type: "assistantMessage", data: { content: summary } },
    ],
    usage: { inputTokens: 10, outputTokens: 12 },
    error: null,
    streamAvailable: false,
    recoveryMode: "poll",
  };
}

function planningRequest() {
  return {
    active_run: null,
    created_at: "2026-08-24T04:00:00Z",
    current_brief_revision: 1,
    current_proposal_content_hash: null,
    current_proposal_revision: null,
    current_run_id: null,
    id: PLANNING_REQUEST_ID,
    kind: "initial",
    last_change_cursor: 1,
    pending_clarifications: [],
    planning_profile_id: PROFILE_ID,
    planning_profile_version: 1,
    profile: {
      capabilities: {
        cancellation: "process",
        clarification: true,
        idempotency: "none",
        interface_version: "1.0",
        max_context_bytes: 200000,
        proposal_schema_versions: [1],
        stability: "stable",
        structured_output: "json_schema",
        usage_reporting: "tokens",
      },
      created_at: "2026-08-24T04:00:00Z",
      display_name: "默认规划器",
      id: PROFILE_ID,
      planner_interface_version: "1.0",
      planner_module_config_version: 1,
      planner_module_instance_id: "70000000-0000-0000-0000-000000000001",
      planner_provider: "opencode",
      profile_key: "default-opencode",
      status: "active",
      user_selectable: true,
      version: 1,
      workspace_id: WORKSPACE_ID,
    },
    profile_key: "default-opencode",
    requires_attention: false,
    source_ref: null,
    source_type: "web",
    status: "planning",
    updated_at: "2026-08-24T04:00:03Z",
    usage: {
      context_bytes: 100,
      cost_microunits: 2,
      input_tokens: 10,
      output_tokens: 12,
      runtime_seconds: 1,
    },
    version: 1,
    workspace_id: WORKSPACE_ID,
  };
}

async function auditChatSurface(page: Page): Promise<void> {
  const result = await new AxeBuilder({ page }).analyze();
  expect(result.violations.filter((violation) => violation.impact === "serious" || violation.impact === "critical")).toEqual([]);

  const viewportWidth = page.viewportSize()?.width ?? 0;
  const layout = await page.evaluate(() => {
    const visible = (element: Element): element is HTMLElement => {
      const node = element as HTMLElement;
      const rect = node.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0 && getComputedStyle(node).visibility !== "hidden";
    };
    const textNodes = Array.from(document.querySelectorAll<HTMLElement>(
      ".chat-page h1, .chat-page h2, .chat-page p, .chat-page code, .chat-page button, .chat-page a",
    )).filter(visible);
    const overlaps: string[] = [];
    for (let leftIndex = 0; leftIndex < textNodes.length; leftIndex += 1) {
      const left = textNodes[leftIndex]!;
      const leftRect = left.getBoundingClientRect();
      for (let rightIndex = leftIndex + 1; rightIndex < textNodes.length; rightIndex += 1) {
        const right = textNodes[rightIndex]!;
        if (left.contains(right) || right.contains(left)) continue;
        const rightRect = right.getBoundingClientRect();
        if (leftRect.left < rightRect.right && leftRect.right > rightRect.left
          && leftRect.top < rightRect.bottom && leftRect.bottom > rightRect.top) {
          overlaps.push(`${left.tagName}:${left.textContent ?? ""} <> ${right.tagName}:${right.textContent ?? ""}`);
        }
      }
    }
    return {
      documentWidth: document.documentElement.scrollWidth,
      overlaps,
    };
  });
  expect(layout.documentWidth).toBeLessThanOrEqual(viewportWidth);
  expect(layout.overlaps).toEqual([]);
}

async function json(route: Parameters<Parameters<Page["route"]>[1]>[0], body: unknown, status = 200): Promise<void> {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}
