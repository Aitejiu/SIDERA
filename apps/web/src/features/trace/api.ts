import {
  apiCommand,
  apiGetAt,
  apiGetBlobAt,
  createCommandIdentity,
} from "@/shared/api/client";
import type { components, paths } from "@/shared/api/schema";

import type {
  ArtifactDescriptor,
  CommandResponse,
  Execution,
  ExecutionContextAddress,
  ExecutionInput,
  ExecutionSession,
  WorkerTrace,
} from "./types";

export function workerTraceQueryKey(
  workspaceId: string,
  executionId: string,
  sessionId: string,
  rootTaskId: string,
) {
  return ["worker-trace", workspaceId, executionId, sessionId, rootTaskId] as const;
}

export function executionSessionQueryKey(workspaceId: string, sessionId: string) {
  return ["execution-session", workspaceId, sessionId] as const;
}

export function executionQueryKey(workspaceId: string, executionId: string) {
  return ["execution", workspaceId, executionId] as const;
}

export function executionInputsQueryKey(workspaceId: string, sessionId: string) {
  return ["execution-inputs", workspaceId, sessionId] as const;
}

export function artifactDescriptorQueryKey(workspaceId: string, artifactId: string) {
  return ["artifact-descriptor", workspaceId, artifactId] as const;
}

export async function getWorkerTrace(
  workspaceId: string,
  rootTaskId: string,
  executionId: string,
  sessionId: string,
  options: { beforeSequence?: number; limit?: number; signal?: AbortSignal } = {},
): Promise<WorkerTrace> {
  const params = new URLSearchParams({
    workspace_id: workspaceId,
    limit: String(options.limit ?? 100),
  });
  if (options.beforeSequence !== undefined) {
    params.set("before_sequence", String(options.beforeSequence));
  }
  const path = `/api/v1/tasks/${encodeURIComponent(rootTaskId)}/executions/${encodeURIComponent(executionId)}/sessions/${encodeURIComponent(sessionId)}/trace?${params.toString()}`;
  const init = options.signal === undefined ? undefined : { signal: options.signal };
  return apiGetAt(
    path,
    "/api/v1/tasks/{root_task_id}/executions/{execution_id}/sessions/{session_id}/trace" as keyof paths,
    init,
  );
}

export async function getExecutionSession(
  workspaceId: string,
  sessionId: string,
  signal?: AbortSignal,
): Promise<ExecutionSession> {
  const params = new URLSearchParams({
    workspace_id: workspaceId,
    session_id: sessionId,
    limit: "1",
  });
  const path = `/api/v1/gateway/sessions?${params.toString()}`;
  const init = signal === undefined ? undefined : { signal };
  const page = await apiGetAt(
    path,
    "/api/v1/gateway/sessions" as keyof paths,
    init,
  ) as components["schemas"]["ExecutionSessionPageResponse"];
  const session = page.items[0];
  if (session === undefined) {
    throw new Error("Execution Session not found");
  }
  return session;
}

export async function getExecution(
  workspaceId: string,
  executionId: string,
  signal?: AbortSignal,
): Promise<Execution> {
  const path = `/api/v1/executions/${encodeURIComponent(executionId)}?workspace_id=${encodeURIComponent(workspaceId)}`;
  const init = signal === undefined ? undefined : { signal };
  return apiGetAt(path, "/api/v1/executions/{execution_id}" as keyof paths, init);
}

export async function listExecutionInputs(
  workspaceId: string,
  sessionId: string,
  signal?: AbortSignal,
): Promise<ExecutionInput[]> {
  const params = new URLSearchParams({
    workspace_id: workspaceId,
    session_id: sessionId,
    limit: "100",
  });
  const path = `/api/v1/gateway/inputs?${params.toString()}`;
  const init = signal === undefined ? undefined : { signal };
  const page = await apiGetAt(
    path,
    "/api/v1/gateway/inputs" as keyof paths,
    init,
  ) as components["schemas"]["ExecutionInputPageResponse"];
  return page.items;
}

