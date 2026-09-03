export const WORKSPACE_TABS_SCHEMA_VERSION = 5;

export type WorkspaceTabId =
  | "task-index"
  | "planning-new"
  | `planning:${string}`
  | `root-task:${string}`
  | `collaboration:${string}`
  | `subtask:${string}:${string}`
  | `worker-item:${string}`
  | `chat:${string}`
  | `knowledge-page:${string}`
  | `knowledge-candidate:${string}`
  | `context-package:${string}`;

export type WorkspaceTab = {
  returnTabId?: WorkspaceTabId;
} & (
  | {
      id: "task-index";
      kind: "task-index";
      title: "任务";
      href: string;
      closeable: false;
    }
  | {
      id: "planning-new";
      kind: "planning-new";
      title: string;
      href: string;
      closeable: true;
    }
  | {
      id: `planning:${string}`;
      kind: "planning";
      title: string;
      href: string;
      closeable: true;
    }
  | {
      id: `root-task:${string}`;
      kind: "root-task";
      title: string;
      href: string;
      closeable: true;
    }
  | {
      id: `collaboration:${string}`;
      kind: "collaboration";
      title: "协同记录";
      href: string;
      closeable: true;
    }
  | {
      id: `subtask:${string}:${string}`;
      kind: "subtask";
      title: string;
      href: string;
      closeable: true;
    }
  | {
      id: `worker-item:${string}`;
      kind: "worker-item";
      title: string;
      href: string;
      closeable: true;
    }
  | {
      id: `chat:${string}`;
      kind: "chat";
      title: "Main Agent";
      href: string;
      closeable: true;
    }
  | {
      id: `knowledge-page:${string}`;
      kind: "knowledge-page";
      title: string;
      href: string;
      closeable: true;
    }
  | {
      id: `knowledge-candidate:${string}`;
      kind: "knowledge-candidate";
      title: string;
      href: string;
      closeable: true;
    }
  | {
      id: `context-package:${string}`;
      kind: "context-package";
      title: string;
      href: string;
      closeable: true;
    }
);

export type WorkspaceTabsState = {
  workspaceId: string;
  tabs: WorkspaceTab[];
  activeTabId: WorkspaceTabId;
  navigationHref: string | null;
};

export type WorkspaceTabsAction =
  | { type: "open"; tab: WorkspaceTab }
  | { type: "activate"; id: WorkspaceTabId }
  | { type: "close"; id: WorkspaceTabId }
  | { type: "register-direct-route"; tab: WorkspaceTab }
  | { type: "clear-navigation" };

type SerializedTabs = {
  schemaVersion: typeof WORKSPACE_TABS_SCHEMA_VERSION;
  tabs: WorkspaceTab[];
  activeTabId: WorkspaceTabId;
};

export function initialTabs(workspaceId: string): WorkspaceTabsState {
  return {
    workspaceId,
    tabs: [
      {
        id: "task-index",
        kind: "task-index",
        title: "任务",
        href: `/workspaces/${workspaceId}/tasks`,
        closeable: false,
      },
    ],
    activeTabId: "task-index",
    navigationHref: null,
  };
}

export function openTab(tab: WorkspaceTab): WorkspaceTabsAction {
  return { type: "open", tab };
}

export function activateTab(id: WorkspaceTabId): WorkspaceTabsAction {
  return { type: "activate", id };
}

export function closeTab(id: WorkspaceTabId): WorkspaceTabsAction {
  return { type: "close", id };
}

export function registerDirectRoute(tab: WorkspaceTab): WorkspaceTabsAction {
  return { type: "register-direct-route", tab };
}

export function clearNavigation(): WorkspaceTabsAction {
  return { type: "clear-navigation" };
}

export function reduceTabs(
  state: WorkspaceTabsState,
  action: WorkspaceTabsAction,
): WorkspaceTabsState {
  switch (action.type) {
    case "open":
    case "register-direct-route": {
      const existingIndex = state.tabs.findIndex((tab) => tab.id === action.tab.id);
      const existingTab = existingIndex < 0 ? undefined : state.tabs[existingIndex];
      const returnTabId = action.tab.returnTabId
        ?? existingTab?.returnTabId
        ?? (
          existingIndex < 0
          && state.activeTabId !== action.tab.id
          && state.activeTabId !== "task-index"
            ? state.activeTabId
            : undefined
        );
      const tab = returnTabId === undefined
        ? action.tab
        : { ...action.tab, returnTabId };
      const tabs = existingIndex < 0
        ? [...state.tabs, tab]
        : state.tabs.map((item, index) => index === existingIndex ? tab : item);
      return {
        ...state,
        tabs,
        activeTabId: tab.id,
        navigationHref: null,
      };
    }
    case "activate": {
      if (!state.tabs.some((tab) => tab.id === action.id)) {
        return state;
      }
      return { ...state, activeTabId: action.id, navigationHref: null };
    }
    case "close": {
      if (action.id === "task-index") {
        return state;
      }
      const index = state.tabs.findIndex((tab) => tab.id === action.id);
      if (index < 0) {
        return state;
      }
      const tabs = state.tabs.filter((tab) => tab.id !== action.id);
      if (state.activeTabId !== action.id) {
        return { ...state, tabs, navigationHref: null };
      }
      const returnTarget = state.tabs[index]?.returnTabId;
      const target = (
        returnTarget === undefined
          ? undefined
          : tabs.find((tab) => tab.id === returnTarget)
      ) ?? tabs[Math.max(0, index - 1)] ?? tabs[0];
      if (target === undefined) {
        return initialTabs(state.workspaceId);
      }
      return {
        ...state,
        tabs,
        activeTabId: target.id,
        navigationHref: target.href,
      };
    }
    case "clear-navigation":
      return state.navigationHref === null
        ? state
        : { ...state, navigationHref: null };
  }
}

