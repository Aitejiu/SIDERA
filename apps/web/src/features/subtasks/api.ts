import { apiGetAt } from "@/shared/api/client";
import type { paths } from "@/shared/api/schema";

import type { SubtaskWorkspace } from "./types";

export function subtaskWorkspaceQueryKey(
  workspaceId: string,
  rootTaskId: string,
  subtaskKey: string,
) {
  return ["subtask-workspace", workspaceId, rootTaskId, subtaskKey] as const;
}

export async function getSubtaskWorkspace(
  workspaceId: string,
  rootTaskId: string,
  subtaskKey: string,
  signal?: AbortSignal,
): Promise<SubtaskWorkspace> {
  const path = `/api/v1/tasks/${encodeURIComponent(rootTaskId)}/subtasks/${encodeURIComponent(subtaskKey)}?workspace_id=${encodeURIComponent(workspaceId)}`;
  const init = signal === undefined ? undefined : { signal };
  return apiGetAt(
    path,
    "/api/v1/tasks/{root_task_id}/subtasks/{subtask_key}" as keyof paths,
    init,
  );
}
