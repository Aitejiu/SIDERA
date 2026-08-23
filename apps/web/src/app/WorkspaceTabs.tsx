import { Plus, X } from "lucide-react";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useReducer,
  type ReactNode,
} from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { getTaskGraph, taskGraphQueryKey } from "@/features/tasks/api";
import type { TaskGraph } from "@/features/tasks/types";

import {
  activateTab,
  clearNavigation,
  closeTab,
  loadTabs,
  openTab,
  reduceTabs,
  registerDirectRoute,
  saveTabs,
  type WorkspaceTab,
  type WorkspaceTabId,
  type WorkspaceTabsState,
} from "./workspaceTabsState";

type WorkspaceTabsContextValue = {
  state: WorkspaceTabsState;
  open: (tab: WorkspaceTab) => void;
  activate: (id: WorkspaceTabId) => void;
  close: (id: WorkspaceTabId) => void;
  registerDirectRoute: (tab: WorkspaceTab) => void;
};

const WorkspaceTabsContext = createContext<WorkspaceTabsContextValue | null>(null);

export function WorkspaceTabsProvider({
  workspaceId,
  children,
}: {
  workspaceId: string;
  children: ReactNode;
}) {
  const navigate = useNavigate();
  const [state, dispatch] = useReducer(
    reduceTabs,
    workspaceId,
    loadTabs,
  );

  useEffect(() => {
    saveTabs(state);
  }, [state]);

  useEffect(() => {
    if (state.navigationHref === null) {
      return;
    }
    const href = state.navigationHref;
    dispatch(clearNavigation());
    navigate(href, { replace: true });
  }, [navigate, state.navigationHref]);

  const open = useCallback((tab: WorkspaceTab) => {
    dispatch(openTab(tab));
    navigate(tab.href);
  }, [navigate]);
  const activate = useCallback((id: WorkspaceTabId) => {
    const tab = state.tabs.find((item) => item.id === id);
    dispatch(activateTab(id));
    if (tab !== undefined) {
      navigate(tab.href);
    }
  }, [navigate, state.tabs]);
  const close = useCallback((id: WorkspaceTabId) => {
    dispatch(closeTab(id));
  }, []);
  const register = useCallback((tab: WorkspaceTab) => {
    dispatch(registerDirectRoute(tab));
  }, []);

  const value = useMemo<WorkspaceTabsContextValue>(() => ({
    state,
    open,
    activate,
    close,
    registerDirectRoute: register,
  }), [activate, close, open, register, state]);

  return (
    <WorkspaceTabsContext.Provider value={value}>
      {children}
    </WorkspaceTabsContext.Provider>
  );
}

export function useWorkspaceTabs(): WorkspaceTabsContextValue {
  const value = useContext(WorkspaceTabsContext);
  if (value === null) {
    throw new Error("useWorkspaceTabs must be used within WorkspaceTabsProvider");
  }
  return value;
}

export function WorkspaceTabs({ workspaceId }: { workspaceId: string }) {
  const location = useLocation();
  const { pathname, search } = location;
  const { state, activate, close, registerDirectRoute } = useWorkspaceTabs();
  const queryClient = useQueryClient();
  const currentRoute = useMemo(
    () => semanticTabForLocation(workspaceId, { pathname, search }),
    [pathname, search, workspaceId],
  );
  const rootTaskId = rootTaskIdFromPath(pathname, workspaceId);
  const graphQuery = useQuery({
    queryKey: rootTaskId === null
      ? ["task-graph", workspaceId, "none"]
      : taskGraphQueryKey(workspaceId, rootTaskId),
    queryFn: ({ signal }) => getTaskGraph(workspaceId, rootTaskId ?? "", signal),
    enabled: rootTaskId !== null,
  });
  const graph = rootTaskId === null
    ? undefined
    : graphQuery.data ?? queryClient.getQueryData<TaskGraph>(taskGraphQueryKey(workspaceId, rootTaskId));

  useEffect(() => {
    if (currentRoute === null) {
      return;
    }
    const title = graph?.overview.title;
    const route = currentRoute.kind === "task-index"
      || currentRoute.kind === "chat"
      || title === undefined
      ? currentRoute
      : { ...currentRoute, title };
    registerDirectRoute(route);
  }, [currentRoute, graph?.overview.title, registerDirectRoute]);

  return (
    <nav className="workspace-tabs" aria-label="已打开的任务">
      {state.tabs.map((tab) => {
        const isActive = tab.id === state.activeTabId;
        const accessibleLabel = tab.id === "task-index"
          ? "任务总览"
          : tab.title;
        return (
          <div className={`workspace-tab${isActive ? " is-active" : ""}`} key={tab.id}>
            <Link
              className="workspace-tab__link"
              to={tab.href}
              aria-current={isActive ? "page" : undefined}
              aria-label={accessibleLabel}
              onClick={(event) => {
                event.preventDefault();
                activate(tab.id);
              }}
            >
              <span className="workspace-tab__label">{tab.title}</span>
              <span className="workspace-tab__detail">{tabDetail(tab)}</span>
            </Link>
            {tab.closeable ? (
              <button
                className="workspace-tab__close"
                type="button"
                aria-label={`关闭${accessibleLabel}`}
                title={`关闭${accessibleLabel}`}
                onClick={() => close(tab.id)}
              >
                <X size={13} aria-hidden="true" />
              </button>
            ) : null}
          </div>
        );
      })}
      <Link
        className="workspace-tab__new"
        to={`/workspaces/${workspaceId}/planning/new`}
        aria-label="新建任务"
        title="新建任务"
      >
        <Plus size={15} aria-hidden="true" />
      </Link>
    </nav>
  );
}

