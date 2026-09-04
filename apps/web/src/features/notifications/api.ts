import {
  apiCommand,
  apiGetAt,
  createCommandIdentity,
} from "@/shared/api/client";
import type { components, paths } from "@/shared/api/schema";

export type NotificationSummary = components["schemas"]["NotificationSummaryResponse"];
export type NotificationThread = components["schemas"]["NotificationThreadResponse"];
export type NotificationThreadDetail = components["schemas"]["NotificationThreadDetailResponse"];
export type NotificationThreadPage = components["schemas"]["NotificationThreadPageResponse"];
export type NotificationAttemptPage = components["schemas"]["NotificationAttemptPageResponse"];
export type NotificationReadiness = components["schemas"]["NotificationReadinessResponse"];
export type NotificationModule = components["schemas"]["ModuleSummaryResponse"];
export type NotificationCommand = components["schemas"]["CommandRequest"];
export type NotificationCommandResponse = components["schemas"]["CommandResponse"];

type MarkReadPayload = components["schemas"]["MarkNotificationThreadReadPayload"];
type ArchivePayload = components["schemas"]["ArchiveNotificationThreadPayload"];
type RequeuePayload = components["schemas"]["RequeueNotificationDeliveryPayload"];

const SUMMARY_PATH = "/api/v1/workspaces/{workspace_id}/notification-summary" as const;
const THREADS_PATH = "/api/v1/workspaces/{workspace_id}/notification-threads" as const;
const THREAD_PATH = "/api/v1/workspaces/{workspace_id}/notification-threads/{thread_id}" as const;
const ATTEMPTS_PATH = "/api/v1/workspaces/{workspace_id}/notification-deliveries/{delivery_id}/attempts" as const;
const READINESS_PATH = "/api/v1/workspaces/{workspace_id}/notification-readiness" as const;
const MODULES_PATH = "/api/v1/modules" as const;
const COMMANDS_PATH = "/api/v1/commands" as const;

export type NotificationThreadCategory = "action" | "progress" | "archived";

export type NotificationThreadListOptions = {
  category?: NotificationThreadCategory;
  rootTaskId?: string;
  subtaskId?: string;
  unreadOnly?: boolean;
  cursor?: string;
  limit?: number;
  signal?: AbortSignal;
};

export type NotificationPageOptions = {
  cursor?: string;
  limit?: number;
  signal?: AbortSignal;
};

export function notificationSummaryQueryKey(workspaceId: string) {
  return ["notifications", workspaceId, "summary"] as const;
}

export function notificationThreadsQueryKey(
  workspaceId: string,
  options: Omit<NotificationThreadListOptions, "signal"> = {},
) {
  return [
    "notifications",
    workspaceId,
    "threads",
    options.category ?? "action",
    options.rootTaskId ?? null,
    options.subtaskId ?? null,
    options.unreadOnly ?? false,
  ] as const;
}

export function notificationThreadQueryKey(workspaceId: string, threadId: string) {
  return ["notifications", workspaceId, "thread", threadId] as const;
}

export function notificationAttemptsQueryKey(workspaceId: string, deliveryId: string) {
  return ["notifications", workspaceId, "delivery", deliveryId, "attempts"] as const;
}

export function notificationReadinessQueryKey(workspaceId: string) {
  return ["notifications", workspaceId, "readiness"] as const;
}

export function notificationModulesQueryKey(workspaceId: string) {
  return ["notifications", workspaceId, "modules"] as const;
}

export async function getNotificationSummary(
  workspaceId: string,
  signal?: AbortSignal,
): Promise<NotificationSummary> {
  return apiGetAt(
    workspacePath(workspaceId, "notification-summary"),
    SUMMARY_PATH as keyof paths,
    requestInit(signal),
  );
}

export async function listNotificationThreads(
  workspaceId: string,
  options: NotificationThreadListOptions = {},
): Promise<NotificationThreadPage> {
  const query = new URLSearchParams();
  if (options.category !== undefined) {
    query.set("category", options.category);
  }
  if (options.rootTaskId !== undefined) {
    query.set("root_task_id", options.rootTaskId);
  }
  if (options.subtaskId !== undefined) {
    query.set("subtask_id", options.subtaskId);
  }
  if (options.unreadOnly !== undefined) {
    query.set("unread_only", String(options.unreadOnly));
  }
  appendPageQuery(query, options);
  return apiGetAt(
    appendQuery(workspacePath(workspaceId, "notification-threads"), query),
    THREADS_PATH as keyof paths,
    requestInit(options.signal),
  );
}

export async function getNotificationThread(
  workspaceId: string,
  threadId: string,
  options: NotificationPageOptions = {},
): Promise<NotificationThreadDetail> {
  const query = new URLSearchParams();
  appendPageQuery(query, options);
  return apiGetAt(
    appendQuery(
      `${workspacePath(workspaceId, "notification-threads")}/${encodeURIComponent(threadId)}`,
      query,
    ),
    THREAD_PATH as keyof paths,
    requestInit(options.signal),
  );
}

