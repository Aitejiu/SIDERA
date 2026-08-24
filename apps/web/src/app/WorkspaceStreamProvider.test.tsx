import { describe, expect, it, vi } from "vitest";

import {
  handleWorkspaceProjectionChange,
  matchesProjectionQuery,
} from "./WorkspaceStreamProvider";
import type { NotificationOwnershipApi } from "@/features/attention/notificationOwnership";
import type { WorkspaceNotification } from "@/features/attention/notifications";
import type { ProjectionChange } from "@/shared/api/projectionStream";

const WORKSPACE_ID = "workspace-1";
const ROOT_TASK_ID = "root-task-1";
const WORKER_ITEM_ID = "worker-item-1";
const WORKER_ID = "worker-1";
const EXECUTION_ID = "execution-1";
const PLANNING_REQUEST_ID = "planning-request-1";

function change(
  projection_type: string,
  resource_id: string | null,
  overrides: Partial<ProjectionChange> = {},
): ProjectionChange {
  return {
    workspace_id: WORKSPACE_ID,
    cursor: 1,
    schema_version: 1,
    projection_type,
    resource_id,
    change_type: "PlanningRequestWaitingForUser",
    resource_version: 2,
    occurred_at: "2026-08-22T00:00:00Z",
    correlation_id: "correlation-1",
    ...overrides,
  };
}

function storageWithBrowserNotifications(): Storage {
  const values = new Map<string, string>([
    [`sidera:browser-notifications:${WORKSPACE_ID}`, "true"],
  ]);
  return {
    length: values.size,
    clear: () => values.clear(),
    getItem: (key: string) => values.get(key) ?? null,
    key: (index: number) => [...values.keys()][index] ?? null,
    removeItem: (key: string) => values.delete(key),
    setItem: (key: string, value: string) => values.set(key, value),
  } as Storage;
}

function collectNotices() {
  let notices: WorkspaceNotification[] = [];
  return {
    get: () => notices,
    set: (update: (current: WorkspaceNotification[]) => WorkspaceNotification[]) => {
      notices = update(notices);
    },
  };
}

function ownershipApi(sourceType: string): NotificationOwnershipApi {
  return {
    getPlanningRequest: vi.fn(async () => ({ source_type: sourceType })),
    listPlanningRequests: vi.fn(async () => ({ items: [], next_cursor: null })),
    getPlanningMaterialization: vi.fn(async () => ({ root_task_id: ROOT_TASK_ID })),
  };
}

async function flushAsyncNotifications(): Promise<void> {
  await new Promise<void>((resolve) => setTimeout(resolve, 0));
}

describe("WorkspaceStreamProvider projection matching", () => {
  it("invalidates only the affected resource for worker and subtask workspaces", () => {
    expect(
      matchesProjectionQuery(
        ["worker-item-workspace", WORKSPACE_ID, WORKER_ITEM_ID],
        WORKSPACE_ID,
        change("worker_item_workspace", WORKER_ITEM_ID),
      ),
    ).toBe(true);
    expect(
      matchesProjectionQuery(
        ["worker-item-workspace", WORKSPACE_ID, "other-worker-item"],
        WORKSPACE_ID,
        change("worker_item_workspace", WORKER_ITEM_ID),
      ),
    ).toBe(false);
    expect(
      matchesProjectionQuery(
        ["subtask-workspace", WORKSPACE_ID, ROOT_TASK_ID, "callback"],
        WORKSPACE_ID,
        change("subtask_workspace", ROOT_TASK_ID),
      ),
    ).toBe(true);
  });

  it("treats a null resource as workspace-wide for its projection type", () => {
    expect(
      matchesProjectionQuery(
        ["worker-item-activity", WORKSPACE_ID, WORKER_ITEM_ID],
        WORKSPACE_ID,
        change("worker_item_activity", null),
      ),
    ).toBe(true);
    expect(
      matchesProjectionQuery(
        ["task-graph", WORKSPACE_ID, ROOT_TASK_ID],
        WORKSPACE_ID,
        change("worker_item_activity", null),
      ),
    ).toBe(false);
  });

  it("invalidates planning request lists and only the affected planning subresources", () => {
    const planningChange = change("planning_request", PLANNING_REQUEST_ID);

    expect(
      matchesProjectionQuery(
        ["planning-requests", WORKSPACE_ID, true],
        WORKSPACE_ID,
        planningChange,
      ),
    ).toBe(true);
    expect(
      matchesProjectionQuery(
        ["planning-request", WORKSPACE_ID, PLANNING_REQUEST_ID],
        WORKSPACE_ID,
        planningChange,
      ),
    ).toBe(true);
    expect(
      matchesProjectionQuery(
        ["planning-proposal", WORKSPACE_ID, PLANNING_REQUEST_ID, 3],
        WORKSPACE_ID,
        planningChange,
      ),
    ).toBe(true);
    expect(
      matchesProjectionQuery(
        ["planning-materialization", WORKSPACE_ID, "other-request"],
        WORKSPACE_ID,
        planningChange,
      ),
    ).toBe(false);
  });

  it("refreshes every planning query when the planning projection has no resource", () => {
    expect(
      matchesProjectionQuery(
        ["planning-clarifications", WORKSPACE_ID, PLANNING_REQUEST_ID],
        WORKSPACE_ID,
        change("planning_request", null),
      ),
    ).toBe(true);
  });

  it("matches runtime, worker, and execution projection resources without broadening other workspaces", () => {
    expect(
      matchesProjectionQuery(
        ["workers", WORKSPACE_ID],
        WORKSPACE_ID,
        change("workers", WORKER_ID),
      ),
    ).toBe(true);
    expect(
      matchesProjectionQuery(
        ["worker", WORKSPACE_ID, WORKER_ID],
        WORKSPACE_ID,
        change("worker_runtime", WORKER_ID),
      ),
    ).toBe(true);
    expect(
      matchesProjectionQuery(
        ["worker-runtime", WORKSPACE_ID, WORKER_ID],
        WORKSPACE_ID,
        change("worker_runtime", WORKER_ID),
      ),
    ).toBe(true);
    expect(
      matchesProjectionQuery(
        ["worker", WORKSPACE_ID, "other-worker"],
        WORKSPACE_ID,
        change("worker_runtime", WORKER_ID),
      ),
    ).toBe(false);
    expect(
      matchesProjectionQuery(
        ["execution", WORKSPACE_ID, EXECUTION_ID],
        WORKSPACE_ID,
        change("executions", EXECUTION_ID),
      ),
    ).toBe(true);
    expect(
      matchesProjectionQuery(
        ["execution", WORKSPACE_ID, "other-execution"],
        WORKSPACE_ID,
        change("executions", EXECUTION_ID),
      ),
    ).toBe(false);
    expect(
      matchesProjectionQuery(
        ["executions", WORKSPACE_ID],
        WORKSPACE_ID,
        change("executions", null),
      ),
    ).toBe(true);
    expect(
      matchesProjectionQuery(
        ["workers", "other-workspace"],
        WORKSPACE_ID,
        change("workers", WORKER_ID),
      ),
    ).toBe(false);
  });
});

