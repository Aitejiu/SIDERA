import {
  getPlanningMaterialization,
  getPlanningRequest,
  listPlanningRequests,
} from "@/features/planning/api";
import type {
  PlanningMaterialization,
  PlanningRequest,
  PlanningRequestSummary,
} from "@/features/planning/types";
import type { ProjectionChange } from "@/shared/api/projectionStream";

export type NotificationOwner = "web" | "private" | "unknown";

type PlanningRequestOwner = Pick<PlanningRequest, "source_type">;
type PlanningRequestCandidate = Pick<
  PlanningRequestSummary,
  "id" | "status" | "source_type"
>;
type PlanningMaterializationRoot = Pick<PlanningMaterialization, "root_task_id">;

export interface NotificationOwnershipApi {
  getPlanningRequest(
    workspaceId: string,
    requestId: string,
  ): Promise<PlanningRequestOwner>;
  listPlanningRequests(workspaceId: string): Promise<{
    items: PlanningRequestCandidate[];
    next_cursor: string | null;
  }>;
  getPlanningMaterialization(
    workspaceId: string,
    requestId: string,
  ): Promise<PlanningMaterializationRoot>;
}

const MAX_MATERIALIZED_REQUESTS = 100;
const rootTaskOwners = new Map<string, NotificationOwner>();

const defaultApi: NotificationOwnershipApi = {
  getPlanningRequest: (workspaceId, requestId) =>
    getPlanningRequest(workspaceId, requestId),
  listPlanningRequests: (workspaceId) => listPlanningRequests(workspaceId),
  getPlanningMaterialization: (workspaceId, requestId) =>
    getPlanningMaterialization(workspaceId, requestId),
};

export function clearNotificationOwnershipCache(): void {
  rootTaskOwners.clear();
}

export async function resolveNotificationOwner(
  workspaceId: string,
  change: ProjectionChange,
  api: NotificationOwnershipApi = defaultApi,
): Promise<NotificationOwner> {
  if (change.workspace_id !== workspaceId) {
    return "unknown";
  }

  try {
    if (change.projection_type === "planning_request") {
      return await resolvePlanningRequestOwner(workspaceId, change.resource_id, api);
    }
    if (change.projection_type === "planning_materialization") {
      return await resolveMaterializationOwner(workspaceId, change.resource_id, api);
    }
    if (change.projection_type === "task_overview") {
      return await resolveRootTaskOwner(workspaceId, change.resource_id, api);
    }
  } catch {
    return "unknown";
  }
  return "unknown";
}

async function resolvePlanningRequestOwner(
  workspaceId: string,
  requestId: string | null,
  api: NotificationOwnershipApi,
): Promise<NotificationOwner> {
  if (requestId === null || requestId.length === 0) {
    return "unknown";
  }
  const request = await api.getPlanningRequest(workspaceId, requestId);
  return ownerFromSourceType(request.source_type);
}

async function resolveMaterializationOwner(
  workspaceId: string,
  requestId: string | null,
  api: NotificationOwnershipApi,
): Promise<NotificationOwner> {
  if (requestId === null || requestId.length === 0) {
    return "unknown";
  }
  const [request, materialization] = await Promise.all([
    api.getPlanningRequest(workspaceId, requestId),
    api.getPlanningMaterialization(workspaceId, requestId),
  ]);
  const owner = ownerFromSourceType(request.source_type);
  rootTaskOwners.set(rootTaskKey(workspaceId, materialization.root_task_id), owner);
  return owner;
}

async function resolveRootTaskOwner(
  workspaceId: string,
  rootTaskId: string | null,
  api: NotificationOwnershipApi,
): Promise<NotificationOwner> {
  if (rootTaskId === null || rootTaskId.length === 0) {
    return "unknown";
  }
  const cached = rootTaskOwners.get(rootTaskKey(workspaceId, rootTaskId));
  if (cached !== undefined) {
    return cached;
  }

  const page = await api.listPlanningRequests(workspaceId);
  const candidates = page.items
    .filter((request) => request.status.toLowerCase() === "materialized")
    .slice(0, MAX_MATERIALIZED_REQUESTS);
  for (const request of candidates) {
    try {
      const materialization = await api.getPlanningMaterialization(
        workspaceId,
        request.id,
      );
      const owner = ownerFromSourceType(request.source_type);
      rootTaskOwners.set(
        rootTaskKey(workspaceId, materialization.root_task_id),
        owner,
      );
      if (materialization.root_task_id === rootTaskId) {
        return owner;
      }
    } catch {
      // A stale materialization must not prevent checking the remaining bounded candidates.
    }
  }
  return "unknown";
}

function ownerFromSourceType(sourceType: string): NotificationOwner {
  if (sourceType === "web" || sourceType === "sidera_web_chat") {
    return "web";
  }
  if (sourceType === "akashic_private") {
    return "private";
  }
  return "unknown";
}

function rootTaskKey(workspaceId: string, rootTaskId: string): string {
  return `${workspaceId}:${rootTaskId}`;
}
