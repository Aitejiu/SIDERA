import { apiGetAt } from "@/shared/api/client";
import type { components, paths } from "@/shared/api/schema";

import type { CollaborationDecisionPage, CollaborationDetail } from "./types";

const COLLABORATION_DETAIL_PATH =
  "/api/v1/workspaces/{workspace_id}/tasks/{root_task_id}/collaboration" as const;
const COLLABORATION_DECISIONS_PATH =
  "/api/v1/workspaces/{workspace_id}/tasks/{root_task_id}/collaboration/decisions" as const;

export type CollaborationDecisionQuery = components["schemas"]["CollaborationDecisionPageResponse"];

export function collaborationDetailQueryKey(workspaceId: string, rootTaskId: string) {
  return ["collaboration-detail", workspaceId, rootTaskId] as const;
}

export function collaborationDecisionsQueryKey(workspaceId: string, rootTaskId: string) {
  return ["collaboration-decisions", workspaceId, rootTaskId] as const;
}

export async function getCollaborationDetail(
  workspaceId: string,
  rootTaskId: string,
  signal?: AbortSignal,
): Promise<CollaborationDetail> {
  const path = `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/tasks/${encodeURIComponent(rootTaskId)}/collaboration`;
  const init = signal === undefined ? undefined : { signal };
  return apiGetAt(path, COLLABORATION_DETAIL_PATH as keyof paths, init);
}

export async function getCollaborationDecisions(
  workspaceId: string,
  rootTaskId: string,
  options: { cursor?: string; limit?: number; signal?: AbortSignal } = {},
): Promise<CollaborationDecisionPage> {
  const params = new URLSearchParams({
    limit: String(options.limit ?? 20),
  });
  if (options.cursor !== undefined) {
    params.set("cursor", options.cursor);
  }
  const path = `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/tasks/${encodeURIComponent(rootTaskId)}/collaboration/decisions?${params.toString()}`;
  const init = options.signal === undefined ? undefined : { signal: options.signal };
  return apiGetAt(path, COLLABORATION_DECISIONS_PATH as keyof paths, init);
}
