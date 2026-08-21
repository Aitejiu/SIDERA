import { describe, expect, it } from "vitest";

import type { ProjectionChange } from "@/shared/api/projectionStream";

import {
  appendNotification,
  browserNotificationsEnabled,
  notificationFromChange,
  readLastNotifiedCursor,
  setBrowserNotificationsEnabled,
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
});