describe("WorkspaceStreamProvider notification routing", () => {
  it("keeps private changes in the tray without sending browser notifications", async () => {
    const notices = collectNotices();
    const enqueue = vi.fn();
    const sendBrowserNotification = vi.fn(() => true);
    const storage = storageWithBrowserNotifications();

    handleWorkspaceProjectionChange(change("planning_request", PLANNING_REQUEST_ID), {
      workspaceId: WORKSPACE_ID,
      invalidationQueue: { enqueue },
      setNotices: notices.set,
      ownershipApi: ownershipApi("akashic_private"),
      sendBrowserNotification,
      storage,
    });

    expect(enqueue).toHaveBeenCalledTimes(1);
    expect(notices.get()).toHaveLength(1);
    expect(storage.getItem(`sidera:last-notified-cursor:${WORKSPACE_ID}`)).toBe("1");
    await flushAsyncNotifications();
    expect(sendBrowserNotification).not.toHaveBeenCalled();
  });

  it("sends a Web-origin change once and ignores an unknown owner", async () => {
    const notices = collectNotices();
    const enqueue = vi.fn();
    const sendBrowserNotification = vi.fn(() => true);
    const storage = storageWithBrowserNotifications();

    handleWorkspaceProjectionChange(change("planning_request", PLANNING_REQUEST_ID), {
      workspaceId: WORKSPACE_ID,
      invalidationQueue: { enqueue },
      setNotices: notices.set,
      ownershipApi: ownershipApi("sidera_web_chat"),
      sendBrowserNotification,
      storage,
    });
    await flushAsyncNotifications();
    expect(sendBrowserNotification).toHaveBeenCalledTimes(1);

    handleWorkspaceProjectionChange(
      change("planning_request", "planning-request-unknown", { cursor: 2 }),
      {
        workspaceId: WORKSPACE_ID,
        invalidationQueue: { enqueue },
        setNotices: notices.set,
        ownershipApi: ownershipApi("future_channel"),
        sendBrowserNotification,
        storage,
      },
    );
    await flushAsyncNotifications();
    expect(sendBrowserNotification).toHaveBeenCalledTimes(1);
  });

  it("does not let out-of-order owner lookups swallow a slower Web notice", async () => {
    const notices = collectNotices();
    const enqueue = vi.fn();
    const sendBrowserNotification = vi.fn(() => true);
    const storage = storageWithBrowserNotifications();
    const deferreds: Array<{
      resolve: (value: { source_type: string }) => void;
      promise: Promise<{ source_type: string }>;
    }> = [];
    const api: NotificationOwnershipApi = {
      getPlanningRequest: vi.fn(() => {
        let resolve!: (value: { source_type: string }) => void;
        const promise = new Promise<{ source_type: string }>((nextResolve) => {
          resolve = nextResolve;
        });
        deferreds.push({ resolve, promise });
        return promise;
      }),
      listPlanningRequests: vi.fn(async () => ({ items: [], next_cursor: null })),
      getPlanningMaterialization: vi.fn(async () => ({ root_task_id: ROOT_TASK_ID })),
    };

    handleWorkspaceProjectionChange(
      change("planning_request", "planning-request-1", { cursor: 1 }),
      {
        workspaceId: WORKSPACE_ID,
        invalidationQueue: { enqueue },
        setNotices: notices.set,
        ownershipApi: api,
        sendBrowserNotification,
        storage,
      },
    );
    handleWorkspaceProjectionChange(
      change("planning_request", "planning-request-2", { cursor: 2 }),
      {
        workspaceId: WORKSPACE_ID,
        invalidationQueue: { enqueue },
        setNotices: notices.set,
        ownershipApi: api,
        sendBrowserNotification,
        storage,
      },
    );

    deferreds[1]?.resolve({ source_type: "sidera_web_chat" });
    await flushAsyncNotifications();
    expect(sendBrowserNotification).toHaveBeenCalledTimes(1);
    deferreds[0]?.resolve({ source_type: "sidera_web_chat" });
    await flushAsyncNotifications();
    expect(sendBrowserNotification).toHaveBeenCalledTimes(2);
  });
});
