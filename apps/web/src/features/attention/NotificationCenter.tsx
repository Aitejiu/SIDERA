import { Bell, BellOff, Check, X } from "lucide-react";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { useWorkspaceStreamOptional } from "@/app/WorkspaceStreamProvider";

import {
  browserNotificationsEnabled,
  requestBrowserNotificationPermission,
  setBrowserNotificationsEnabled,
} from "./notifications";

export function NotificationCenter({ mode = "tray" }: { mode?: "tray" | "settings" }) {
  const stream = useWorkspaceStreamOptional();
  const workspaceId = useWorkspaceIdFromPath();
  const [permission, setPermission] = useState<NotificationPermission | "unsupported">(() => getPermission());
  const [enabled, setEnabled] = useState(() => browserNotificationsEnabled(workspaceId));
  const notices = stream?.notices ?? [];

  const permissionLabel = useMemo(() => {
    if (permission === "unsupported") return "当前浏览器不支持";
    if (permission === "denied") return "浏览器权限已拒绝";
    if (permission === "granted" && enabled) return "浏览器通知已开启";
    return "浏览器通知未开启";
  }, [enabled, permission]);

  const toggleBrowserNotifications = async () => {
    if (enabled) {
      setBrowserNotificationsEnabled(workspaceId, false);
      setEnabled(false);
      return;
    }
    const nextPermission = await requestBrowserNotificationPermission();
    setPermission(nextPermission);
    if (nextPermission === "granted") {
      setBrowserNotificationsEnabled(workspaceId, true);
      setEnabled(true);
    }
  };

  if (mode === "settings") {
    return (
      <section className="notification-settings" aria-labelledby="notification-settings-title">
        <div className="breadcrumb">工作区 / 设置</div>
        <div className="page-heading">
          <div>
            <span className="eyebrow">NOTIFICATION POLICY</span>
            <h1 id="notification-settings-title">通知</h1>
            <p>默认使用站内通知；浏览器通知只在此处的明确点击后启用。</p>
          </div>
        </div>
        <div className="notification-setting-row">
          <div>
            <strong>浏览器通知</strong>
            <span>{permissionLabel}</span>
          </div>
          <button
            className="quiet-button"
            type="button"
            onClick={() => void toggleBrowserNotifications()}
            disabled={permission === "unsupported" || permission === "denied"}
          >
            {enabled ? <BellOff size={14} aria-hidden="true" /> : <Bell size={14} aria-hidden="true" />}
            {enabled ? "关闭" : "开启"}
          </button>
        </div>
      </section>
    );
  }

  if (notices.length === 0) {
    return null;
  }
  return (
    <aside className="notification-tray" aria-live="polite" aria-label="任务通知">
      <div className="notification-tray__heading"><span className="eyebrow">NOTICES</span><span className="mono-label">{notices.length}</span></div>
      {notices.map((notice) => (
        <div className="notification-item" key={notice.id}>
          <Link to={notice.href}>
            <strong>{notice.title}</strong>
            <span>{notice.summary}</span>
          </Link>
          <button
            className="notification-dismiss"
            type="button"
            onClick={() => stream?.dismissNotice(notice.id)}
            aria-label={`关闭${notice.title}`}
          >
            <X size={13} aria-hidden="true" />
          </button>
        </div>
      ))}
      <span className="notification-tray__policy"><Check size={12} aria-hidden="true" />站内通知已启用</span>
    </aside>
  );
}

function getPermission(): NotificationPermission | "unsupported" {
  return typeof Notification === "undefined" ? "unsupported" : Notification.permission;
}

function useWorkspaceIdFromPath(): string {
  if (typeof window === "undefined") return "workspace";
  const match = window.location.pathname.match(/^\/workspaces\/([^/]+)/);
  return match?.[1] ?? "workspace";
}
