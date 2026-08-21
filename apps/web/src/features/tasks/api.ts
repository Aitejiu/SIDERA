import { apiGetAt } from "@/shared/api/client";
import type { components } from "@/shared/api/schema";

import type { paths } from "@/shared/api/schema";

import type { TaskActivity, TaskDetail, TaskGraph } from "./types";

export type TaskOverview = components["schemas"]["TaskOverviewProjectionResponse"];

const TASK_OVERVIEWS_PATH = "/api/v1/workspaces/{workspace_id}/task-overviews" as const;

export function taskOverviewQueryKey(workspaceId: string) {
  return ["task-overviews", workspaceId] as const;
}

export function taskOverviewDetailQueryKey(workspaceId: string, rootTaskId: string) {
  return ["task-overview", workspaceId, rootTaskId] as const;
}

export function taskGraphQueryKey(workspaceId: string, rootTaskId: string) {
  return ["task-graph", workspaceId, rootTaskId] as const;
}

export function taskDetailQueryKey(
  workspaceId: string,
  rootTaskId: string,
  workItemKey: string,
) {
  return ["task-detail", workspaceId, rootTaskId, workItemKey] as const;
}

export function taskActivityQueryKey(workspaceId: string, rootTaskId: string) {
  return ["task-activity", workspaceId, rootTaskId] as const;
}

export async function listTaskOverviews(
  workspaceId: string,
  signal?: AbortSignal,
): Promise<TaskOverview[]> {
  const path = `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/task-overviews`;
  const init = signal === undefined ? undefined : { signal };
  return apiGetAt(path, TASK_OVERVIEWS_PATH as keyof paths, init);
}

export async function getTaskGraph(
  workspaceId: string,
  rootTaskId: string,
  signal?: AbortSignal,
): Promise<TaskGraph> {
  const path = `/api/v1/tasks/${encodeURIComponent(rootTaskId)}/graph?workspace_id=${encodeURIComponent(workspaceId)}`;
  const init = signal === undefined ? undefined : { signal };
  return apiGetAt(
    path,
    "/api/v1/tasks/{root_task_id}/graph" as keyof paths,
    init,
  );
}

export async function getTaskDetail(
  workspaceId: string,
  rootTaskId: string,
  workItemKey: string,
  signal?: AbortSignal,
): Promise<TaskDetail> {
  const path = `/api/v1/tasks/${encodeURIComponent(rootTaskId)}/work-items/${encodeURIComponent(workItemKey)}?workspace_id=${encodeURIComponent(workspaceId)}`;
  const init = signal === undefined ? undefined : { signal };
  return apiGetAt(
    path,
    "/api/v1/tasks/{root_task_id}/work-items/{work_item_key}" as keyof paths,
    init,
  );
}

export async function getTaskActivity(
  workspaceId: string,
  rootTaskId: string,
  options: { after?: string; limit?: number; signal?: AbortSignal } = {},
): Promise<TaskActivity> {
  const params = new URLSearchParams({
    workspace_id: workspaceId,
    limit: String(options.limit ?? 50),
  });
  if (options.after !== undefined) {
    params.set("after", options.after);
  }
  const path = `/api/v1/tasks/${encodeURIComponent(rootTaskId)}/activity?${params.toString()}`;
  const init = options.signal === undefined ? undefined : { signal: options.signal };
  return apiGetAt(
    path,
    "/api/v1/tasks/{root_task_id}/activity" as keyof paths,
    init,
  );
}
