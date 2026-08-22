import { Boxes, Clock3, ListTodo, Mail, MessageCircle, Settings2 } from "lucide-react";
import type { ReactNode } from "react";
import { NavLink, Outlet, useLocation, useParams } from "react-router-dom";

import { WorkspaceStreamProvider, useWorkspaceStream } from "./WorkspaceStreamProvider";
import { WorkspaceTabs } from "./WorkspaceTabs";
import { StatusMark } from "@/shared/ui/StatusMark";

import { NotificationCenter } from "@/features/attention/NotificationCenter";

const DEFAULT_WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";

export function App() {
  const { workspaceId } = useParams<{ workspaceId: string }>();
  const resolvedWorkspaceId = workspaceId ?? DEFAULT_WORKSPACE_ID;

  return (
    <WorkspaceStreamProvider workspaceId={resolvedWorkspaceId}>
      <AppFrame workspaceId={resolvedWorkspaceId} />
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
            <NavItem to={`/workspaces/${workspaceId}/inbox`} label="信箱" icon={<Mail size={17} />} />
            <NavItem to={`/workspaces/${workspaceId}/chat`} label="聊天" icon={<MessageCircle size={17} />} />
            <NavItem to={`/workspaces/${workspaceId}/history`} label="历史" icon={<Clock3 size={17} />} />
            <NavItem to={`/workspaces/${workspaceId}/capabilities`} label="能力" icon={<Boxes size={17} />} />
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
}: {
  to: string;
  label: string;
  icon: ReactNode;
}) {
  return (
    <NavLink
      className={({ isActive }) => `navigation-item${isActive ? " is-active" : ""}`}
      to={to}
      aria-label={label}
    >
      {icon}
      <span>{label}</span>
    </NavLink>
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
