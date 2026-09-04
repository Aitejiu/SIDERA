import { describe, expect, it, vi } from "vitest";

import type { ProjectionChange } from "@/shared/api/projectionStream";

import {
  appendNotification,
  browserNotificationIdsKey,
  browserNotificationsEnabled,
  hasSentBrowserNotification,
  planningNotificationFromChange,
  planningNotificationFromRequest,
  notificationFromChange,
  notificationFromFact,
  readSentBrowserNotificationIds,
  readLastNotifiedCursor,
  recordSentBrowserNotification,
  setBrowserNotificationsEnabled,
  sendBrowserNotification,
  writeLastNotifiedCursor,
} from "./notifications";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";

function change(overrides: Partial<ProjectionChange> = {}): ProjectionChange {
  return {
    workspace_id: WORKSPACE_ID,
    cursor: 12,
    schema_version: 1,
    projection_type: "task_first",
    resource_id: "30000000-0000-0000-0000-000000000001",
    change_type: "TaskWaitingForDecision",
    resource_version: 3,
    occurred_at: "2026-08-21T08:00:00Z",
    correlation_id: "70000000-0000-0000-0000-000000000001",
    ...overrides,
  };
}

describe("workspace notifications", () => {
  it("deduplicates the same cursor and resource version", () => {
    const first = notificationFromChange(WORKSPACE_ID, change());
    const replay = notificationFromChange(WORKSPACE_ID, change());

    expect(first).not.toBeNull();
    expect(replay?.id).toBe(first?.id);
  });

  it("aggregates repeated notices for one resource", () => {
    const first = notificationFromChange(WORKSPACE_ID, change({ cursor: 12 }));
    const second = notificationFromChange(WORKSPACE_ID, change({ cursor: 13, resource_version: 4 }));

    expect(first).not.toBeNull();
    expect(second).not.toBeNull();
    const result = appendNotification([first!], second!);
    expect(result).toHaveLength(1);
    expect(result[0]?.title).toContain("批量 2");
    expect(result[0]?.summary).toContain("Cursor 13");
  });

  it("persists notification cursors and explicit browser preference", () => {
    const storage = new Map<string, string>();
    const fakeStorage = {
      length: 0,
      clear: () => storage.clear(),
      getItem: (key: string) => storage.get(key) ?? null,
      key: () => null,
      removeItem: (key: string) => storage.delete(key),
      setItem: (key: string, value: string) => {
        storage.set(key, value);
      },
    } as Storage;

    expect(readLastNotifiedCursor(WORKSPACE_ID, fakeStorage)).toBe(0);
    writeLastNotifiedCursor(WORKSPACE_ID, 42, fakeStorage);
    setBrowserNotificationsEnabled(WORKSPACE_ID, true, fakeStorage);

    expect(readLastNotifiedCursor(WORKSPACE_ID, fakeStorage)).toBe(42);
    expect(browserNotificationsEnabled(WORKSPACE_ID, fakeStorage)).toBe(true);
  });

  it("deduplicates browser notices with a bounded recent ID set", () => {
    const storage = new Map<string, string>();
    const fakeStorage = {
      length: 0,
      clear: () => storage.clear(),
      getItem: (key: string) => storage.get(key) ?? null,
      key: () => null,
      removeItem: (key: string) => storage.delete(key),
      setItem: (key: string, value: string) => {
        storage.set(key, value);
      },
    } as Storage;

    for (let index = 0; index < 130; index += 1) {
      recordSentBrowserNotification(WORKSPACE_ID, `notice-${index}`, fakeStorage);
    }
    recordSentBrowserNotification(WORKSPACE_ID, "notice-129", fakeStorage);

    const ids = readSentBrowserNotificationIds(WORKSPACE_ID, fakeStorage);
    expect(storage.has(browserNotificationIdsKey(WORKSPACE_ID))).toBe(true);
    expect(ids).toHaveLength(128);
    expect(ids[0]).toBe("notice-2");
    expect(ids.at(-1)).toBe("notice-129");
    expect(hasSentBrowserNotification(WORKSPACE_ID, "notice-1", fakeStorage)).toBe(false);
    expect(hasSentBrowserNotification(WORKSPACE_ID, "notice-129", fakeStorage)).toBe(true);
  });

  it("builds browser copy from a new high-value Fact and keeps its deep link", () => {
    const fact = {
      id: "fact-1",
      priority: "action",
      title: "服务端事实标题",
      summary: "服务端事实摘要",
      deep_link: `/workspaces/${WORKSPACE_ID}/tasks/task-1?view=governance`,
      baseline_import: false,
      kind: "execution_attention",
      thread_id: "thread-1",
    };

    expect(notificationFromFact(WORKSPACE_ID, fact)).toEqual({
      id: `notification-fact:${WORKSPACE_ID}:fact-1`,
      kind: "notification_fact",
      title: "服务端事实标题",
      summary: "服务端事实摘要",
      resourceId: "thread-1",
      resourceVersion: null,
      cursor: null,
      href: `/workspaces/${WORKSPACE_ID}/tasks/task-1?view=governance`,
    });
  });

  it("does not turn progress or baseline Facts into browser notifications", () => {
    const baseFact = {
      id: "fact-1",
      priority: "progress",
      title: "进度事实",
      summary: "进度已完成",
      deep_link: `/workspaces/${WORKSPACE_ID}/tasks/task-1`,
      baseline_import: false,
      thread_id: "thread-1",
    };

    expect(notificationFromFact(WORKSPACE_ID, baseFact)).toBeNull();
    expect(notificationFromFact(WORKSPACE_ID, { ...baseFact, priority: "action", baseline_import: true })).toBeNull();
  });

  it("keeps notification storage helpers fail-closed when localStorage throws", () => {
    const storage = {
      getItem: () => { throw new Error("storage unavailable"); },
      setItem: () => { throw new Error("storage unavailable"); },
    } as unknown as Storage;

    expect(() => readLastNotifiedCursor(WORKSPACE_ID, storage)).not.toThrow();
    expect(() => writeLastNotifiedCursor(WORKSPACE_ID, 42, storage)).not.toThrow();
    expect(() => setBrowserNotificationsEnabled(WORKSPACE_ID, true, storage)).not.toThrow();
    expect(browserNotificationsEnabled(WORKSPACE_ID, storage)).toBe(false);
    expect(readSentBrowserNotificationIds(WORKSPACE_ID, storage)).toEqual([]);
  });

  it("opens the exact same-origin Fact deep link when a browser notice is clicked", () => {
    const instances: Array<{ onclick: (() => void) | null }> = [];
    class FakeNotification {
      static permission: NotificationPermission = "granted";
      onclick: (() => void) | null = null;

      constructor(
        public readonly title: string,
        public readonly options: { body: string },
      ) {
        instances.push(this);
      }
    }
    vi.stubGlobal("Notification", FakeNotification);
    const assign = vi.fn();

    const sent = sendBrowserNotification({
      id: "notification-fact-1",
      kind: "notification_fact",
      title: "需要处理",
      summary: "服务端事实摘要",
      resourceId: "thread-1",
      resourceVersion: null,
      cursor: null,
      href: `/workspaces/${WORKSPACE_ID}/tasks/task-1?view=governance#fact-1`,
    }, assign);

    expect(sent).toBe(true);
    expect(instances).toHaveLength(1);
    instances[0]?.onclick?.();
    expect(assign).toHaveBeenCalledWith(
      `/workspaces/${WORKSPACE_ID}/tasks/task-1?view=governance#fact-1`,
    );
    vi.unstubAllGlobals();
  });

  it.each([
    ["PlanningRequestWaitingForUser", "planning_clarification", "任务规划需要补充信息"],
    ["PlanningProposalReady", "planning_proposal", "任务方案待确认"],
    ["PlanningRequestConflicted", "planning_conflicted", "任务方案已冲突"],
    ["PlanningRequestMaterialized", "planning_materialized", "任务已开始"],
  ])("maps %s to a planning deep-link", (changeType, kind, title) => {
    const notification = planningNotificationFromChange(
      WORKSPACE_ID,
      change({ projection_type: "planning_request", change_type: changeType }),
    );

    expect(notification).toMatchObject({
      kind,
      title,
      href: `${"/workspaces"}/${WORKSPACE_ID}/planning/${change().resource_id}`,
    });
  });

  it.each([
    ["waiting_for_user", "planning_clarification", "任务规划需要补充信息"],
    ["proposal_ready", "planning_proposal", "任务方案待确认"],
    ["conflicted", "planning_conflicted", "任务方案已冲突"],
    ["blocked", "planning_blocked", "任务规划已阻塞"],
  ])("keeps persisted %s planning facts actionable after refresh", (status, kind, title) => {
    const notification = planningNotificationFromRequest({
      id: "planning-request-2",
      status,
      version: 7,
      workspace_id: WORKSPACE_ID,
    });

    expect(notification).toMatchObject({
      kind,
      title,
      href: `/workspaces/${WORKSPACE_ID}/planning/planning-request-2`,
      cursor: null,
    });
  });
});
