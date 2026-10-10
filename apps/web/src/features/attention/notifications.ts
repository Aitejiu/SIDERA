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
    | "planning_materialized"
    | "notification_fact";
  title: string;
  summary: string;
  resourceId: string | null;
  resourceVersion: number | null;
  cursor: number | null;
  href: string;
}

const LAST_CURSOR_PREFIX = "sidera:last-notified-cursor:";
const ENABLED_PREFIX = "sidera:browser-notifications:";
const BROWSER_NOTIFICATION_IDS_PREFIX = "sidera:browser-notification-ids:";
const MAX_BROWSER_NOTIFICATION_IDS = 128;

export interface NotificationFactForBrowser {
  id: string;
  priority: string;
  title: string;
  summary: string;
  deep_link: string;
  baseline_import: boolean;
  thread_id: string;
  source_projection_cursor?: number;
  source_resource_version?: number;
}

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
    notification_fact: "通知需要处理",
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

export function notificationFromFact(
  workspaceId: string,
  fact: NotificationFactForBrowser,
): WorkspaceNotification | null {
  if (
    fact.priority !== "action"
    || fact.baseline_import
    || fact.id.length === 0
    || fact.title.length === 0
    || fact.summary.length === 0
    || fact.deep_link.length === 0
  ) {
    return null;
  }
  return {
    id: `notification-fact:${workspaceId}:${fact.id}`,
    kind: "notification_fact",
    title: fact.title,
    summary: fact.summary,
    resourceId: fact.thread_id,
    resourceVersion: fact.source_resource_version ?? null,
    cursor: fact.source_projection_cursor ?? null,
    href: fact.deep_link,
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
        summary: `${next.summary}，最近一次 Cursor ${next.cursor}`,
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
  let raw: string | null;
  try {
    raw = storage?.getItem(lastNotifiedCursorKey(workspaceId)) ?? null;
  } catch {
    return 0;
  }
  const cursor = raw === null || raw === undefined ? 0 : Number(raw);
  return Number.isFinite(cursor) ? cursor : 0;
}

export function writeLastNotifiedCursor(
  workspaceId: string,
  cursor: number,
  storage: Storage | undefined = safeStorage(),
): void {
  try {
    storage?.setItem(lastNotifiedCursorKey(workspaceId), String(cursor));
  } catch {
    // Browser notification state must not affect the server-authoritative thread.
  }
}

export function browserNotificationIdsKey(workspaceId: string): string {
  return `${BROWSER_NOTIFICATION_IDS_PREFIX}${workspaceId}`;
}

export function readSentBrowserNotificationIds(
  workspaceId: string,
  storage: Storage | undefined = safeStorage(),
): string[] {
  let raw: string | null;
  try {
    raw = storage?.getItem(browserNotificationIdsKey(workspaceId)) ?? null;
  } catch {
    return [];
  }
  if (raw === null || raw === undefined) {
    return [];
  }
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) {
      return [];
    }
    return parsed
      .filter((value): value is string => typeof value === "string")
      .slice(-MAX_BROWSER_NOTIFICATION_IDS);
  } catch {
    return [];
  }
}

export function hasSentBrowserNotification(
  workspaceId: string,
  notificationId: string,
  storage: Storage | undefined = safeStorage(),
): boolean {
  return readSentBrowserNotificationIds(workspaceId, storage).includes(notificationId);
}

export function recordSentBrowserNotification(
  workspaceId: string,
  notificationId: string,
  storage: Storage | undefined = safeStorage(),
): void {
  if (storage === undefined) {
    return;
  }
  const ids = readSentBrowserNotificationIds(workspaceId, storage);
  if (ids.includes(notificationId)) {
    return;
  }
  try {
    storage.setItem(
      browserNotificationIdsKey(workspaceId),
      JSON.stringify([...ids, notificationId].slice(-MAX_BROWSER_NOTIFICATION_IDS)),
    );
  } catch {
    // Notification delivery remains best-effort when browser storage is unavailable.
  }
}

export function browserNotificationsEnabled(
  workspaceId: string,
  storage: Storage | undefined = safeStorage(),
): boolean {
  try {
    return storage?.getItem(`${ENABLED_PREFIX}${workspaceId}`) === "true";
  } catch {
    return false;
  }
}

export function setBrowserNotificationsEnabled(
  workspaceId: string,
  enabled: boolean,
  storage: Storage | undefined = safeStorage(),
): void {
  try {
    storage?.setItem(`${ENABLED_PREFIX}${workspaceId}`, String(enabled));
  } catch {
    // Opt-in preference is best-effort and never changes Thread state.
  }
}

export async function requestBrowserNotificationPermission(): Promise<NotificationPermission | "unsupported"> {
  if (typeof Notification === "undefined") {
    return "unsupported";
  }
  try {
    return await Notification.requestPermission();
  } catch {
    return "unsupported";
  }
}

export function sendBrowserNotification(
  notification: WorkspaceNotification,
  openDeepLink: (href: string) => void = (href) => window.location.assign(href),
): boolean {
  if (
    typeof window === "undefined"
    || typeof Notification === "undefined"
    || Notification.permission !== "granted"
  ) {
    return false;
  }
  try {
    const target = new URL(notification.href, window.location.origin);
    if (target.origin !== window.location.origin) {
      return false;
    }
    const browserNotice = new Notification(notification.title, {
      body: notification.summary,
    });
    browserNotice.onclick = () => {
      openDeepLink(`${target.pathname}${target.search}${target.hash}`);
    };
    return true;
  } catch {
    return false;
  }
}

function safeStorage(): Storage | undefined {
  try {
    return typeof window === "undefined" ? undefined : window.localStorage;
  } catch {
    return undefined;
  }
}
