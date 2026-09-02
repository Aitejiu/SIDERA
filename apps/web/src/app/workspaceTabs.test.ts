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
import { semanticTabForLocation } from "./WorkspaceTabs";

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

function planningNewTab(workspaceId = "ws-1"): WorkspaceTab {
  return {
    id: "planning-new",
    kind: "planning-new",
    title: "新建任务",
    href: `/workspaces/${workspaceId}/planning/new`,
    closeable: true,
  };
}

function planningTab(
  requestId: string,
  href = `/workspaces/ws-1/planning/${requestId}`,
): WorkspaceTab {
  return {
    id: `planning:${requestId}`,
    kind: "planning",
    title: "规划请求",
    href,
    closeable: true,
  };
}

function chatTab(threadId: string, workspaceId = "ws-1"): WorkspaceTab {
  return {
    id: `chat:${threadId}`,
    kind: "chat",
    title: "Main Agent",
    href: `/workspaces/${workspaceId}/chat/${threadId}`,
    closeable: true,
  };
}

function knowledgePageTab(pageId: string): WorkspaceTab {
  return {
    id: `knowledge-page:${pageId}`,
    kind: "knowledge-page",
    title: "知识页",
    href: `/workspaces/ws-1/knowledge/pages/${pageId}`,
    closeable: true,
  };
}

function knowledgeCandidateTab(candidateId: string): WorkspaceTab {
  return {
    id: `knowledge-candidate:${candidateId}`,
    kind: "knowledge-candidate",
    title: "候选",
    href: `/workspaces/ws-1/knowledge/candidates/${candidateId}`,
    closeable: true,
  };
}

function contextPackageTab(packageId: string): WorkspaceTab {
  return {
    id: `context-package:${packageId}`,
    kind: "context-package",
    title: "上下文包",
    href: `/workspaces/ws-1/context-packages/${packageId}`,
    closeable: true,
  };
}

