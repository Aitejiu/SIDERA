import { apiGetAt } from "@/shared/api/client";
import type { components } from "@/shared/api/schema";

import type { paths } from "@/shared/api/schema";

import type {
  PlanDiff,
  PlanSnapshot,
  TaskActivity,
  TaskDetail,
  TaskGraph,
  TaskView,
} from "./types";

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

export function taskQueryKey(workspaceId: string, taskId: string) {
  return ["task", workspaceId, taskId] as const;
}

export function planGenerationsQueryKey(workspaceId: string, rootTaskId: string) {
  return ["plan-generations", workspaceId, rootTaskId] as const;
}

export function planGenerationQueryKey(
  workspaceId: string,
  rootTaskId: string,
  generation: number,
) {
  return ["plan-generation", workspaceId, rootTaskId, generation] as const;
}

export function planDiffQueryKey(
  workspaceId: string,
  rootTaskId: string,
  left: number,
  right: number,
) {
  return ["plan-diff", workspaceId, rootTaskId, left, right] as const;
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

export async function getTask(
  workspaceId: string,
  taskId: string,
  signal?: AbortSignal,
): Promise<TaskView> {
  const path = `/api/v1/tasks/${encodeURIComponent(taskId)}?workspace_id=${encodeURIComponent(workspaceId)}`;
  const init = signal === undefined ? undefined : { signal };
  return apiGetAt(path, "/api/v1/tasks/{task_id}" as keyof paths, init);
}

export async function listPlanGenerations(
  workspaceId: string,
  rootTaskId: string,
  signal?: AbortSignal,
): Promise<PlanSnapshot[]> {
  const path = `/api/v1/tasks/${encodeURIComponent(rootTaskId)}/plan/generations?workspace_id=${encodeURIComponent(workspaceId)}`;
  const init = signal === undefined ? undefined : { signal };
  return apiGetAt(
    path,
    "/api/v1/tasks/{root_task_id}/plan/generations" as keyof paths,
    init,
  );
}

export async function getPlanGeneration(
  workspaceId: string,
  rootTaskId: string,
  generation: number,
  signal?: AbortSignal,
): Promise<PlanSnapshot> {
  const path = `/api/v1/tasks/${encodeURIComponent(rootTaskId)}/plan/generations/${generation}?workspace_id=${encodeURIComponent(workspaceId)}`;
  const init = signal === undefined ? undefined : { signal };
  return apiGetAt(
    path,
    "/api/v1/tasks/{root_task_id}/plan/generations/{generation}" as keyof paths,
    init,
  );
}

export async function getPlanDiff(
  workspaceId: string,
  rootTaskId: string,
  left: number,
  right: number,
  signal?: AbortSignal,
): Promise<PlanDiff> {
  const params = new URLSearchParams({
    workspace_id: workspaceId,
    left: String(left),
    right: String(right),
  });
  const path = `/api/v1/tasks/${encodeURIComponent(rootTaskId)}/plan-diff?${params.toString()}`;
  const init = signal === undefined ? undefined : { signal };
  return apiGetAt(
    path,
    "/api/v1/tasks/{root_task_id}/plan-diff" as keyof paths,
    init,
  );
}
