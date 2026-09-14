import { BookOpen, Bot, FileText, Folder, GitBranch, Inbox, ListTodo, MessageCircle, Package, Plus, X, type LucideIcon } from "lucide-react";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  type ReactNode,
} from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { getTaskGraph, taskGraphQueryKey } from "@/features/tasks/api";
import { getWorkerItemWorkspace, workerWorkspaceQueryKey } from "@/features/worker-items/api";

import { WorkspaceViewStateProvider } from "./WorkspaceViewState";

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
      <WorkspaceViewStateProvider openTabIds={state.tabs.map(tab => tab.id)}>
        {children}
      </WorkspaceViewStateProvider>
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
  const stripRef = useRef<HTMLElement>(null);
  const location = useLocation();
  const { pathname, search } = location;
  const { state, activate, close, registerDirectRoute } = useWorkspaceTabs();
  const currentRoute = useMemo(
    () => semanticTabForLocation(workspaceId, { pathname, search }),
    [pathname, search, workspaceId],
  );
  const workerItemId = currentRoute?.kind === "worker-item" ? currentRoute.id.slice("worker-item:".length) : "";
  const workerQuery = useQuery({
    queryKey: workerWorkspaceQueryKey(workspaceId, workerItemId),
    queryFn: ({ signal }) => getWorkerItemWorkspace(workspaceId, workerItemId, signal),
    enabled: workerItemId.length > 0,
  });
  const rootTaskId = workerItemId
    ? workerQuery.data?.worker_item.root_task_id ?? null
    : rootTaskIdFromPath(pathname, workspaceId);
  const graphQuery = useQuery({
    queryKey: rootTaskId === null
      ? ["task-graph", workspaceId, "none"]
      : taskGraphQueryKey(workspaceId, rootTaskId),
    queryFn: ({ signal }) => getTaskGraph(workspaceId, rootTaskId ?? "", signal),
    enabled: rootTaskId !== null,
  });
  const graph = graphQuery.data;
  const subtaskKey = currentRoute?.kind === "subtask"
    ? currentRoute.id.slice(`subtask:${rootTaskId}:`.length)
    : workerQuery.data?.current_placement.subtask_key;
  const subtaskTitle = graph?.nodes.find(node => node.subtask_key === subtaskKey)?.title;
  const rootTitle = graph?.overview.title;
  const workerTitle = workerQuery.data?.worker_item.title;
  const resolvedTitle = currentRoute?.kind === "root-task" ? rootTitle
    : currentRoute?.kind === "subtask" ? subtaskTitle
      : currentRoute?.kind === "worker-item" ? workerTitle : undefined;
  const resolvedPath = currentRoute?.kind === "worker-item" && workerTitle && rootTitle && subtaskTitle
    ? `${rootTitle} / ${subtaskTitle} / ${workerTitle}`
    : currentRoute?.kind === "subtask" && rootTitle && subtaskTitle
      ? `${rootTitle} / ${subtaskTitle}`
      : currentRoute?.kind === "root-task" ? rootTitle : undefined;
  const existingRoute = state.tabs.find(tab => tab.id === currentRoute?.id);
  const existingTitle = existingRoute?.title;
  const existingDescription = existingRoute?.description;

  useEffect(() => {
    if (currentRoute === null || state.navigationHref !== null) {
      return;
    }
    const route = currentRoute.kind === "root-task" || currentRoute.kind === "subtask" || currentRoute.kind === "worker-item"
      ? {
        ...currentRoute,
        title: resolvedTitle ?? existingTitle ?? currentRoute.title,
        description: resolvedPath
          ? `${tabDetail(currentRoute)} · ${resolvedPath}`
          : existingDescription ?? `${tabDetail(currentRoute)} · ${resolvedTitle ?? existingTitle ?? currentRoute.title}`,
      }
      : currentRoute;
    registerDirectRoute(route);
  }, [currentRoute, resolvedTitle, resolvedPath, existingTitle, existingDescription, registerDirectRoute, state.navigationHref]);

  useEffect(() => {
    const strip = stripRef.current;
    const active = strip?.querySelector<HTMLElement>(".workspace-tab.is-active");
    if (!strip || !active) return;
    const revealActive = () => {
      const bounds = active.getBoundingClientRect();
      const viewport = strip.getBoundingClientRect();
      if (bounds.left < viewport.left) strip.scrollLeft -= viewport.left - bounds.left + 8;
      else if (bounds.right > viewport.right) strip.scrollLeft += bounds.right - viewport.right + 8;
    };
    revealActive();
    if (typeof ResizeObserver !== "function") return;
    const observer = new ResizeObserver(revealActive);
    observer.observe(strip);
    observer.observe(active);
    return () => observer.disconnect();
  }, [state.activeTabId]);

  return (
    <nav className="workspace-tabs" aria-label="已打开的任务" ref={stripRef}>
      {state.tabs.map((tab) => {
        const isActive = tab.id === state.activeTabId;
        const accessibleLabel = tab.id === "task-index"
          ? "任务总览"
          : tab.title;
        const Icon = tabIcon(tab);
        const description = tab.description ?? `${tabDetail(tab)} · ${tab.title}`;
        const descriptionId = `workspace-tab-description-${encodeURIComponent(tab.id)}`;
        return (
          <div className={`workspace-tab${isActive ? " is-active" : ""}${!tab.closeable ? " workspace-tab--pinned" : ""}`} key={tab.id}>
            <Link
              className="workspace-tab__link"
              to={tab.href}
              aria-current={isActive ? "page" : undefined}
              aria-label={accessibleLabel}
              aria-describedby={descriptionId}
              title={description}
              onClick={(event) => {
                if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
                event.preventDefault();
                activate(tab.id);
              }}
            >
              <Icon className="workspace-tab__icon" size={15} aria-hidden="true" />
              <span className="workspace-tab__label">{tab.title}</span>
              <span className="workspace-tab__detail sr-only" id={descriptionId}>{description}</span>
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

export function semanticTabForLocation(
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

  const inboxPath = `/workspaces/${workspaceId}/inbox`;
  if (location.pathname === inboxPath) {
    return {
      id: "inbox",
      kind: "inbox",
      title: "信箱",
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

  const collaborationMatch = location.pathname.match(
    new RegExp(`^/workspaces/${escapeRegExp(workspaceId)}/tasks/([^/]+)/collaboration$`),
  );
  if (collaborationMatch?.[1] !== undefined) {
    const rootTaskId = decodeURIComponent(collaborationMatch[1]);
    return {
      id: `collaboration:${rootTaskId}`,
      kind: "collaboration",
      title: "协同记录",
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

  const knowledgePageMatch = location.pathname.match(
    new RegExp(`^/workspaces/${escapeRegExp(workspaceId)}/knowledge/pages/([^/]+)$`),
  );
  if (knowledgePageMatch?.[1] !== undefined) {
    const pageId = decodeURIComponent(knowledgePageMatch[1]);
    return {
      id: `knowledge-page:${pageId}`,
      kind: "knowledge-page",
      title: "知识页",
      href: `${location.pathname}${location.search}`,
      closeable: true,
    };
  }

  const knowledgeCandidateMatch = location.pathname.match(
    new RegExp(`^/workspaces/${escapeRegExp(workspaceId)}/knowledge/candidates/([^/]+)$`),
  );
  if (knowledgeCandidateMatch?.[1] !== undefined) {
    const candidateId = decodeURIComponent(knowledgeCandidateMatch[1]);
    return {
      id: `knowledge-candidate:${candidateId}`,
      kind: "knowledge-candidate",
      title: "知识候选",
      href: `${location.pathname}${location.search}`,
      closeable: true,
    };
  }

  const contextPackageMatch = location.pathname.match(
    new RegExp(`^/workspaces/${escapeRegExp(workspaceId)}/context-packages/([^/]+)$`),
  );
  if (contextPackageMatch?.[1] !== undefined) {
    const packageId = decodeURIComponent(contextPackageMatch[1]);
    return {
      id: `context-package:${packageId}`,
      kind: "context-package",
      title: "上下文包",
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

  const revisionMatch = location.pathname.match(
    new RegExp(`^/workspaces/${escapeRegExp(workspaceId)}/tasks/([^/]+)/revisions/([^/]+)$`),
  );
  if (revisionMatch?.[1] !== undefined && revisionMatch[2] !== undefined) {
    const rootTaskId = decodeURIComponent(revisionMatch[1]);
    return {
      id: `root-task:${rootTaskId}`,
      kind: "root-task",
      title: `任务 ${rootTaskId.slice(0, 8)}`,
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
    case "inbox":
      return "通知";
    case "planning-new":
      return "规划入口";
    case "planning":
      return "规划请求";
    case "root-task":
      return "任务";
    case "collaboration":
      return "协同记录";
    case "subtask":
      return "子任务";
    case "worker-item":
      return "WorkerItem";
    case "chat":
      return "对话";
    case "knowledge-page":
      return "知识页";
    case "knowledge-candidate":
      return "候选";
    case "context-package":
      return "上下文包";
  }
}

function tabIcon(tab: WorkspaceTab): LucideIcon {
  switch (tab.kind) {
    case "task-index": return ListTodo;
    case "root-task": return Folder;
    case "subtask": return GitBranch;
    case "worker-item": return Bot;
    case "inbox": return Inbox;
    case "chat": return MessageCircle;
    case "knowledge-page":
    case "knowledge-candidate": return BookOpen;
    case "context-package": return Package;
    default: return FileText;
  }
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
