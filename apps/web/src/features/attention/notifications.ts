import type { ProjectionChange } from "@/shared/api/projectionStream";

export interface WorkspaceNotification {
  id: string;
  kind: "completed" | "waiting_for_user" | "blocked" | "milestone_completed" | "root_completed";
  title: string;
  summary: string;
  resourceId: string | null;
  resourceVersion: number | null;
  cursor: number;
  href: string;
}

const LAST_CURSOR_PREFIX = "sidera:last-notified-cursor:";
const ENABLED_PREFIX = "sidera:browser-notifications:";

export function notificationFromChange(
  workspaceId: string,
  change: ProjectionChange,
): WorkspaceNotification | null {
  const kind = notificationKind(change.change_type);
  if (kind === null) {
    return null;
  }
  const title = {
    completed: "任务节点已完成",
    waiting_for_user: "任务等待用户确认",
    blocked: "任务节点持续阻塞",
    milestone_completed: "里程碑已完成",
    root_completed: "Root Task 已完成",
  }[kind];
  const resource = change.resource_id ? ` · ${change.resource_id.slice(0, 8)}` : "";
  return {
    id: notificationDedupeKey(workspaceId, change),
    kind,
    title,
    summary: `${title}${resource}`,
    resourceId: change.resource_id,
    resourceVersion: change.resource_version,
    cursor: change.cursor,
    href: change.resource_id
      ? `/workspaces/${workspaceId}/tasks/${change.resource_id}`
      : `/workspaces/${workspaceId}/tasks`,
  };
}

export function notificationKind(
  changeType: string,
): WorkspaceNotification["kind"] | null {
  const normalized = changeType.toLowerCase();
  if (normalized.includes("root") && normalized.includes("complete")) {
    return "root_completed";
  }
  if (normalized.includes("milestone") && normalized.includes("complete")) {
    return "milestone_completed";
  }
  if (
    normalized.includes("waiting") &&
    (normalized.includes("user") || normalized.includes("decision"))
  ) {
    return "waiting_for_user";
  }
  if (normalized.includes("blocked")) {
    return "blocked";
  }
  if (normalized.includes("complete") || normalized.includes("succeed")) {
    return "completed";
  }
  return null;
}

export function notificationDedupeKey(
  workspaceId: string,
  change: Pick<ProjectionChange, "cursor" | "resource_id" | "resource_version">,
): string {
  return [
    workspaceId,
    change.cursor,
    change.resource_id ?? "workspace",
    change.resource_version ?? "unknown",
  ].join(":");
}

export function appendNotification(
  current: readonly WorkspaceNotification[],
  next: WorkspaceNotification,
): WorkspaceNotification[] {
  const burst = current.find(
    (item) => item.kind === next.kind && item.resourceId === next.resourceId,
  );
  if (burst) {
    return [
      {
        ...next,
        id: burst.id,
        title: `${next.title}（批量 ${Number(burst.title.match(/批量 (\d+)/)?.[1] ?? 1) + 1}）`,
        summary: `${burst.summary}，最近一次 Cursor ${next.cursor}`,
      },
      ...current.filter((item) => item.id !== burst.id),
    ];
  }
  return [next, ...current].slice(0, 6);
}

export function lastNotifiedCursorKey(workspaceId: string): string {
  return `${LAST_CURSOR_PREFIX}${workspaceId}`;
}

export function readLastNotifiedCursor(
  workspaceId: string,
  storage: Storage | undefined = safeStorage(),
): number {
  const raw = storage?.getItem(lastNotifiedCursorKey(workspaceId));
  const cursor = raw === null || raw === undefined ? 0 : Number(raw);
  return Number.isFinite(cursor) ? cursor : 0;
}

export function writeLastNotifiedCursor(
  workspaceId: string,
  cursor: number,
  storage: Storage | undefined = safeStorage(),
): void {
  storage?.setItem(lastNotifiedCursorKey(workspaceId), String(cursor));
}

export function browserNotificationsEnabled(
  workspaceId: string,
  storage: Storage | undefined = safeStorage(),
): boolean {
  return storage?.getItem(`${ENABLED_PREFIX}${workspaceId}`) === "true";
}

export function setBrowserNotificationsEnabled(
  workspaceId: string,
  enabled: boolean,
  storage: Storage | undefined = safeStorage(),
): void {
  storage?.setItem(`${ENABLED_PREFIX}${workspaceId}`, String(enabled));
}

export async function requestBrowserNotificationPermission(): Promise<NotificationPermission | "unsupported"> {
  if (typeof Notification === "undefined") {
    return "unsupported";
  }
  return Notification.requestPermission();
}

export function sendBrowserNotification(notification: WorkspaceNotification): void {
  if (typeof Notification === "undefined" || Notification.permission !== "granted") {
    return;
  }
  const browserNotice = new Notification(notification.title, {
    body: notification.summary,
  });
  browserNotice.onclick = () => {
    window.location.assign(notification.href);
  };
}

function safeStorage(): Storage | undefined {
  try {
    return typeof window === "undefined" ? undefined : window.localStorage;
  } catch {
    return undefined;
  }
}