function semanticTabForLocation(
  workspaceId: string,
  location: { pathname: string; search: string },
): WorkspaceTab | null {
  const taskIndexPath = `/workspaces/${workspaceId}/tasks`;
  if (location.pathname === taskIndexPath) {
    return {
      id: "task-index",
      kind: "task-index",
      title: "任务",
      href: `${location.pathname}${location.search}`,
      closeable: false,
    };
  }

  const planningNewPath = `/workspaces/${workspaceId}/planning/new`;
  if (location.pathname === planningNewPath) {
    return {
      id: "planning-new",
      kind: "planning-new",
      title: "新建任务",
      href: `${location.pathname}${location.search}`,
      closeable: true,
    };
  }

  const chatMatch = location.pathname.match(
    new RegExp(`^/workspaces/${escapeRegExp(workspaceId)}/chat/([^/]+)$`),
  );
  if (chatMatch?.[1] !== undefined) {
    const threadId = decodeURIComponent(chatMatch[1]);
    return {
      id: `chat:${threadId}`,
      kind: "chat",
      title: "Main Agent" as const,
      href: `${location.pathname}${location.search}`,
      closeable: true,
    };
  }

  const planningMatch = location.pathname.match(
    new RegExp(`^/workspaces/${escapeRegExp(workspaceId)}/planning/([^/]+)$`),
  );
  if (planningMatch?.[1] !== undefined) {
    const planningRequestId = decodeURIComponent(planningMatch[1]);
    return {
      id: `planning:${planningRequestId}`,
      kind: "planning",
      title: `规划 ${planningRequestId.slice(0, 8)}`,
      href: `${location.pathname}${location.search}`,
      closeable: true,
    };
  }

  const subtaskMatch = location.pathname.match(
    new RegExp(`^/workspaces/${escapeRegExp(workspaceId)}/tasks/([^/]+)/subtasks/([^/]+)$`),
  );
  if (subtaskMatch?.[1] !== undefined && subtaskMatch[2] !== undefined) {
    const taskId = decodeURIComponent(subtaskMatch[1]);
    const subtaskKey = decodeURIComponent(subtaskMatch[2]);
    return {
      id: `subtask:${taskId}:${subtaskKey}`,
      kind: "subtask",
      title: subtaskKey,
      href: `${location.pathname}${location.search}`,
      closeable: true,
    };
  }

  const workerMatch = location.pathname.match(
    new RegExp(`^/workspaces/${escapeRegExp(workspaceId)}/worker-items/([^/]+)$`),
  );
  if (workerMatch?.[1] !== undefined) {
    const workerItemId = decodeURIComponent(workerMatch[1]);
    return {
      id: `worker-item:${workerItemId}`,
      kind: "worker-item",
      title: `WorkerItem ${workerItemId.slice(0, 8)}`,
      href: `${location.pathname}${location.search}`,
      closeable: true,
    };
  }

  const rootTaskId = rootTaskIdFromPath(location.pathname, workspaceId);
  if (rootTaskId !== null) {
    const basePath = `/workspaces/${workspaceId}/tasks/${rootTaskId}`;
    const href = location.pathname === basePath
      ? `${location.pathname}${location.search}`
      : basePath;
    return {
      id: `root-task:${rootTaskId}`,
      kind: "root-task",
      title: `任务 ${rootTaskId.slice(0, 8)}`,
      href,
      closeable: true,
    };
  }
  return null;
}

function rootTaskIdFromPath(pathname: string, workspaceId: string): string | null {
  const match = pathname.match(
    new RegExp(`^/workspaces/${escapeRegExp(workspaceId)}/tasks/([^/]+)`),
  );
  if (match?.[1] === undefined || match[1] === "") {
    return null;
  }
  return decodeURIComponent(match[1]);
}

function tabDetail(tab: WorkspaceTab): string {
  switch (tab.kind) {
    case "task-index":
      return "总览";
    case "planning-new":
      return "规划入口";
    case "planning":
      return "规划请求";
    case "root-task":
      return "任务";
    case "subtask":
      return "子任务";
    case "worker-item":
      return "WorkerItem";
    case "chat":
      return "对话";
  }
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
