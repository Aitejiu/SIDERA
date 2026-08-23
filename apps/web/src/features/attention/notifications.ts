import type { ProjectionChange } from "@/shared/api/projectionStream";
import type { PlanningRequestSummary } from "@/features/planning/types";

export interface WorkspaceNotification {
  id: string;
  kind:
    | "completed"
    | "waiting_for_user"
    | "blocked"
    | "milestone_completed"
    | "root_completed"
    | "planning_clarification"
    | "planning_proposal"
    | "planning_conflicted"
    | "planning_blocked"
    | "planning_materialized";
  title: string;
  summary: string;
  resourceId: string | null;
  resourceVersion: number | null;
  cursor: number | null;
  href: string;
}

const LAST_CURSOR_PREFIX = "sidera:last-notified-cursor:";
const ENABLED_PREFIX = "sidera:browser-notifications:";

export function notificationFromChange(
  workspaceId: string,
  change: ProjectionChange,
): WorkspaceNotification | null {
  if (change.projection_type === "planning_request") {
    return planningNotificationFromChange(workspaceId, change);
  }
  const kind = notificationKind(change.change_type);
  if (kind === null) {
    return null;
  }
  const titleMap: Record<WorkspaceNotification["kind"], string> = {
    completed: "任务节点已完成",
    waiting_for_user: "任务等待用户确认",
    blocked: "任务节点持续阻塞",
    milestone_completed: "里程碑已完成",
    root_completed: "Root Task 已完成",
    planning_clarification: "任务规划需要补充信息",
    planning_proposal: "任务方案待确认",
    planning_conflicted: "任务方案已冲突",
    planning_blocked: "任务规划已阻塞",
    planning_materialized: "任务已开始",
  };
  const title = titleMap[kind];
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

export function planningNotificationFromChange(
  workspaceId: string,
  change: ProjectionChange,
): WorkspaceNotification | null {
  const kind = planningNotificationKind(change.change_type);
  if (kind === null) {
    return null;
  }
  const title = planningNotificationTitle(kind);
  const requestId = change.resource_id;
  const resource = requestId ? ` · ${requestId.slice(0, 8)}` : "";
  return {
    id: `planning:${notificationDedupeKey(workspaceId, change)}`,
    kind,
    title,
    summary: `${title}${resource}`,
    resourceId: requestId,
    resourceVersion: change.resource_version,
    cursor: change.cursor,
    href: requestId
      ? `/workspaces/${workspaceId}/planning/${requestId}`
      : `/workspaces/${workspaceId}/inbox`,
  };
}

export function planningNotificationFromRequest(
  request: Pick<PlanningRequestSummary, "id" | "status" | "version" | "workspace_id">,
): WorkspaceNotification | null {
  const kind = planningNotificationKind(request.status);
  if (kind === null || kind === "planning_materialized") {
    return null;
  }
  const title = planningNotificationTitle(kind);
  return {
    id: `planning-fact:${request.workspace_id}:${request.id}:${request.status}:${request.version}`,
    kind,
    title,
    summary: `${title} · ${request.id.slice(0, 8)}`,
    resourceId: request.id,
    resourceVersion: request.version,
    cursor: null,
    href: `/workspaces/${request.workspace_id}/planning/${request.id}`,
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

function planningNotificationKind(
  value: string,
): Extract<WorkspaceNotification["kind"], `planning_${string}`> | null {
  const normalized = value.toLowerCase();
  if (
    normalized.includes("clarif")
    || (normalized.includes("waiting") && (normalized.includes("user") || normalized.includes("decision")))
  ) {
    return "planning_clarification";
  }
  if (normalized.includes("proposal") && (normalized.includes("ready") || normalized.includes("confirm"))) {
    return "planning_proposal";
  }
  if (normalized.includes("conflict")) {
    return "planning_conflicted";
  }
  if (normalized.includes("block")) {
    return "planning_blocked";
  }
  if (normalized.includes("material") || normalized.includes("start")) {
    return "planning_materialized";
  }
  return null;
}

function planningNotificationTitle(
  kind: Extract<WorkspaceNotification["kind"], `planning_${string}`>,
): string {
  return {
    planning_clarification: "任务规划需要补充信息",
    planning_proposal: "任务方案待确认",
    planning_conflicted: "任务方案已冲突",
    planning_blocked: "任务规划已阻塞",
    planning_materialized: "任务已开始",
  }[kind];
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
