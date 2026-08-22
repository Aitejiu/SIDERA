import { Plus, X } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { getTaskGraph, taskGraphQueryKey } from "@/features/tasks/api";
import type { TaskGraph } from "@/features/tasks/types";

type WorkspaceTab = {
  key: string;
  href: string;
  label: string;
  detail: string;
};

export function WorkspaceTabs({ workspaceId }: { workspaceId: string }) {
  const location = useLocation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const taskRootId = rootTaskIdFromPath(location.pathname);
  const currentHref = `${location.pathname}${location.search}`;
  const currentWorkItem = new URLSearchParams(location.search).get("workItem");
  const graphQuery = useQuery({
    queryKey: taskRootId === null ? ["task-graph", workspaceId, "none"] : taskGraphQueryKey(workspaceId, taskRootId),
    queryFn: ({ signal }) => getTaskGraph(workspaceId, taskRootId ?? "", signal),
    enabled: taskRootId !== null,
  });
  const graph = taskRootId === null
    ? undefined
    : graphQuery.data ?? queryClient.getQueryData<TaskGraph>(taskGraphQueryKey(workspaceId, taskRootId));
  const taskTitle = graph?.overview.title ?? (taskRootId === null ? "任务" : `任务 ${taskRootId.slice(0, 8)}`);
  const [tabs, setTabs] = useState<WorkspaceTab[]>(() => {
    const initial: WorkspaceTab[] = [{
      key: "task-index",
      href: `/workspaces/${workspaceId}/tasks`,
      label: "任务",
      detail: "总览",
    }];
    if (taskRootId === null) {
      return initial;
    }
    return [...initial, {
      key: `task:${currentHref}`,
      href: currentHref,
      label: taskTitle,
      detail: currentWorkItem ?? "任务",
    }];
  });

  useEffect(() => {
    const handleLinkClick = (event: MouseEvent) => {
      const link = (event.target as Element).closest<HTMLAnchorElement>("a[href]");
      if (link === null) return;
      const url = new URL(link.href, window.location.origin);
      const rootId = rootTaskIdFromPath(url.pathname);
      if (rootId === null) return;
      const href = `${url.pathname}${url.search}`;
      const cachedGraph = queryClient.getQueryData<TaskGraph>(taskGraphQueryKey(workspaceId, rootId));
      setTabs((current) => current.some((tab) => tab.href === href) ? current : [...current, {
        key: `task:${href}`,
        href,
        label: cachedGraph?.overview.title ?? `任务 ${rootId.slice(0, 8)}`,
        detail: new URL(url).searchParams.get("workItem") ?? "任务",
      }]);
    };
    window.addEventListener("click", handleLinkClick);
    return () => window.removeEventListener("click", handleLinkClick);
  }, [queryClient, workspaceId]);

  const visibleTabs = tabs;

  const closeTab = (tab: WorkspaceTab) => {
    if (tab.key === "task-index") {
      return;
    }
    const index = tabs.findIndex((item) => item.key === tab.key);
    const remaining = tabs.filter((item) => item.key !== tab.key);
    setTabs(remaining);
    if (tab.href === currentHref) {
      navigate(remaining[Math.max(0, index - 1)]?.href ?? `/workspaces/${workspaceId}/tasks`);
    }
  };

  return (
    <nav className="workspace-tabs" aria-label="已打开的任务">
      {visibleTabs.map((tab) => {
        const isActive = tab.href === currentHref || (tab.key === "task-index" && location.pathname.endsWith("/tasks"));
        const tabRootId = rootTaskIdFromPath(tab.href);
        const tabGraph = tabRootId === null ? undefined : queryClient.getQueryData<TaskGraph>(taskGraphQueryKey(workspaceId, tabRootId));
        const label = tabGraph?.overview.title ?? tab.label;
        const accessibleLabel = tab.key === "task-index"
          ? "任务总览"
          : label.startsWith("任务 ") ? label : `任务 ${label}`;
        return (
          <div className={`workspace-tab${isActive ? " is-active" : ""}`} key={tab.key}>
            <Link
              className="workspace-tab__link"
              to={tab.href}
              aria-current={isActive ? "page" : undefined}
              aria-label={accessibleLabel}
            >
              <span className="workspace-tab__label">{label}</span>
              <span className="workspace-tab__detail">{tab.detail}</span>
            </Link>
            {tab.key !== "task-index" ? (
              <button
                className="workspace-tab__close"
                type="button"
                aria-label={`关闭${accessibleLabel}`}
                title={`关闭${accessibleLabel}`}
                onClick={() => closeTab(tab)}
              >
                <X size={13} aria-hidden="true" />
              </button>
            ) : null}
          </div>
        );
      })}
      <Link
        className="workspace-tab__new"
        to={`/workspaces/${workspaceId}/tasks`}
        aria-label="打开任务"
        title="打开任务"
      >
        <Plus size={15} aria-hidden="true" />
      </Link>
    </nav>
  );
}

function rootTaskIdFromPath(pathname: string): string | null {
  const match = pathname.match(/\/tasks\/([^/]+)/);
  return match?.[1] ?? null;
}
