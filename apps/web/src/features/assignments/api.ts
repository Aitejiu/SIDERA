import { apiGetAt } from "@/shared/api/client";
import {
  AssignmentUnavailableError,
  type AgentBindingView,
  type NativeWorkerContextView,
} from "./types";

type AssignmentKind = AgentBindingView["target_type"];

const ASSIGNMENT_PATH =
  "/api/v1/workspaces/{workspace_id}/assignments/{target_type}/{target_id}" as const;
const NATIVE_CONTEXT_PATH =
  "/api/v1/workspaces/{workspace_id}/worker-items/{worker_item_id}/native-context" as const;

export function assignmentTargetKey(kind: AssignmentKind, idOrKey: string): string {
  return JSON.stringify([kind, idOrKey]);
}

export function assignmentQueryKey(
  workspaceId: string,
  kind: AssignmentKind,
  targetId: string,
) {
  return ["agent-assignment", workspaceId, kind, targetId] as const;
}

export function nativeWorkerContextQueryKey(workspaceId: string, workerItemId: string) {
  return ["native-worker-context", workspaceId, workerItemId] as const;
}

export async function readAssignment(
  workspaceId: string,
  kind: AssignmentKind,
  targetId: string,
  signal?: AbortSignal,
): Promise<AgentBindingView | null> {
  const path = `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/assignments/${kind}/${encodeURIComponent(targetId)}`;
  try {
    const response: unknown = await apiGetAt(
      path,
      ASSIGNMENT_PATH,
      signal === undefined ? undefined : { signal },
    );
    if (response === null) return null;
    if (!isRecord(response) || response.workspace_id !== workspaceId
      || response.target_type !== kind || response.target_id !== targetId) {
      throw new AssignmentUnavailableError();
    }
    return response as unknown as AgentBindingView;
  } catch (error) {
    throwSafeReadError(error);
  }
}

export async function readNativeWorkerContext(
  workspaceId: string,
  workerItemId: string,
  signal?: AbortSignal,
): Promise<NativeWorkerContextView> {
  const path = `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/worker-items/${encodeURIComponent(workerItemId)}/native-context`;
  try {
    const response: unknown = await apiGetAt(
      path,
      NATIVE_CONTEXT_PATH,
      signal === undefined ? undefined : { signal },
    );
    if (!isRecord(response) || response.workspace_id !== workspaceId
      || response.worker_item_id !== workerItemId) {
      throw new AssignmentUnavailableError();
    }
    return response as unknown as NativeWorkerContextView;
  } catch (error) {
    throwSafeReadError(error);
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function throwSafeReadError(error: unknown): never {
  if (error instanceof AssignmentUnavailableError) throw error;
  if (error instanceof Error && error.name === "AbortError") throw error;
  throw new AssignmentUnavailableError();
}
