import { apiGetAt, apiRequestJson } from "@/shared/api/client";
import type { paths } from "@/shared/api/schema";

import type {
  ContextPackage,
  ContextPackageMaterialization,
  KnowledgeSubmissionPage,
  WorkContextDetail,
  WorkContextRevision,
} from "./types";

export function contextPackageQueryKey(workspaceId: string, packageId: string) {
  return ["context-package", workspaceId, packageId] as const;
}

export function contextPackageByExecutionQueryKey(workspaceId: string, executionId: string) {
  return ["context-package", workspaceId, "execution", executionId] as const;
}

export function contextPackageByPlanningRunQueryKey(workspaceId: string, runId: string) {
  return ["context-package", workspaceId, "planning-run", runId] as const;
}

export function workContextQueryKey(workspaceId: string, contextId: string) {
  return ["work-context", workspaceId, contextId] as const;
}

export function workContextByRootTaskQueryKey(workspaceId: string, rootTaskId: string) {
  return ["work-context", workspaceId, "root-task", rootTaskId] as const;
}

export function workContextByPlanningRequestQueryKey(workspaceId: string, planningRequestId: string) {
  return ["work-context", workspaceId, "planning-request", planningRequestId] as const;
}

export function taskKnowledgeSubmissionsQueryKey(workspaceId: string, taskId: string) {
  return ["context-knowledge-submissions", workspaceId, "task", taskId] as const;
}

export async function getContextPackage(
  workspaceId: string,
  packageId: string,
  signal?: AbortSignal,
): Promise<ContextPackage> {
  const path = `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/context-packages/${encodeURIComponent(packageId)}`;
  return apiGetAt(
    path,
    "/api/v1/workspaces/{workspace_id}/context-packages/{package_id}" as keyof paths,
    signal === undefined ? undefined : { signal },
  );
}

export async function getContextPackageByExecution(
  workspaceId: string,
  executionId: string,
  signal?: AbortSignal,
): Promise<ContextPackage> {
  const path = `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/executions/${encodeURIComponent(executionId)}/context-package`;
  return apiGetAt(
    path,
    "/api/v1/workspaces/{workspace_id}/executions/{execution_id}/context-package" as keyof paths,
    signal === undefined ? undefined : { signal },
  );
}

export async function getContextPackageByPlanningRun(
  workspaceId: string,
  runId: string,
  signal?: AbortSignal,
): Promise<ContextPackage> {
  const path = `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/planning-runs/${encodeURIComponent(runId)}/context-package`;
  return apiGetAt(
    path,
    "/api/v1/workspaces/{workspace_id}/planning-runs/{run_id}/context-package" as keyof paths,
    signal === undefined ? undefined : { signal },
  );
}

export async function materializeContextPackage(
  workspaceId: string,
  packageId: string,
  signal?: AbortSignal,
): Promise<ContextPackageMaterialization> {
  const path = `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/context-packages/${encodeURIComponent(packageId)}/materialize`;
  const init: RequestInit = { method: "POST", cache: "no-store" };
  if (signal !== undefined) init.signal = signal;
  return apiRequestJson<ContextPackageMaterialization>(path, init);
}

export async function getWorkContextByRootTask(
  workspaceId: string,
  rootTaskId: string,
  signal?: AbortSignal,
): Promise<WorkContextDetail> {
  const path = `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/tasks/${encodeURIComponent(rootTaskId)}/work-context`;
  return apiGetAt(
    path,
    "/api/v1/workspaces/{workspace_id}/tasks/{root_task_id}/work-context" as keyof paths,
    signal === undefined ? undefined : { signal },
  ) as Promise<WorkContextDetail>;
}

export async function getWorkContextByPlanningRequest(
  workspaceId: string,
  planningRequestId: string,
  signal?: AbortSignal,
): Promise<WorkContextDetail> {
  const path = `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/planning-requests/${encodeURIComponent(planningRequestId)}/work-context`;
  return apiGetAt(
    path,
    "/api/v1/workspaces/{workspace_id}/planning-requests/{planning_request_id}/work-context" as keyof paths,
    signal === undefined ? undefined : { signal },
  ) as Promise<WorkContextDetail>;
}

export async function listTaskKnowledgeSubmissions(
  workspaceId: string,
  taskId: string,
  signal?: AbortSignal,
): Promise<KnowledgeSubmissionPage> {
  const path = `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/tasks/${encodeURIComponent(taskId)}/knowledge-submissions`;
  return apiGetAt(
    path,
    "/api/v1/workspaces/{workspace_id}/tasks/{task_id}/knowledge-submissions" as keyof paths,
    signal === undefined ? undefined : { signal },
  ) as Promise<KnowledgeSubmissionPage>;
}

export async function getWorkContext(
  workspaceId: string,
  contextId: string,
  signal?: AbortSignal,
): Promise<WorkContextDetail> {
  const path = `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/work-contexts/${encodeURIComponent(contextId)}`;
  return apiGetAt(
    path,
    "/api/v1/workspaces/{workspace_id}/work-contexts/{work_context_id}" as keyof paths,
    signal === undefined ? undefined : { signal },
  ) as Promise<WorkContextDetail>;
}

export async function getWorkContextRevision(
  workspaceId: string,
  contextId: string,
  revisionId: string,
  signal?: AbortSignal,
): Promise<WorkContextRevision> {
  const path = `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/work-contexts/${encodeURIComponent(contextId)}/revisions/${encodeURIComponent(revisionId)}`;
  return apiGetAt(
    path,
    "/api/v1/workspaces/{workspace_id}/work-contexts/{work_context_id}/revisions/{revision_id}" as keyof paths,
    signal === undefined ? undefined : { signal },
  ) as Promise<WorkContextRevision>;
}
