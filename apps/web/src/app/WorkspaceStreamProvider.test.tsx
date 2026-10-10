import type { QueryClient } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";

import {
  createInvalidationQueue,
  handleWorkspaceProjectionChange,
  matchesProjectionQuery,
} from "./WorkspaceStreamProvider";
import type { NotificationThreadDetail } from "@/features/notifications/api";
import type { NotificationOwnershipApi } from "@/features/attention/notificationOwnership";
import type { WorkspaceNotification } from "@/features/attention/notifications";
import type { ProjectionChange } from "@/shared/api/projectionStream";

const WORKSPACE_ID = "workspace-1";
const ROOT_TASK_ID = "root-task-1";
const WORKER_ITEM_ID = "worker-item-1";
const WORKER_ID = "worker-1";
const EXECUTION_ID = "execution-1";
const PLANNING_REQUEST_ID = "planning-request-1";
const CONTEXT_ID = "context-1";
const PACKAGE_ID = "package-1";
const CANDIDATE_ID = "candidate-1";
const PROPOSAL_ID = "proposal-1";
const THREAD_ID = "notification-thread-1";
const DELIVERY_ID = "notification-delivery-1";

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
  it("invalidates only the matching root task result query", () => {
    const resultChange = change("result_overview", ROOT_TASK_ID);

    expect(matchesProjectionQuery(
      ["task-results", WORKSPACE_ID, ROOT_TASK_ID],
      WORKSPACE_ID,
      resultChange,
    )).toBe(true);
    expect(matchesProjectionQuery(
      ["task-results", WORKSPACE_ID, "another-root"],
      WORKSPACE_ID,
      resultChange,
    )).toBe(false);
  });

  it("targets notification summary, lists, and only the changed thread detail", () => {
    const threadChange = change("notification_thread", THREAD_ID);

    expect(matchesProjectionQuery(["notifications", WORKSPACE_ID, "summary"], WORKSPACE_ID, threadChange)).toBe(true);
    expect(matchesProjectionQuery(["notifications", WORKSPACE_ID, "threads", "action"], WORKSPACE_ID, threadChange)).toBe(true);
    expect(matchesProjectionQuery(["notifications", WORKSPACE_ID, "thread", THREAD_ID], WORKSPACE_ID, threadChange)).toBe(true);
    expect(matchesProjectionQuery(["notifications", WORKSPACE_ID, "thread", "other-thread"], WORKSPACE_ID, threadChange)).toBe(false);
  });

  it("targets delivery settings, attempts, and a cached detail containing that delivery", () => {
    const deliveryChange = change("notification_delivery", DELIVERY_ID);
    const detail = { deliveries: [{ delivery_id: DELIVERY_ID }] };

    expect(matchesProjectionQuery(["notifications", WORKSPACE_ID, "summary"], WORKSPACE_ID, deliveryChange)).toBe(true);
    expect(matchesProjectionQuery(["notifications", WORKSPACE_ID, "threads", "progress"], WORKSPACE_ID, deliveryChange)).toBe(true);
    expect(matchesProjectionQuery(["notifications", WORKSPACE_ID, "readiness"], WORKSPACE_ID, deliveryChange)).toBe(true);
    expect(matchesProjectionQuery(["notifications", WORKSPACE_ID, "modules"], WORKSPACE_ID, deliveryChange)).toBe(true);
    expect(matchesProjectionQuery(["notifications", WORKSPACE_ID, "delivery", DELIVERY_ID, "attempts"], WORKSPACE_ID, deliveryChange)).toBe(true);
    expect(matchesProjectionQuery(["notifications", WORKSPACE_ID, "delivery", "other-delivery", "attempts"], WORKSPACE_ID, deliveryChange)).toBe(false);
    expect(matchesProjectionQuery(["notifications", WORKSPACE_ID, "thread", THREAD_ID], WORKSPACE_ID, deliveryChange, detail)).toBe(true);
    expect(matchesProjectionQuery(["notifications", WORKSPACE_ID, "thread", THREAD_ID], WORKSPACE_ID, deliveryChange, { deliveries: [] })).toBe(false);
  });

  it("coalesces duplicate and replayed cursors into one invalidation", async () => {
    const queryClient = { invalidateQueries: vi.fn() } as unknown as QueryClient;
    const queue = createInvalidationQueue(queryClient, WORKSPACE_ID);
    const current = change("notification_thread", THREAD_ID, { cursor: 21 });

    queue.enqueue(current);
    queue.enqueue(current);
    queue.enqueue({ ...current, cursor: 20 });
    await new Promise<void>((resolve) => setTimeout(resolve, 50));

    expect(queryClient.invalidateQueries).toHaveBeenCalledTimes(1);
    queue.close();
  });

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

  it.each([
    { queryKey: ["native-worker-context", "workspace-1", "worker-item-1", "principal-A"], expected: true },
    { queryKey: ["native-worker-context", "workspace-1", "worker-item-1", "principal-B"], expected: true },
    { queryKey: ["native-worker-context", "workspace-1", "other-item", "principal-A"], expected: false },
    { queryKey: ["native-worker-context", "other-workspace", "worker-item-1", "principal-A"], expected: false },
    { queryKey: ["agent-assignment", "workspace-1", "worker_item", "worker-item-1", "principal-A"], expected: true },
    { queryKey: ["agent-assignment", "workspace-1", "worker_item", "other-item", "principal-A"], expected: false },
    { queryKey: ["agent-assignment", "workspace-1", "subtask", "worker-item-1", "principal-A"], expected: false },
    { queryKey: ["agent-assignment", "other-workspace", "worker_item", "worker-item-1", "principal-A"], expected: false },
    { queryKey: ["execution", "workspace-1", "execution-1"], expected: false },
  ])("matches a WorkerItem workspace change for $queryKey as $expected", ({ queryKey, expected }) => {
    expect(matchesProjectionQuery(
      queryKey,
      WORKSPACE_ID,
      change("worker_item_workspace", WORKER_ITEM_ID),
    )).toBe(expected);
  });

  it.each([
    { queryKey: ["native-worker-context", "workspace-1", "worker-item-1", "principal-A"], expected: true },
    { queryKey: ["native-worker-context", "workspace-1", "other-item", "principal-B"], expected: true },
    { queryKey: ["native-worker-context", "other-workspace", "worker-item-1", "principal-A"], expected: false },
    { queryKey: ["agent-assignment", "workspace-1", "worker_item", "worker-item-1", "principal-A"], expected: true },
    { queryKey: ["agent-assignment", "workspace-1", "worker_item", "other-item", "principal-B"], expected: true },
    { queryKey: ["agent-assignment", "workspace-1", "subtask", "worker-item-1", "principal-A"], expected: false },
    { queryKey: ["agent-assignment", "other-workspace", "worker_item", "worker-item-1", "principal-A"], expected: false },
    { queryKey: ["execution", "workspace-1", "execution-1"], expected: false },
  ])("scopes a null WorkerItem workspace resource for $queryKey as $expected", ({ queryKey, expected }) => {
    expect(matchesProjectionQuery(
      queryKey,
      WORKSPACE_ID,
      change("worker_item_workspace", null),
    )).toBe(expected);
  });

  it.each([WORKER_ITEM_ID, null])("does not refresh native context or assignment from activity resource %s", (resourceId) => {
    const activityChange = change("worker_item_activity", resourceId);

    expect(matchesProjectionQuery(
      ["native-worker-context", WORKSPACE_ID, WORKER_ITEM_ID, "principal-A"],
      WORKSPACE_ID,
      activityChange,
    )).toBe(false);
    expect(matchesProjectionQuery(
      ["agent-assignment", WORKSPACE_ID, "worker_item", WORKER_ITEM_ID, "principal-A"],
      WORKSPACE_ID,
      activityChange,
    )).toBe(false);
  });

  it("refreshes governance detail and resource queries without treating it as runtime", () => {
    const governanceChange = change("collaboration_governance", ROOT_TASK_ID);

    expect(
      matchesProjectionQuery(
        ["collaboration-detail", WORKSPACE_ID, ROOT_TASK_ID],
        WORKSPACE_ID,
        governanceChange,
      ),
    ).toBe(true);
    expect(
      matchesProjectionQuery(
        ["collaboration-decisions", WORKSPACE_ID, ROOT_TASK_ID],
        WORKSPACE_ID,
        governanceChange,
      ),
    ).toBe(true);
    expect(
      matchesProjectionQuery(
        ["collaboration-governance-worker-item", WORKSPACE_ID, WORKER_ITEM_ID],
        WORKSPACE_ID,
        change("collaboration_governance", WORKER_ITEM_ID),
      ),
    ).toBe(true);
    expect(
      matchesProjectionQuery(
        ["collaboration-governance-proposal", WORKSPACE_ID, PROPOSAL_ID],
        WORKSPACE_ID,
        change("collaboration_governance", PROPOSAL_ID),
      ),
    ).toBe(true);
    expect(
      matchesProjectionQuery(
        ["collaboration-runtime", WORKSPACE_ID, ROOT_TASK_ID],
        WORKSPACE_ID,
        governanceChange,
      ),
    ).toBe(false);
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

  it("refreshes context package and work context aliases within the workspace", () => {
    const packageChange = change("context_package", PACKAGE_ID);
    expect(
      matchesProjectionQuery(
        ["context-package", WORKSPACE_ID, PACKAGE_ID],
        WORKSPACE_ID,
        packageChange,
      ),
    ).toBe(true);
    expect(
      matchesProjectionQuery(
        ["context-package", WORKSPACE_ID, "other-package"],
        WORKSPACE_ID,
        packageChange,
      ),
    ).toBe(false);
    expect(
      matchesProjectionQuery(
        ["context-package", WORKSPACE_ID, "execution", EXECUTION_ID],
        WORKSPACE_ID,
        packageChange,
      ),
    ).toBe(true);
    expect(
      matchesProjectionQuery(
        ["worker-result-inputs", WORKSPACE_ID, WORKER_ITEM_ID],
        WORKSPACE_ID,
        packageChange,
      ),
    ).toBe(true);
    expect(
      matchesProjectionQuery(
        ["context-package", "other-workspace", "execution", EXECUTION_ID],
        WORKSPACE_ID,
        packageChange,
      ),
    ).toBe(false);

    const contextChange = change("work_context", CONTEXT_ID);
    expect(
      matchesProjectionQuery(
        ["work-context", WORKSPACE_ID, CONTEXT_ID],
        WORKSPACE_ID,
        contextChange,
      ),
    ).toBe(true);
    expect(
      matchesProjectionQuery(
        ["work-context", WORKSPACE_ID, "root-task", ROOT_TASK_ID],
        WORKSPACE_ID,
        contextChange,
      ),
    ).toBe(true);
    expect(
      matchesProjectionQuery(
        ["work-context", WORKSPACE_ID, "planning-request", PLANNING_REQUEST_ID],
        WORKSPACE_ID,
        contextChange,
      ),
    ).toBe(true);
    expect(
      matchesProjectionQuery(
        ["work-context", WORKSPACE_ID, "other-context"],
        WORKSPACE_ID,
        contextChange,
      ),
    ).toBe(false);
  });

  it("refreshes task knowledge submission queries without touching knowledge service queries", () => {
    const submissionChange = change(
      "knowledge_candidate_submission",
      CANDIDATE_ID,
    );
    expect(
      matchesProjectionQuery(
        ["context-knowledge-submissions", WORKSPACE_ID, "task", ROOT_TASK_ID],
        WORKSPACE_ID,
        submissionChange,
      ),
    ).toBe(true);
    expect(
      matchesProjectionQuery(
        ["knowledge", WORKSPACE_ID, "candidates", { limit: "100" }],
        WORKSPACE_ID,
        submissionChange,
      ),
    ).toBe(false);
    expect(
      matchesProjectionQuery(
        ["task-overview", WORKSPACE_ID, ROOT_TASK_ID],
        WORKSPACE_ID,
        submissionChange,
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

  it("does not create a browser notification from projection metadata", async () => {
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
    expect(sendBrowserNotification).not.toHaveBeenCalled();

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
    expect(sendBrowserNotification).not.toHaveBeenCalled();
  });

  it("fetches a new high-value Fact before sending a browser notification", async () => {
    const notices = collectNotices();
    const enqueue = vi.fn();
    const sendBrowserNotification = vi.fn(() => true);
    const storage = storageWithBrowserNotifications();
    const fetchNotificationThread = vi.fn(async () => notificationDetail());

    handleWorkspaceProjectionChange(
      change("notification_thread", THREAD_ID, { cursor: 3 }),
      {
        workspaceId: WORKSPACE_ID,
        invalidationQueue: { enqueue },
        setNotices: notices.set,
        ownershipApi: ownershipApi("sidera_web_chat"),
        sendBrowserNotification,
        storage,
        fetchNotificationThread,
      },
    );

    await flushAsyncNotifications();
    expect(fetchNotificationThread).toHaveBeenCalledWith(WORKSPACE_ID, THREAD_ID);
    expect(sendBrowserNotification).toHaveBeenCalledTimes(1);
    expect(sendBrowserNotification).toHaveBeenCalledWith(expect.objectContaining({
      id: `notification-fact:${WORKSPACE_ID}:fact-1`,
      title: "服务端事实标题",
      summary: "服务端事实摘要",
      href: `/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}?view=governance`,
    }));
  });

  it("does not send progress or private Facts and tolerates storage failures", async () => {
    const notices = collectNotices();
    const enqueue = vi.fn();
    const sendBrowserNotification = vi.fn(() => true);
    const storage = throwingStorage();
    const fetchNotificationThread = vi.fn(async () => notificationDetail({
      priority: "progress",
    }));

    expect(() => handleWorkspaceProjectionChange(
      change("notification_thread", THREAD_ID, { cursor: 4 }),
      {
        workspaceId: WORKSPACE_ID,
        invalidationQueue: { enqueue },
        setNotices: notices.set,
        ownershipApi: ownershipApi("akashic_private"),
        sendBrowserNotification,
        storage,
        fetchNotificationThread,
      },
    )).not.toThrow();
    await flushAsyncNotifications();
    expect(sendBrowserNotification).not.toHaveBeenCalled();
  });
});

function notificationDetail(
  overrides: Partial<NotificationThreadDetail["facts"]["items"][number]> = {},
): NotificationThreadDetail {
  return {
    thread: {
      latest_fact_id: "fact-1",
      deep_link: `/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}?view=governance`,
    },
    facts: {
      items: [{
        id: "fact-1",
        priority: "action",
        title: "服务端事实标题",
        summary: "服务端事实摘要",
        deep_link: `/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}?view=governance`,
        baseline_import: false,
        thread_id: THREAD_ID,
        source_projection_cursor: 3,
        source_projection_type: "planning_request",
        source_resource_id: "planning-request-1",
        source_resource_version: 2,
        correlation_id: "fact-correlation-1",
        kind: "planning_clarification",
        ...overrides,
      }],
      next_cursor: null,
    },
  } as unknown as NotificationThreadDetail;
}

function throwingStorage(): Storage {
  return {
    length: 0,
    clear: () => { throw new Error("storage unavailable"); },
    getItem: () => { throw new Error("storage unavailable"); },
    key: () => { throw new Error("storage unavailable"); },
    removeItem: () => { throw new Error("storage unavailable"); },
    setItem: () => { throw new Error("storage unavailable"); },
  } as Storage;
}
