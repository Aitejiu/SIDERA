import {
  apiCommand,
  apiGetAt,
  createCommandIdentity,
} from "@/shared/api/client";
import type { paths } from "@/shared/api/schema";

import type {
  CommandRequest,
  CommandResponse,
  WorkerActivityPage,
  WorkerWorkspace,
} from "./types";

export function workerWorkspaceQueryKey(workspaceId: string, workerItemId: string) {
  return ["worker-item-workspace", workspaceId, workerItemId] as const;
}

export function workerActivityQueryKey(workspaceId: string, workerItemId: string) {
  return ["worker-item-activity", workspaceId, workerItemId] as const;
}

export async function getWorkerItemWorkspace(
  workspaceId: string,
  workerItemId: string,
  signal?: AbortSignal,
): Promise<WorkerWorkspace> {
  const path = `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/worker-items/${encodeURIComponent(workerItemId)}/workspace`;
  const init = signal === undefined ? undefined : { signal };
  return apiGetAt(
    path,
    "/api/v1/workspaces/{workspace_id}/worker-items/{worker_item_id}/workspace" as keyof paths,
    init,
  );
}

export async function listWorkerItemActivity(
  workspaceId: string,
  workerItemId: string,
  options: { after?: string; limit?: number; signal?: AbortSignal } = {},
): Promise<WorkerActivityPage> {
  const params = new URLSearchParams({
    limit: String(options.limit ?? 50),
  });
  if (options.after !== undefined) {
    params.set("after", options.after);
  }
  const path = `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/worker-items/${encodeURIComponent(workerItemId)}/activity?${params.toString()}`;
  const init = options.signal === undefined ? undefined : { signal: options.signal };
  return apiGetAt(
    path,
    "/api/v1/workspaces/{workspace_id}/worker-items/{worker_item_id}/activity" as keyof paths,
    init,
  );
}

export async function pinExecutionWorker(
  workspaceId: string,
  executionId: string,
  expectedVersion: number,
  workerId: string,
): Promise<CommandResponse> {
  return executeRoutingCommand(workspaceId, {
    commandType: "PinExecutionWorker",
    executionId,
    expectedVersion,
    payload: { worker_id: workerId },
  });
}

export async function clearExecutionWorkerPin(
  workspaceId: string,
  executionId: string,
  expectedVersion: number,
): Promise<CommandResponse> {
  return executeRoutingCommand(workspaceId, {
    commandType: "ClearExecutionWorkerPin",
    executionId,
    expectedVersion,
    payload: {},
  });
}

async function executeRoutingCommand(
  workspaceId: string,
  input: {
    commandType: string;
    executionId: string;
    expectedVersion: number;
    payload: Record<string, unknown>;
  },
): Promise<CommandResponse> {
  const identity = createCommandIdentity();
  const command: CommandRequest = {
    command_id: identity.commandId,
    workspace_id: workspaceId,
    command_type: input.commandType,
    target_type: "execution",
    actor: { type: "local", id: "aitejiu" },
    target_id: input.executionId,
    expected_version: input.expectedVersion,
    idempotency_key: identity.idempotencyKey,
    correlation_id: crypto.randomUUID(),
    payload: input.payload,
  };
  return apiCommand<CommandResponse>("/api/v1/commands", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(command),
  });
}