export async function listNotificationDeliveryAttempts(
  workspaceId: string,
  deliveryId: string,
  options: NotificationPageOptions = {},
): Promise<NotificationAttemptPage> {
  const query = new URLSearchParams();
  appendPageQuery(query, options);
  return apiGetAt(
    appendQuery(
      `${workspacePath(workspaceId, "notification-deliveries")}/${encodeURIComponent(deliveryId)}/attempts`,
      query,
    ),
    ATTEMPTS_PATH as keyof paths,
    requestInit(options.signal),
  );
}

export async function getNotificationReadiness(
  workspaceId: string,
  signal?: AbortSignal,
): Promise<NotificationReadiness> {
  return apiGetAt(
    workspacePath(workspaceId, "notification-readiness"),
    READINESS_PATH as keyof paths,
    requestInit(signal),
  );
}

export async function listNotificationModules(
  workspaceId: string,
  signal?: AbortSignal,
): Promise<NotificationModule[]> {
  const query = new URLSearchParams({
    workspace_id: workspaceId,
    module_type: "notification_channel",
  });
  return apiGetAt(
    appendQuery("/api/v1/modules", query),
    MODULES_PATH as keyof paths,
    requestInit(signal),
  );
}

export function buildMarkNotificationThreadReadCommand({
  workspaceId,
  threadId,
  expectedVersion,
  expectedLatestFactId,
  actorId = "aitejiu",
}: {
  workspaceId: string;
  threadId: string;
  expectedVersion: number | null;
  expectedLatestFactId: string;
  actorId?: string;
}): NotificationCommand {
  return buildNotificationCommand({
    workspaceId,
    commandType: "MarkNotificationThreadRead",
    targetType: "notification_thread",
    targetId: threadId,
    expectedVersion,
    actorId,
    payload: { expected_latest_fact_id: expectedLatestFactId } satisfies MarkReadPayload,
  });
}

export function buildArchiveNotificationThreadCommand({
  workspaceId,
  threadId,
  expectedVersion,
  actorId = "aitejiu",
}: {
  workspaceId: string;
  threadId: string;
  expectedVersion: number | null;
  actorId?: string;
}): NotificationCommand {
  return buildNotificationCommand({
    workspaceId,
    commandType: "ArchiveNotificationThread",
    targetType: "notification_thread",
    targetId: threadId,
    expectedVersion,
    actorId,
    payload: {} satisfies ArchivePayload,
  });
}

export function buildRequeueNotificationDeliveryCommand({
  workspaceId,
  deliveryId,
  expectedRouteVersion,
  reason,
  actorId = "aitejiu",
}: {
  workspaceId: string;
  deliveryId: string;
  expectedRouteVersion: number;
  reason: string;
  actorId?: string;
}): NotificationCommand {
  return buildNotificationCommand({
    workspaceId,
    commandType: "RequeueNotificationDelivery",
    targetType: "notification_delivery",
    targetId: deliveryId,
    expectedVersion: null,
    actorId,
    payload: {
      expected_route_version: expectedRouteVersion,
      expected_status: "exhausted",
      reason,
    } satisfies RequeuePayload,
  });
}

export async function executeNotificationCommand(
  command: NotificationCommand,
): Promise<NotificationCommandResponse> {
  return apiCommand<NotificationCommandResponse, "/api/v1/commands">(
    COMMANDS_PATH,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(command),
    },
  );
}

function buildNotificationCommand({
  workspaceId,
  commandType,
  targetType,
  targetId,
  expectedVersion,
  payload,
  actorId,
}: {
  workspaceId: string;
  commandType: string;
  targetType: NotificationCommand["target_type"];
  targetId: string;
  expectedVersion: number | null;
  payload: NotificationCommand["payload"];
  actorId: string;
}): NotificationCommand {
  const identity = createCommandIdentity();
  return {
    command_id: identity.commandId,
    workspace_id: workspaceId,
    command_type: commandType,
    actor: { type: "local", id: actorId },
    target_type: targetType,
    target_id: targetId,
    expected_version: expectedVersion,
    idempotency_key: identity.idempotencyKey,
    correlation_id: crypto.randomUUID(),
    payload,
  };
}

function workspacePath(workspaceId: string, resource: string): string {
  return `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/${resource}`;
}

function appendPageQuery(
  query: URLSearchParams,
  options: NotificationPageOptions,
): void {
  if (options.cursor !== undefined) {
    query.set("cursor", options.cursor);
  }
  if (options.limit !== undefined) {
    query.set("limit", String(options.limit));
  }
}

function appendQuery(path: string, query: URLSearchParams): string {
  const serialized = query.toString();
  return serialized.length === 0 ? path : `${path}?${serialized}`;
}

function requestInit(signal?: AbortSignal): RequestInit | undefined {
  return signal === undefined ? undefined : { signal };
}
