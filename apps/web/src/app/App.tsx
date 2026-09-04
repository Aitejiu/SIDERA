import { BookOpen, Boxes, Clock3, ListTodo, Mail, MessageCircle, ServerCog, Settings2 } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { NavLink, Outlet, useLocation, useParams } from "react-router-dom";

import { WorkspaceStreamProvider, useWorkspaceStream } from "./WorkspaceStreamProvider";
import { WorkspaceTabs, WorkspaceTabsProvider } from "./WorkspaceTabs";
import { StatusMark } from "@/shared/ui/StatusMark";

import { NotificationCenter } from "@/features/attention/NotificationCenter";
import { getNotificationSummary, notificationSummaryQueryKey } from "@/features/notifications/api";

const DEFAULT_WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";

export function App() {
  const { workspaceId } = useParams<{ workspaceId: string }>();
  const resolvedWorkspaceId = workspaceId ?? DEFAULT_WORKSPACE_ID;

  return (
    <WorkspaceStreamProvider workspaceId={resolvedWorkspaceId}>
      <WorkspaceTabsProvider key={resolvedWorkspaceId} workspaceId={resolvedWorkspaceId}>
        <AppFrame workspaceId={resolvedWorkspaceId} />
      </WorkspaceTabsProvider>
    </WorkspaceStreamProvider>
  );
}

function AppFrame({ workspaceId }: { workspaceId: string }) {
  const { streamState } = useWorkspaceStream();

  return (
    <div className="control-room">
      <header className="topbar">
        <NavLink className="product-mark" to={`/workspaces/${workspaceId}/tasks`}>
          <span className="product-mark__glyph" aria-hidden="true">
            S
          </span>
          <span>SIDERA</span>
        </NavLink>
        <div className="topbar__context">
          <StatusMark kind={streamState.status} />
          <span className="mono-label">LOCAL CONTROL PLANE</span>
          <span className="mono-label">{formatEventTime(streamState.lastEventAt)}</span>
        </div>
      </header>

      <div className="room-layout">
        <aside className="navigation-rail" aria-label="主导航">
          <div className="workspace-context">
            <span className="eyebrow">工作区</span>
            <strong>SIDERA Local</strong>
            <span className="mono-label">{workspaceId.slice(0, 8)}</span>
          </div>
          <nav className="navigation-list">
            <NavItem to={`/workspaces/${workspaceId}/tasks`} label="任务" icon={<ListTodo size={17} />} />
            <NavItem to={`/workspaces/${workspaceId}/knowledge`} label="知识" icon={<BookOpen size={17} />} />
            <NavItem
              to={`/workspaces/${workspaceId}/inbox`}
              label="信箱"
              icon={<Mail size={17} />}
              badge={<NotificationNavBadge workspaceId={workspaceId} />}
            />
            <NavItem to={`/workspaces/${workspaceId}/chat`} label="聊天" icon={<MessageCircle size={17} />} />
            <NavItem to={`/workspaces/${workspaceId}/history`} label="历史" icon={<Clock3 size={17} />} />
            <NavItem to={`/workspaces/${workspaceId}/capabilities`} label="能力" icon={<Boxes size={17} />} />
            <NavItem to={`/workspaces/${workspaceId}/workers`} label="执行节点" icon={<ServerCog size={17} />} />
            <NavItem
              to={`/workspaces/${workspaceId}/settings`}
              label="设置"
              icon={<Settings2 size={17} />}
              className="navigation-item--compact-settings"
            />
          </nav>
          <div className="rail-footer">
            <NavItem to={`/workspaces/${workspaceId}/settings`} label="设置" icon={<Settings2 size={17} />} />
            <span className="eyebrow">连接状态</span>
            <StatusMark kind={streamState.status} />
          </div>
        </aside>

        <main className="main-surface">
          <WorkspaceTabs workspaceId={workspaceId} />
          <div className="route-frame">
            <Outlet />
          </div>
        </main>
      </div>
      <NotificationCenter />
    </div>
  );
}