function collaborationTab(rootTaskId: string): WorkspaceTab {
  return {
    id: `collaboration:${rootTaskId}`,
    kind: "collaboration",
    title: "协同记录",
    href: `/workspaces/ws-1/tasks/${rootTaskId}/collaboration`,
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

  it("keeps the new brief and different planning requests as separate semantic tabs", () => {
    let state = taskIndex();
    state = reduceTabs(state, openTab(planningNewTab()));
    state = reduceTabs(state, openTab(planningTab("request-1")));
    state = reduceTabs(state, openTab(planningTab("request-2")));
    state = reduceTabs(state, openTab(planningTab("request-1", "/workspaces/ws-1/planning/request-1?view=history")));

    expect(state.tabs.map((tab) => tab.id)).toEqual([
      "task-index",
      "planning-new",
      "planning:request-1",
      "planning:request-2",
    ]);
    expect(state.activeTabId).toBe("planning:request-1");
    expect(state.tabs.find((tab) => tab.id === "planning:request-1")?.href).toContain("view=history");
  });

  it("closes an active planning tab to its left neighbor without losing the audit tab", () => {
    let state = taskIndex();
    state = reduceTabs(state, openTab(planningNewTab()));
    state = reduceTabs(state, openTab(planningTab("request-1")));
    state = reduceTabs(state, openTab(rootTaskTab("task-1")));
    state = reduceTabs(state, closeTab("root-task:task-1"));

    expect(state.activeTabId).toBe("planning:request-1");
    expect(state.navigationHref).toBe("/workspaces/ws-1/planning/request-1");
    expect(state.tabs.map((tab) => tab.id)).toContain("planning:request-1");
  });

  it("keeps planning audit tabs when a root task is opened", () => {
    let state = taskIndex();
    state = reduceTabs(state, openTab(planningTab("request-1")));
    state = reduceTabs(state, openTab(rootTaskTab("task-1")));

    expect(state.tabs.map((tab) => tab.id)).toEqual([
      "task-index",
      "planning:request-1",
      "root-task:task-1",
    ]);
  });

  it("keeps a collaboration record as a separate tab from its root task", () => {
    let state = taskIndex();
    state = reduceTabs(state, openTab(rootTaskTab("task-1")));
    state = reduceTabs(state, openTab(collaborationTab("task-1")));

    expect(state.tabs.map((tab) => tab.id)).toEqual([
      "task-index",
      "root-task:task-1",
      "collaboration:task-1",
    ]);
    expect(state.activeTabId).toBe("collaboration:task-1");

    const closed = reduceTabs(state, closeTab("collaboration:task-1"));
    expect(closed.activeTabId).toBe("root-task:task-1");
    expect(closed.navigationHref).toBe("/workspaces/ws-1/tasks/task-1");
  });

  it("keeps multiple chat threads as separate semantic tabs", () => {
    let state = taskIndex();
    state = reduceTabs(state, openTab(chatTab("thread-1")));
    state = reduceTabs(state, openTab(chatTab("thread-2")));
    state = reduceTabs(state, openTab(chatTab("thread-1", "ws-1")));

    expect(state.tabs.map((tab) => tab.id)).toContain("chat:thread-1");
    expect(state.tabs.map((tab) => tab.id)).toContain("chat:thread-2");
    expect(state.tabs.filter((tab) => tab.id === "chat:thread-1")).toHaveLength(1);
    expect(state.activeTabId).toBe("chat:thread-1");
  });

  it("keeps knowledge pages, candidates, and context packages independently addressable", () => {
    let state = taskIndex();
    state = reduceTabs(state, openTab(knowledgePageTab("page-1")));
    state = reduceTabs(state, openTab(knowledgePageTab("page-1")));
    state = reduceTabs(state, openTab(knowledgeCandidateTab("candidate-1")));
    state = reduceTabs(state, openTab(contextPackageTab("package-1")));

    expect(state.tabs.map((tab) => tab.id)).toEqual([
      "task-index",
      "knowledge-page:page-1",
      "knowledge-candidate:candidate-1",
      "context-package:package-1",
    ]);
    expect(state.activeTabId).toBe("context-package:package-1");
  });

  it("returns to the previous semantic tab when a context package is closed", () => {
    let state = taskIndex();
    state = reduceTabs(state, openTab(knowledgePageTab("page-1")));
    state = reduceTabs(state, openTab(contextPackageTab("package-1")));

    const next = reduceTabs(state, closeTab("context-package:package-1"));

    expect(next.activeTabId).toBe("knowledge-page:page-1");
    expect(next.navigationHref).toBe("/workspaces/ws-1/knowledge/pages/page-1");
  });

  it("closes an active chat tab to its left neighbor", () => {
    let state = taskIndex();
    state = reduceTabs(state, openTab(chatTab("thread-1")));
    state = reduceTabs(state, openTab(chatTab("thread-2")));

    const next = reduceTabs(state, closeTab("chat:thread-2"));

    expect(next.activeTabId).toBe("chat:thread-1");
    expect(next.navigationHref).toBe("/workspaces/ws-1/chat/thread-1");
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

  it("rebuilds a direct planning request deep link", () => {
    const direct = planningTab("request-1");
    const state = reduceTabs(
      initialTabs("ws-1"),
      registerDirectRoute(direct),
    );

    expect(state.activeTabId).toBe("planning:request-1");
    expect(state.tabs).toContainEqual(direct);
  });

  it("maps a nested revision route to the existing root-task semantic tab", () => {
    const revisionRoute = semanticTabForLocation("ws-1", {
      pathname: "/workspaces/ws-1/tasks/task-1/revisions/request-1",
      search: "",
    });

    expect(revisionRoute).toEqual({
      id: "root-task:task-1",
      kind: "root-task",
      title: "任务 task-1",
      href: "/workspaces/ws-1/tasks/task-1/revisions/request-1",
      closeable: true,
    });
  });

  it("maps a collaboration route to its own semantic tab", () => {
    expect(semanticTabForLocation("ws-1", {
      pathname: "/workspaces/ws-1/tasks/task-1/collaboration",
      search: "",
    })).toEqual(collaborationTab("task-1"));
  });

  it("discards damaged or cross-workspace session data", () => {
    const state = initialTabs("ws-1");
    expect(hydrateTabs("ws-1", "not-json")).toEqual(state);
    expect(
      hydrateTabs(
        "ws-1",
        JSON.stringify({
          schemaVersion: 2,
          tabs: state.tabs,
          activeTabId: state.activeTabId,
        }),
      ),
    ).toEqual(state);
    expect(
      hydrateTabs(
        "ws-1",
        serializeTabs(
          reduceTabs(
            state,
            openTab(chatTab("thread-1", "ws-2")),
          ),
        ),
      ),
    ).toEqual(state);
    expect(
      hydrateTabs(
        "ws-1",
        serializeTabs(
          reduceTabs(state, openTab(rootTaskTab("task-1", "/workspaces/ws-2/tasks/task-1"))),
        ),
      ),
    ).toEqual(state);
    expect(
      hydrateTabs(
        "ws-1",
        JSON.stringify({
          schemaVersion: 4,
          tabs: state.tabs,
          activeTabId: state.activeTabId,
        }),
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
