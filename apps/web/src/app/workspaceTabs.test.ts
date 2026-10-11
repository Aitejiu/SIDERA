import { describe, expect, it } from "vitest";

import {
  activateTab,
  closeTab,
  hydrateTabs,
  initialTabs,
  loadTabs,
  openTab,
  reduceTabs,
  registerDirectRoute,
  serializeTabs,
  saveTabs,
  storageKey,
  type WorkspaceTab,
} from "./workspaceTabsState";
import { semanticTabForLocation } from "./WorkspaceTabs";

describe("agent and runtime tab metadata", () => {
  it.each([
    ["/agents", "agents", "agents"],
    ["/agents/profile-a?view=settings", "agent:profile-a", "agent"],
    ["/runtimes", "runtimes", "runtimes"],
    ["/runtimes/runtime-a", "runtime:runtime-a", "runtime"],
  ])("registers the committed %s route without changing Task ownership", (path, id, kind) => {
    const [pathname, query] = path.split("?");
    const tab = semanticTabForLocation("ws-1", { pathname: `/workspaces/ws-1${pathname}`, search: query ? `?${query}` : "" });
    expect(tab).toMatchObject({ id, kind, closeable: true, href: `/workspaces/ws-1${path}` });
    expect(tab && reduceTabs(initialTabs("ws-1"), openTab(tab)).tabs[0]).toEqual(initialTabs("ws-1").tabs[0]);
  });

  it("loads valid v5 ownership and return targets into v6 storage without restoring a draft", () => {
    let old = stateWithTaskSubtaskAndWorker();
    old = reduceTabs(old, openTab(chatTab("original-chat")));
    old = reduceTabs(old, openTab(knowledgePageTab("original-page")));
    const raw = JSON.stringify({ schemaVersion: 5, tabs: old.tabs, activeTabId: old.activeTabId });
    window.sessionStorage.removeItem(storageKey("ws-1"));
    window.sessionStorage.setItem("sidera.workspace-tabs.v5:ws-1", raw);
    const loaded = loadTabs("ws-1");
    expect(loaded).toEqual(old);
    saveTabs(loaded);
    const saved = JSON.parse(window.sessionStorage.getItem("sidera.workspace-tabs.v6:ws-1") ?? "null");
    expect(saved).toEqual({ schemaVersion: 6, tabs: old.tabs, activeTabId: old.activeTabId });
    expect(window.sessionStorage.getItem("sidera.workspace-tabs.v5:ws-1")).toBe(raw);
    window.sessionStorage.clear();
  });

  it("retains only safe original operation/ref recovery alongside an agent return target", () => {
    const tab: WorkspaceTab = {
      id: "agent:profile-a", kind: "agent", title: "Reusable reviewer", href: "/workspaces/ws-1/agents/profile-a?view=settings", closeable: true,
      configurationRecovery: { operationId: "original/save 1", target_ref: { workspace_id: "ws-1", profile_id: "profile-a", revision: 2 } },
    };
    const state = reduceTabs(reduceTabs(initialTabs("ws-1"), openTab(rootTaskTab("task-1"))), openTab(tab));
    const saved = serializeTabs(state);
    expect(hydrateTabs("ws-1", saved)).toEqual(state);
    expect(JSON.parse(saved).tabs[2].returnTabId).toBe("root-task:task-1");
    expect(saved).not.toMatch(/instructions|environment|secret|draft/);
  });

  it("round-trips an original create operation without inventing its unknown target Ref", () => {
    const tab: WorkspaceTab = {
      id: "agent:new", kind: "agent", title: "New Profile", href: "/workspaces/ws-1/agents/new", closeable: true,
      configurationRecovery: { operationId: "original/create 1" },
    };
    const state = reduceTabs(initialTabs("ws-1"), openTab(tab));
    expect(hydrateTabs("ws-1", serializeTabs(state))).toEqual(state);
    expect(serializeTabs(state)).not.toContain("target_ref");
  });

  it.each(["null-ref", "primitive-ref", "array-ref", "bad-revision", "extra-ref", "no-ref-extra", "no-ref-token", "no-ref-empty-operation"])("rejects malformed original recovery %s", variant => {
    const recovery: Record<string, unknown> = { operationId: "original/create 1" };
    const ref = { workspace_id: "ws-1", profile_id: "profile-a", revision: 1 };
    if (variant === "null-ref") recovery.target_ref = null;
    if (variant === "primitive-ref") recovery.target_ref = "profile-a";
    if (variant === "array-ref") recovery.target_ref = [ref];
    if (variant === "bad-revision") recovery.target_ref = { ...ref, revision: 0 };
    if (variant === "extra-ref") recovery.target_ref = { ...ref, draft: "PRIVATE" };
    if (variant === "no-ref-extra") recovery.draft = "PRIVATE";
    if (variant === "no-ref-token") recovery.token = "PRIVATE";
    if (variant === "no-ref-empty-operation") recovery.operationId = "";
    const tab = { id: "agent:new", kind: "agent", title: "New Profile", href: "/workspaces/ws-1/agents/new", closeable: true, configurationRecovery: recovery };
    const raw = JSON.stringify({ schemaVersion: 6, tabs: [initialTabs("ws-1").tabs[0], tab], activeTabId: tab.id });
    expect(hydrateTabs("ws-1", raw)).toEqual(initialTabs("ws-1"));
  });

  it.each(["foreign-ref", "empty-operation", "extra-secret", "draft", "empty-id", "duplicate", "foreign-href"])("denies unsafe %s metadata rather than restoring an editor", (variant) => {
    const tab = { id: "agent:profile-a", kind: "agent", title: "Reviewer", href: "/workspaces/ws-1/agents/profile-a", closeable: true,
      configurationRecovery: { operationId: "original", target_ref: { workspace_id: "ws-1", profile_id: "profile-a", revision: 1 } } };
    const payload = { schemaVersion: 6, tabs: [initialTabs("ws-1").tabs[0], tab], activeTabId: tab.id };
    if (variant === "foreign-ref") tab.configurationRecovery.target_ref.workspace_id = "foreign";
    if (variant === "empty-operation") tab.configurationRecovery.operationId = "";
    if (variant === "extra-secret") Object.assign(tab.configurationRecovery, { token: "PRIVATE" });
    if (variant === "draft") Object.assign(tab, { draft: { instructions: "PRIVATE" } });
    if (variant === "empty-id") { tab.id = "agent:"; payload.activeTabId = tab.id; }
    if (variant === "duplicate") payload.tabs.push(tab);
    if (variant === "foreign-href") tab.href = "/workspaces/foreign/agents/profile-a";
    expect(hydrateTabs("ws-1", JSON.stringify(payload))).toEqual(initialTabs("ws-1"));
  });
});

