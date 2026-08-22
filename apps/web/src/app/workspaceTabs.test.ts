import { describe, expect, it } from "vitest";

import {
  closeTab,
  hydrateTabs,
  initialTabs,
  openTab,
  reduceTabs,
  registerDirectRoute,
  serializeTabs,
  type WorkspaceTab,
} from "./workspaceTabsState";

const taskIndex = (workspaceId = "ws-1") => initialTabs(workspaceId);

function rootTaskTab(
  taskId: string,
  href = `/workspaces/ws-1/tasks/${taskId}`,
): WorkspaceTab {
  return {
    id: `root-task:${taskId}`,
    kind: "root-task",
    title: "支付重构",
    href,
    closeable: true,
  };
}

function subtaskTab(
  taskId: string,
  subtaskKey: string,
  href = `/workspaces/ws-1/tasks/${taskId}/subtasks/${subtaskKey}`,
): WorkspaceTab {
  return {
    id: `subtask:${taskId}:${subtaskKey}`,
    kind: "subtask",
    title: "回调幂等",
    href,
    closeable: true,
  };
}

function workerTab(workerItemId: string): WorkspaceTab {
  return {
    id: `worker-item:${workerItemId}`,
    kind: "worker-item",
    title: "后端执行 WorkerItem",
    href: `/workspaces/ws-1/worker-items/${workerItemId}`,
    closeable: true,
  };
}

function stateWithTaskSubtaskAndWorker() {
  let state = taskIndex();
  state = reduceTabs(state, openTab(rootTaskTab("task-1")));
  state = reduceTabs(state, openTab(subtaskTab("task-1", "callback-idempotency")));
  return reduceTabs(state, openTab(workerTab("worker-1")));
}

describe("workspace tabs", () => {
  it("deduplicates semantic tabs while preserving the latest URL", () => {
    const first = reduceTabs(
      taskIndex(),
      openTab(rootTaskTab("task-1")),
    );
    const second = reduceTabs(
      first,
      openTab(rootTaskTab("task-1", "/workspaces/ws-1/tasks/task-1?view=dag")),
    );

    expect(second.tabs.filter((tab) => tab.id === "root-task:task-1")).toHaveLength(1);
    expect(second.tabs.at(-1)?.href).toContain("view=dag");
    expect(second.activeTabId).toBe("root-task:task-1");
  });

  it("closes the active tab and activates its left neighbor", () => {
    const next = reduceTabs(
      stateWithTaskSubtaskAndWorker(),
      closeTab("worker-item:worker-1"),
    );

    expect(next.activeTabId).toBe("subtask:task-1:callback-idempotency");
    expect(next.navigationHref).toBe(
      "/workspaces/ws-1/tasks/task-1/subtasks/callback-idempotency",
    );
  });

  it("never closes task-index", () => {
    const state = initialTabs("ws-1");
    expect(reduceTabs(state, closeTab("task-index"))).toEqual(state);
  });

  it("keeps different tasks and semantic child tabs separate", () => {
    let state = taskIndex();
    state = reduceTabs(state, openTab(rootTaskTab("task-1")));
    state = reduceTabs(state, openTab(rootTaskTab("task-2")));
    state = reduceTabs(state, openTab(subtaskTab("task-1", "callback-idempotency")));
    state = reduceTabs(state, openTab(subtaskTab("task-1", "callback-idempotency", "/workspaces/ws-1/tasks/task-1/subtasks/callback-idempotency?view=dag")));
    state = reduceTabs(state, openTab(workerTab("worker-1")));
    state = reduceTabs(state, openTab(workerTab("worker-1")));

    expect(state.tabs.map((tab) => tab.id)).toEqual([
      "task-index",
      "root-task:task-1",
      "root-task:task-2",
      "subtask:task-1:callback-idempotency",
      "worker-item:worker-1",
    ]);
    expect(state.tabs.find((tab) => tab.id === "subtask:task-1:callback-idempotency")?.href).toContain("view=dag");
  });

  it("does not navigate when closing an inactive tab", () => {
    const state = stateWithTaskSubtaskAndWorker();
    const next = reduceTabs(state, closeTab("root-task:task-1"));

    expect(next.activeTabId).toBe("worker-item:worker-1");
    expect(next.navigationHref).toBeNull();
  });

  it("falls back to task-index when the active tab has no left neighbor", () => {
    const state = reduceTabs(taskIndex(), openTab(rootTaskTab("task-1")));
    const next = reduceTabs(state, closeTab("root-task:task-1"));

    expect(next.activeTabId).toBe("task-index");
    expect(next.navigationHref).toBe("/workspaces/ws-1/tasks");
  });

  it("rebuilds a direct deep link without duplicating it", () => {
    const direct = subtaskTab("task-1", "callback-idempotency");
    const state = reduceTabs(
      initialTabs("ws-1"),
      registerDirectRoute(direct),
    );

    expect(state.activeTabId).toBe(direct.id);
    expect(state.tabs).toContainEqual(direct);
  });

  it("discards damaged or cross-workspace session data", () => {
    const state = initialTabs("ws-1");
    expect(hydrateTabs("ws-1", "not-json")).toEqual(state);
    expect(
      hydrateTabs(
        "ws-1",
        serializeTabs(
          reduceTabs(state, openTab(rootTaskTab("task-1", "/workspaces/ws-2/tasks/task-1"))),
        ),
      ),
    ).toEqual(state);
  });

  it("restores tab order, active id, and latest href", () => {
    let state = taskIndex();
    state = reduceTabs(state, openTab(rootTaskTab("task-1")));
    state = reduceTabs(state, openTab(rootTaskTab("task-2")));
    state = reduceTabs(state, openTab(rootTaskTab("task-1", "/workspaces/ws-1/tasks/task-1?view=ledger")));

    expect(hydrateTabs("ws-1", serializeTabs(state))).toEqual(state);
  });
});
