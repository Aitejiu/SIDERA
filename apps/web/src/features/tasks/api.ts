import { apiGetAt } from "@/shared/api/client";
import type { components } from "@/shared/api/schema";

import type { paths } from "@/shared/api/schema";

export type TaskOverview = components["schemas"]["TaskOverviewProjectionResponse"];

const TASK_OVERVIEWS_PATH = "/api/v1/workspaces/{workspace_id}/task-overviews" as const;

export function taskOverviewQueryKey(workspaceId: string) {
  return ["task-overviews", workspaceId] as const;
}

export function taskOverviewDetailQueryKey(workspaceId: string, rootTaskId: string) {
  return ["task-overview", workspaceId, rootTaskId] as const;
}

export async function listTaskOverviews(
  workspaceId: string,
  signal?: AbortSignal,
): Promise<TaskOverview[]> {
  const path = `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/task-overviews`;
  const init = signal === undefined ? undefined : { signal };
  return apiGetAt(path, TASK_OVERVIEWS_PATH as keyof paths, init);
}
