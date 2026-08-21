import { Activity, ListTodo, Settings2 } from "lucide-react";
import { NavLink, Outlet } from "react-router-dom";

const DEFAULT_WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";

export function App() {
  return (
    <div className="control-room">
      <header className="topbar">
        <NavLink className="product-mark" to={`/workspaces/${DEFAULT_WORKSPACE_ID}/tasks`}>
          <span className="product-mark__glyph" aria-hidden="true">
            S
          </span>
          <span>SIDERA</span>
        </NavLink>
        <div className="topbar__context">
          <span className="connection-mark" aria-label="控制面在线" title="控制面在线" />
          <span className="mono-label">LOCAL CONTROL PLANE</span>
        </div>
      </header>

      <div className="room-layout">
        <aside className="navigation-rail" aria-label="主导航">
          <div className="workspace-context">
            <span className="eyebrow">工作区</span>
            <strong>SIDERA Local</strong>
            <span className="mono-label">{DEFAULT_WORKSPACE_ID.slice(0, 8)}</span>
          </div>
          <nav className="navigation-list">
            <NavItem to={`/workspaces/${DEFAULT_WORKSPACE_ID}/tasks`} label="任务" icon={<ListTodo size={17} />} />
            <NavItem to={`/workspaces/${DEFAULT_WORKSPACE_ID}/activity`} label="活动" icon={<Activity size={17} />} />
            <NavItem to={`/workspaces/${DEFAULT_WORKSPACE_ID}/settings`} label="设置" icon={<Settings2 size={17} />} />
          </nav>
          <div className="rail-footer">
            <span className="eyebrow">连接状态</span>
            <span className="status-line"><span className="status-dot status-dot--live" />实时通道待接入</span>
          </div>
        </aside>

        <main className="main-surface">
          <div className="route-frame">
            <Outlet />
          </div>
        </main>
      </div>
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
  icon: React.ReactNode;
}) {
  return (
    <NavLink className={({ isActive }) => `navigation-item${isActive ? " is-active" : ""}`} to={to}>
      {icon}
      <span>{label}</span>
    </NavLink>
  );
}

export function TaskRoutePlaceholder() {
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