export async function getArtifactDescriptor(
  workspaceId: string,
  artifactId: string,
  signal?: AbortSignal,
): Promise<ArtifactDescriptor> {
  const path = `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/artifacts/${encodeURIComponent(artifactId)}/descriptor`;
  const init = signal === undefined ? undefined : { signal };
  return apiGetAt(
    path,
    "/api/v1/workspaces/{workspace_id}/artifacts/{artifact_id}/descriptor" as keyof paths,
    init,
  );
}

export async function getArtifactContent(
  workspaceId: string,
  artifactId: string,
  signal?: AbortSignal,
): Promise<Blob> {
  const path = `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/artifacts/${encodeURIComponent(artifactId)}/content`;
  return apiGetBlobAt(
    path,
    "/api/v1/workspaces/{workspace_id}/artifacts/{artifact_id}/content" as keyof paths,
    signal === undefined ? undefined : { signal },
  );
}

export async function getWorkspaceCapabilities(
  workspaceId: string,
  signal?: AbortSignal,
): Promise<Record<string, unknown>> {
  const path = `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/capabilities`;
  const init = signal === undefined ? undefined : { signal };
  return apiGetAt(
    path,
    "/api/v1/workspaces/{workspace_id}/capabilities" as keyof paths,
    init,
  ) as Promise<Record<string, unknown>>;
}

export async function sendExecutionInput(
  workspaceId: string,
  session: ExecutionSession,
  context: ExecutionContextAddress,
  clientIdempotencyKey: string,
  text: string,
): Promise<CommandResponse> {
  return executeCommand(workspaceId, {
    command_type: "SendExecutionInput",
    target_type: "execution_session",
    target_id: session.id,
    expected_version: session.version,
    payload: {
      context,
      client_idempotency_key: clientIdempotencyKey,
      text,
      attachment_refs: [],
    },
  });
}

export async function updatePendingExecutionInput(
  workspaceId: string,
  session: ExecutionSession,
  executionId: string,
  clientIdempotencyKey: string,
  text: string,
): Promise<CommandResponse> {
  return executeCommand(workspaceId, {
    command_type: "UpdatePendingExecutionInput",
    target_type: "execution_session",
    target_id: session.id,
    expected_version: session.version,
    payload: { execution_id: executionId, client_idempotency_key: clientIdempotencyKey, text },
  });
}

export async function cancelExecutionInput(
  workspaceId: string,
  session: ExecutionSession,
  executionId: string,
  clientIdempotencyKey: string,
): Promise<CommandResponse> {
  return executeCommand(workspaceId, {
    command_type: "CancelExecutionInput",
    target_type: "execution_session",
    target_id: session.id,
    expected_version: session.version,
    payload: { execution_id: executionId, client_idempotency_key: clientIdempotencyKey },
  });
}

export async function retryBlockedExecutionInput(
  workspaceId: string,
  session: ExecutionSession,
  executionId: string,
  clientIdempotencyKey: string,
): Promise<CommandResponse> {
  return executeCommand(workspaceId, {
    command_type: "RetryBlockedExecutionInput",
    target_type: "execution_session",
    target_id: session.id,
    expected_version: session.version,
    payload: { execution_id: executionId, client_idempotency_key: clientIdempotencyKey },
  });
}

export async function stopExecution(
  workspaceId: string,
  execution: Execution,
): Promise<CommandResponse> {
  return executeCommand(workspaceId, {
    command_type: "StopExecution",
    target_type: "execution",
    target_id: execution.id,
    expected_version: execution.version,
    payload: {},
  });
}

async function executeCommand(
  workspaceId: string,
  input: {
    command_type: string;
    target_type: components["schemas"]["CommandRequest"]["target_type"];
    target_id: string;
    expected_version: number;
    payload: Record<string, unknown>;
  },
): Promise<CommandResponse> {
  const identity = createCommandIdentity();
  const request: components["schemas"]["CommandRequest"] = {
    command_id: identity.commandId,
    workspace_id: workspaceId,
    command_type: input.command_type,
    target_type: input.target_type,
    actor: { type: "local", id: "aitejiu" },
    target_id: input.target_id,
    expected_version: input.expected_version,
    idempotency_key: identity.idempotencyKey,
    correlation_id: crypto.randomUUID(),
    payload: input.payload,
  };
  return apiCommand("/api/v1/commands", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  }) as Promise<CommandResponse>;
}
