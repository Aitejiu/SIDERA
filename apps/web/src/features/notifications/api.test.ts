import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { ApiProblem } from "@/shared/api/client";
import { server } from "@/test/server";

import {
  buildArchiveNotificationThreadCommand,
  buildMarkNotificationThreadReadCommand,
  buildRequeueNotificationDeliveryCommand,
  executeNotificationCommand,
  getNotificationReadiness,
  getNotificationSummary,
  getNotificationThread,
  listNotificationDeliveryAttempts,
  listNotificationModules,
  listNotificationThreads,
} from "./api";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const THREAD_ID = "30000000-0000-0000-0000-000000000001";
const DELIVERY_ID = "40000000-0000-0000-0000-000000000001";
const LATEST_FACT_ID = "50000000-0000-0000-0000-000000000001";

describe("notification API", () => {
  it("reads summary, category pages, detail, attempts, readiness, and modules through typed routes", async () => {
    const summary = {
      open_count: 2,
      unread_count: 1,
      progress_count: 3,
      channel_issue_count: 1,
      last_change_cursor: 19,
      recipient_type: "workspace_operator",
      recipient_id: "aitejiu",
    };
    const page = { items: [], next_cursor: "cursor-next", last_change_cursor: 20 };
    const detail = {
      thread: { id: THREAD_ID },
      facts: { items: [], next_cursor: "fact-next" },
      deliveries: [],
      notification_ref: "notification-ref-1",
      available_commands: ["mark_read"],
      deep_link: `/workspaces/${WORKSPACE_ID}/tasks/task-1`,
      last_change_cursor: 20,
    };
    const attempts = { items: [], next_cursor: null };
    const readiness = {
      component: "notification_gateway",
      as_of: "2026-09-04T08:00:00Z",
      status: "ready",
      checks: [],
    };
    const modules = [{
      id: "60000000-0000-0000-0000-000000000001",
      instance_name: "akashic-private",
      provider: "akashic",
      module_type: "notification_channel",
      config_version: 1,
      definition_lifecycle: "active",
      instance_lifecycle: "active",
    }];

    server.use(
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/notification-summary`, () =>
        HttpResponse.json(summary)),
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/notification-threads`, ({ request }) => {
        const url = new URL(request.url);
        expect(url.searchParams.get("category")).toBe("progress");
        expect(url.searchParams.get("root_task_id")).toBe("task-1");
        expect(url.searchParams.get("subtask_id")).toBe("subtask-1");
        expect(url.searchParams.get("unread_only")).toBe("true");
        expect(url.searchParams.get("cursor")).toBe("opaque/cursor?2");
        expect(url.searchParams.get("limit")).toBe("25");
        return HttpResponse.json(page);
      }),
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/notification-threads/${THREAD_ID}`, ({ request }) => {
        const url = new URL(request.url);
        expect(url.searchParams.get("cursor")).toBe("fact/cursor?3");
        expect(url.searchParams.get("limit")).toBe("10");
        return HttpResponse.json(detail);
      }),
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/notification-deliveries/${DELIVERY_ID}/attempts`, ({ request }) => {
        const url = new URL(request.url);
        expect(url.searchParams.get("cursor")).toBe("attempt/cursor?4");
        expect(url.searchParams.get("limit")).toBe("5");
        return HttpResponse.json(attempts);
      }),
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/notification-readiness`, () =>
        HttpResponse.json(readiness)),
      http.get("/api/v1/modules", ({ request }) => {
        const url = new URL(request.url);
        expect(url.searchParams.get("workspace_id")).toBe(WORKSPACE_ID);
        expect(url.searchParams.get("module_type")).toBe("notification_channel");
        return HttpResponse.json(modules);
      }),
    );

    await expect(getNotificationSummary(WORKSPACE_ID)).resolves.toEqual(summary);
    await expect(listNotificationThreads(WORKSPACE_ID, {
      category: "progress",
      rootTaskId: "task-1",
      subtaskId: "subtask-1",
      unreadOnly: true,
      cursor: "opaque/cursor?2",
      limit: 25,
    })).resolves.toEqual(page);
    await expect(getNotificationThread(WORKSPACE_ID, THREAD_ID, {
      cursor: "fact/cursor?3",
      limit: 10,
    })).resolves.toEqual(detail);
    await expect(listNotificationDeliveryAttempts(WORKSPACE_ID, DELIVERY_ID, {
      cursor: "attempt/cursor?4",
      limit: 5,
    })).resolves.toEqual(attempts);
    await expect(getNotificationReadiness(WORKSPACE_ID)).resolves.toEqual(readiness);
    await expect(listNotificationModules(WORKSPACE_ID)).resolves.toEqual(modules);
  });

  it("builds exact local commands with stable identity and opaque target versions", () => {
    const markRead = buildMarkNotificationThreadReadCommand({
      workspaceId: WORKSPACE_ID,
      threadId: THREAD_ID,
      expectedVersion: 7,
      expectedLatestFactId: LATEST_FACT_ID,
    });
    const archive = buildArchiveNotificationThreadCommand({
      workspaceId: WORKSPACE_ID,
      threadId: THREAD_ID,
      expectedVersion: 8,
    });
    const requeue = buildRequeueNotificationDeliveryCommand({
      workspaceId: WORKSPACE_ID,
      deliveryId: DELIVERY_ID,
      expectedRouteVersion: 3,
      reason: "用户确认重新投递",
    });

    expect(markRead).toMatchObject({
      workspace_id: WORKSPACE_ID,
      command_type: "MarkNotificationThreadRead",
      target_type: "notification_thread",
      target_id: THREAD_ID,
      expected_version: 7,
      actor: { type: "local", id: "aitejiu" },
      payload: { expected_latest_fact_id: LATEST_FACT_ID },
    });
    expect(archive).toMatchObject({
      command_type: "ArchiveNotificationThread",
      target_type: "notification_thread",
      target_id: THREAD_ID,
      expected_version: 8,
      payload: {},
    });
    expect(requeue).toMatchObject({
      command_type: "RequeueNotificationDelivery",
      target_type: "notification_delivery",
      target_id: DELIVERY_ID,
      expected_version: null,
      payload: {
        expected_route_version: 3,
        expected_status: "exhausted",
        reason: "用户确认重新投递",
      },
    });
    for (const command of [markRead, archive, requeue]) {
      expect(command.command_id).toMatch(/^[0-9a-f-]{36}$/);
      expect(command.idempotency_key).toMatch(/^[0-9a-f-]{36}$/);
      expect(command.correlation_id).toMatch(/^[0-9a-f-]{36}$/);
    }
  });

  it("reuses the same command envelope without inventing a retry", async () => {
    const command = buildArchiveNotificationThreadCommand({
      workspaceId: WORKSPACE_ID,
      threadId: THREAD_ID,
      expectedVersion: 8,
    });
    const requests: Record<string, unknown>[] = [];
    server.use(
      http.post("/api/v1/commands", async ({ request }) => {
        requests.push((await request.json()) as Record<string, unknown>);
        return HttpResponse.json({
          aggregate_id: THREAD_ID,
          aggregate_version: 9,
          command_id: command.command_id,
          event_ids: [],
        });
      }),
    );

    await executeNotificationCommand(command);
    await executeNotificationCommand(command);

    expect(requests).toHaveLength(2);
    expect(requests[0]).toEqual(requests[1]);
    expect(requests[0]?.command_id).toBe(command.command_id);
    expect(requests[0]?.idempotency_key).toBe(command.idempotency_key);
  });

  it("preserves actionable server conflict details", async () => {
    server.use(
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/notification-summary`, () =>
        HttpResponse.json(
          { error: { code: "version_conflict", message: "请刷新后重试", details: { current_version: 9 } } },
          { status: 409, headers: { "X-Correlation-Id": "corr-1" } },
        )),
    );

    await expect(getNotificationSummary(WORKSPACE_ID)).rejects.toSatisfy((error: unknown) => {
      return error instanceof ApiProblem
        && error.status === 409
        && error.code === "version_conflict"
        && error.detail === "请刷新后重试"
        && error.details.current_version === 9
        && error.correlationId === "corr-1";
    });
  });
});
