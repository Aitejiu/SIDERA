import {
  apiCommand,
  apiGetAt,
  createCommandIdentity,
} from "@/shared/api/client";
import type { paths } from "@/shared/api/schema";

import type {
  CommandRequest,
  CommandResponse,
  ConfigureWorkerRuntimePayload,
  ModuleSummary,
  Worker,
  WorkerModuleType,
} from "./types";

const WORKERS_PATH = "/api/v1/workers" as const;
const WORKER_PATH = "/api/v1/workers/{worker_id}" as const;
const MODULES_PATH = "/api/v1/modules" as const;

export function workersQueryKey(workspaceId: string) {
  return ["workers", workspaceId] as const;
}

export function workerQueryKey(workspaceId: string, workerId: string) {
  return ["worker", workspaceId, workerId] as const;
}

export function modulesQueryKey(workspaceId: string, moduleType: WorkerModuleType) {
  return ["modules", workspaceId, moduleType] as const;
}

export async function listWorkers(
  workspaceId: string,
  signal?: AbortSignal,
): Promise<Worker[]> {
  const query = new URLSearchParams({ workspace_id: workspaceId });
  const path = `${WORKERS_PATH}?${query.toString()}`;
  const init = signal === undefined ? undefined : { signal };
  return apiGetAt(path, WORKERS_PATH as keyof paths, init);
}

export async function getWorker(
  workspaceId: string,
  workerId: string,
  signal?: AbortSignal,
): Promise<Worker> {
  const query = new URLSearchParams({ workspace_id: workspaceId });
  const path = `${WORKERS_PATH}/${encodeURIComponent(workerId)}?${query.toString()}`;
  const init = signal === undefined ? undefined : { signal };
  return apiGetAt(path, WORKER_PATH as keyof paths, init);
}

export async function listModules(
  workspaceId: string,
  moduleType: WorkerModuleType,
  signal?: AbortSignal,
): Promise<ModuleSummary[]> {
  const query = new URLSearchParams({
    workspace_id: workspaceId,
    module_type: moduleType,
  });
  const path = `${MODULES_PATH}?${query.toString()}`;
  const init = signal === undefined ? undefined : { signal };
  return apiGetAt(path, MODULES_PATH as keyof paths, init);
}

export function buildConfigureWorkerRuntimeCommand({
  workspaceId,
  workerId,
  expectedVersion,
  payload,
  actorId = "aitejiu",
}: {
  workspaceId: string;
  workerId: string;
  expectedVersion: number | null;
  payload: ConfigureWorkerRuntimePayload;
  actorId?: string;
}): CommandRequest {
  const identity = createCommandIdentity();
  return {
    command_id: identity.commandId,
    workspace_id: workspaceId,
    command_type: "ConfigureWorkerRuntime",
    target_type: "worker",
    actor: { type: "local", id: actorId },
    target_id: workerId,
    expected_version: expectedVersion,
    idempotency_key: identity.idempotencyKey,
    correlation_id: crypto.randomUUID(),
    payload,
  };
}

export async function configureWorkerRuntime(
  command: CommandRequest,
): Promise<CommandResponse> {
  return apiCommand<CommandResponse, "/api/v1/commands">(
    "/api/v1/commands",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(command),
    },
  );
}