export function storageKey(workspaceId: string): string {
  return `sidera.workspace-tabs.v5:${workspaceId}`;
}

export function serializeTabs(state: WorkspaceTabsState): string {
  const payload: SerializedTabs = {
    schemaVersion: WORKSPACE_TABS_SCHEMA_VERSION,
    tabs: state.tabs,
    activeTabId: state.activeTabId,
  };
  return JSON.stringify(payload);
}

export function hydrateTabs(
  workspaceId: string,
  raw: string | null | undefined,
): WorkspaceTabsState {
  const fallback = initialTabs(workspaceId);
  if (raw === null || raw === undefined) {
    return fallback;
  }
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!isSerializedTabs(parsed) || !isValidTabSet(workspaceId, parsed.tabs)) {
      return fallback;
    }
    if (!parsed.tabs.some((tab) => tab.id === parsed.activeTabId)) {
      return fallback;
    }
    return {
      workspaceId,
      tabs: parsed.tabs,
      activeTabId: parsed.activeTabId,
      navigationHref: null,
    };
  } catch {
    return fallback;
  }
}

export function loadTabs(workspaceId: string): WorkspaceTabsState {
  if (typeof window === "undefined") {
    return initialTabs(workspaceId);
  }
  return hydrateTabs(workspaceId, window.sessionStorage.getItem(storageKey(workspaceId)));
}

export function saveTabs(state: WorkspaceTabsState): void {
  if (typeof window === "undefined") {
    return;
  }
  window.sessionStorage.setItem(storageKey(state.workspaceId), serializeTabs(state));
}

function isSerializedTabs(value: unknown): value is SerializedTabs {
  if (!isRecord(value) || value.schemaVersion !== WORKSPACE_TABS_SCHEMA_VERSION) {
    return false;
  }
  return Array.isArray(value.tabs) && isTabId(value.activeTabId);
}

function isValidTabSet(workspaceId: string, tabs: unknown[]): tabs is WorkspaceTab[] {
  const firstTab = tabs[0];
  if (firstTab === undefined || !isWorkspaceTab(firstTab, workspaceId)) {
    return false;
  }
  const ids = new Set<string>();
  for (const value of tabs) {
    if (!isWorkspaceTab(value, workspaceId) || ids.has(value.id)) {
      return false;
    }
    ids.add(value.id);
  }
  return firstTab.id === "task-index";
}

function isWorkspaceTab(value: unknown, workspaceId: string): value is WorkspaceTab {
  if (!isRecord(value) || !isTabId(value.id) || typeof value.kind !== "string") {
    return false;
  }
  if (typeof value.title !== "string" || typeof value.href !== "string") {
    return false;
  }
  if (value.id === "task-index") {
    return value.kind === "task-index"
      && value.title === "任务"
      && value.closeable === false
      && value.href === `/workspaces/${workspaceId}/tasks`;
  }
  if (value.id === "planning-new") {
    return value.kind === "planning-new"
      && value.title === "新建任务"
      && value.closeable === true
      && isWorkspaceHref(value.href, workspaceId);
  }
  if (value.id.startsWith("chat:")) {
    return value.kind === "chat"
      && value.title === "Main Agent"
      && value.closeable === true
      && isWorkspaceHref(value.href, workspaceId);
  }
  if (value.id.startsWith("knowledge-page:")) {
    return value.kind === "knowledge-page"
      && value.closeable === true
      && isWorkspaceHref(value.href, workspaceId);
  }
  if (value.id.startsWith("knowledge-candidate:")) {
    return value.kind === "knowledge-candidate"
      && value.closeable === true
      && isWorkspaceHref(value.href, workspaceId);
  }
  if (value.id.startsWith("context-package:")) {
    return value.kind === "context-package"
      && value.closeable === true
      && isWorkspaceHref(value.href, workspaceId);
  }
  return value.closeable === true
    && ((value.id.startsWith("planning:") && value.kind === "planning")
      || (value.id.startsWith("root-task:") && value.kind === "root-task")
      || (value.id.startsWith("collaboration:") && value.kind === "collaboration" && value.title === "协同记录")
      || (value.id.startsWith("subtask:") && value.kind === "subtask")
      || (value.id.startsWith("worker-item:") && value.kind === "worker-item"))
    && isWorkspaceHref(value.href, workspaceId);
}

function isWorkspaceHref(href: string, workspaceId: string): boolean {
  try {
    const url = new URL(href, "http://sidera.local");
    const prefix = `/workspaces/${workspaceId}`;
    return url.origin === "http://sidera.local"
      && url.pathname.startsWith(`${prefix}/`);
  } catch {
    return false;
  }
}

function isTabId(value: unknown): value is WorkspaceTabId {
  return value === "task-index"
    || (typeof value === "string" && (
      value === "planning-new"
      || value.startsWith("planning:")
      || value.startsWith("root-task:")
      || value.startsWith("collaboration:")
      || value.startsWith("subtask:")
      || value.startsWith("worker-item:")
      || value.startsWith("chat:")
      || value.startsWith("knowledge-page:")
      || value.startsWith("knowledge-candidate:")
      || value.startsWith("context-package:")
    ));
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
