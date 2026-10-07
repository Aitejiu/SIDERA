import { apiGetAt } from "@/shared/api/client";
import type { paths } from "@/shared/api/schema";

import { AssignmentUnavailableError } from "@/features/assignments/types";
import type { ExecutionInput, ExecutionInputPage } from "./types";

const INPUTS_PATH = "/api/v1/gateway/inputs" as keyof paths;
const DELIVERY_STATUSES = new Set([
  "accepted",
  "queued",
  "dispatching",
  "delivered",
  "blocked",
  "failed",
  "cancelled",
  "superseded",
]);

export interface NativeInputPageOptions {
  cursor?: string;
  limit?: number;
  signal?: AbortSignal;
}

export async function readNativeExecutionInputPage(
  workspaceId: string,
  executionId: string,
  sessionId: string,
  options: NativeInputPageOptions = {},
): Promise<ExecutionInputPage> {
  const limit = options.limit ?? 100;
  if (!isNonEmptyString(workspaceId) || !isNonEmptyString(executionId)
    || !isNonEmptyString(sessionId) || !Number.isInteger(limit) || limit < 1 || limit > 100
    || (options.cursor !== undefined && typeof options.cursor !== "string")) {
    throw new AssignmentUnavailableError();
  }

  const params = new URLSearchParams({
    workspace_id: workspaceId,
    execution_id: executionId,
    session_id: sessionId,
    limit: String(limit),
  });
  if (options.cursor !== undefined) params.set("cursor", options.cursor);
  const path = `${INPUTS_PATH}?${params.toString()}`;

  try {
    const response: unknown = await apiGetAt(
      path,
      INPUTS_PATH,
      options.signal === undefined ? undefined : { signal: options.signal },
    );
    if (!isRecord(response) || !Array.isArray(response.items)
      || !(response.next_cursor === null || typeof response.next_cursor === "string")
      || !response.items.every((item) => isScopedExecutionInput(item, workspaceId, executionId, sessionId))) {
      throw new AssignmentUnavailableError();
    }
    return response as unknown as ExecutionInputPage;
  } catch (error) {
    if (error instanceof AssignmentUnavailableError) throw error;
    if (error instanceof Error && error.name === "AbortError") throw error;
    throw new AssignmentUnavailableError();
  }
}

function isScopedExecutionInput(
  value: unknown,
  workspaceId: string,
  executionId: string,
  sessionId: string,
): value is ExecutionInput {
  return isRecord(value)
    && isUuid(value.id)
    && value.workspace_id === workspaceId
    && value.execution_id === executionId
    && value.session_id === sessionId
    && typeof value.status === "string" && DELIVERY_STATUSES.has(value.status)
    && isNonEmptyString(value.client_idempotency_key)
    && typeof value.created_at === "string"
    && (value.failure_code === null || typeof value.failure_code === "string")
    && (value.operation_id === null || isUuid(value.operation_id))
    && isNonNegativeInteger(value.attachment_count)
    && isNonNegativeInteger(value.text_length)
    && typeof value.updated_at === "string"
    && isPositiveInteger(value.version);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function isUuid(value: unknown): value is string {
  return typeof value === "string"
    && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);
}

function isNonNegativeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

function isPositiveInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0;
}