function NavItem({
  to,
  label,
  icon,
  badge,
  className,
}: {
  to: string;
  label: string;
  icon: ReactNode;
  badge?: ReactNode;
  className?: string;
}) {
  return (
    <NavLink
      className={({ isActive }) => `navigation-item${isActive ? " is-active" : ""}${className === undefined ? "" : ` ${className}`}`}
      to={to}
      aria-label={label}
    >
      {icon}
      <span>{label}</span>
      {badge}
    </NavLink>
  );
}

function NotificationNavBadge({ workspaceId }: { workspaceId: string }) {
  const query = useQuery({
    queryKey: notificationSummaryQueryKey(workspaceId),
    queryFn: ({ signal }) => getNotificationSummary(workspaceId, signal),
    staleTime: 15_000,
  });
  if (query.data === undefined || query.data.open_count <= 0) {
    return null;
  }
  return (
    <span className="navigation-item__badge" aria-label={`需要处理 ${query.data.open_count}`}>
      {query.data.open_count}
    </span>
  );
}

function formatEventTime(timestamp: number | null): string {
  if (timestamp === null) {
    return "未接收事件";
  }
  return new Date(timestamp).toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function TaskRoutePlaceholder() {
  const location = useLocation();
  if (location.pathname.endsWith("/settings")) {
    return <NotificationCenter mode="settings" />;
  }
  return (
    <section className="route-placeholder" aria-labelledby="workspace-title">
      <div className="breadcrumb">工作区 / 任务</div>
      <div className="page-heading">
        <div>
          <span className="eyebrow">TASK FIRST CONTROL ROOM</span>
          <h1 id="workspace-title">任务</h1>
        </div>
        <span className="mono-label">SNAPSHOT · WAITING FOR API</span>
      </div>
      <div className="placeholder-line" aria-hidden="true" />
    </section>
  );
}

export function SubtaskRoutePlaceholder() {
  return (
    <section className="route-placeholder" aria-labelledby="subtask-placeholder-title">
      <div className="breadcrumb">任务 / 子任务</div>
      <div className="page-heading">
        <div>
          <span className="eyebrow">SUBTASK WORKSPACE</span>
          <h1 id="subtask-placeholder-title">正在打开子任务</h1>
        </div>
        <span className="mono-label">WORKSPACE LOADING</span>
      </div>
      <div className="placeholder-line" aria-hidden="true" />
    </section>
  );
}

export function KnowledgeRoutePlaceholder() {
  const location = useLocation();
  const isPage = location.pathname.includes("/knowledge/pages/");
  const isCandidate = location.pathname.includes("/knowledge/candidates/");
  const title = isPage ? "知识页" : isCandidate ? "知识候选" : "知识";
  const detail = isPage ? "PAGE DETAIL" : isCandidate ? "CANDIDATE REVIEW" : "LIBRARY";
  return (
    <section className="route-placeholder" aria-labelledby="knowledge-placeholder-title">
      <div className="breadcrumb">工作区 / 知识</div>
      <div className="page-heading">
        <div>
          <span className="eyebrow">KNOWLEDGE REFERENCE</span>
          <h1 id="knowledge-placeholder-title">{title}</h1>
        </div>
        <span className="mono-label">{detail} · LOADING</span>
      </div>
      <div className="placeholder-line" aria-hidden="true" />
    </section>
  );
}

export function ContextPackageRoutePlaceholder() {
  return (
    <section className="route-placeholder" aria-labelledby="context-package-placeholder-title">
      <div className="breadcrumb">工作区 / 上下文包</div>
      <div className="page-heading">
        <div>
          <span className="eyebrow">CONTEXT PACKAGE</span>
          <h1 id="context-package-placeholder-title">上下文包</h1>
        </div>
        <span className="mono-label">MANIFEST · LOADING</span>
      </div>
      <div className="placeholder-line" aria-hidden="true" />
    </section>
  );
}