describe("workspace tab descriptions", () => {
  it("persists the resolved ownership path with the tab", () => {
    const tab = { ...subtaskTab("task-1", "callback"), description: "子任务 · 支付重构 / 回调幂等" };
    const state = reduceTabs(initialTabs("ws-1"), openTab(tab));
    expect(hydrateTabs("ws-1", serializeTabs(state))).toEqual(state);
  });

  it("rejects a non-text description from storage", () => {
    const state = reduceTabs(initialTabs("ws-1"), openTab(rootTaskTab("task-1")));
    const serialized = JSON.parse(serializeTabs(state));
    serialized.tabs[1].description = { title: "wrong shape" };
    expect(hydrateTabs("ws-1", JSON.stringify(serialized))).toEqual(initialTabs("ws-1"));
  });
});

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
    title: `WorkerItem ${workerItemId.slice(0, 8)}`,
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

function inboxTab(workspaceId = "ws-1"): WorkspaceTab {
  return {
    id: "inbox",
    kind: "inbox",
    title: "信箱",
    href: `/workspaces/${workspaceId}/inbox`,
    closeable: false,
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

  it("keeps the inbox as a fixed semantic tab and returns to it from a task", () => {
    let state = reduceTabs(taskIndex(), registerDirectRoute(inboxTab()));
    state = reduceTabs(state, openTab(rootTaskTab("task-1")));

    expect(state.tabs.find((tab) => tab.id === "root-task:task-1")?.returnTabId).toBe("inbox");
    const closed = reduceTabs(state, closeTab("root-task:task-1"));

    expect(closed.activeTabId).toBe("inbox");
    expect(closed.navigationHref).toBe("/workspaces/ws-1/inbox");
    expect(closed.tabs).not.toContainEqual(expect.objectContaining({ id: "root-task:task-1" }));
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

  it("returns a newly opened tab to the tab that opened it", () => {
    let state = stateWithTaskSubtaskAndWorker();
    state = reduceTabs(state, activateTab("root-task:task-1"));
    state = reduceTabs(state, openTab(collaborationTab("task-1")));

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

  it("falls back to the nearest valid tab when a worker's parent was already closed", () => {
    let state = taskIndex();
    state = reduceTabs(state, openTab(rootTaskTab("task-1")));
    state = reduceTabs(state, openTab(workerTab("worker-1")));
    state = reduceTabs(state, closeTab("root-task:task-1"));

    const next = reduceTabs(state, closeTab("worker-item:worker-1"));

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

  it("maps a WorkerItem direct link to its own semantic tab", () => {
    expect(semanticTabForLocation("ws-1", {
      pathname: "/workspaces/ws-1/worker-items/worker-1",
      search: "",
    })).toEqual(workerTab("worker-1"));
  });

  it("maps the inbox route to its fixed semantic tab", () => {
    expect(semanticTabForLocation("ws-1", {
      pathname: "/workspaces/ws-1/inbox",
      search: "",
    })).toEqual(inboxTab());
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
