import type { LocalSession } from "@/features/local-session/api";
import { apiCommand, apiGetAt, createCommandIdentity } from "@/shared/api/client";
import {
  AssignmentUnavailableError,
  type CommandRequest,
  type CommandResponse,
  type CommandStatusResponse,
} from "./types";

const COMMANDS_PATH = "/api/v1/commands" as const;
const COMMAND_STATUS_PATH = "/api/v1/workspaces/{workspace_id}/commands/{command_id}" as const;

export function buildNativeCommand({
  session,
  workspaceId,
  commandType,
  targetType,
  targetId,
  expectedVersion,
  payload,
}: {
  session: LocalSession;
  workspaceId: string;
  commandType: string;
  targetType: CommandRequest["target_type"];
  targetId: string | null;
  expectedVersion: number | null;
  payload: CommandRequest["payload"];
}): CommandRequest {
  const expiresAt = Date.parse(session.expires_at);
  if (session.workspace_id !== workspaceId || session.session_id.trim().length === 0
    || session.principal_id.trim().length === 0 || !Number.isFinite(expiresAt)
    || expiresAt <= Date.now()) {
    throw new AssignmentUnavailableError();
  }

  const copiedPayload = cloneJsonObject(payload);
  const identity = createCommandIdentity();
  const command: CommandRequest = {
    command_id: identity.commandId,
    workspace_id: workspaceId,
    command_type: commandType,
    actor: { type: "user", id: session.principal_id },
    target_type: targetType,
    target_id: targetId,
    expected_version: expectedVersion,
    idempotency_key: identity.idempotencyKey,
    correlation_id: crypto.randomUUID(),
    payload: copiedPayload,
  };
  return deepFreeze(command);
}

export async function submitNativeCommand(original: CommandRequest): Promise<CommandResponse> {
  try {
    return await apiCommand<CommandResponse, typeof COMMANDS_PATH>(COMMANDS_PATH, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(original),
    });
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") throw error;
    throw new AssignmentUnavailableError();
  }
}

export async function lookupNativeCommand(
  original: CommandRequest,
  signal?: AbortSignal,
): Promise<CommandStatusResponse> {
  const path = `/api/v1/workspaces/${encodeURIComponent(original.workspace_id)}/commands/${encodeURIComponent(original.command_id)}`;
  try {
    const response: unknown = await apiGetAt(
      path,
      COMMAND_STATUS_PATH,
      signal === undefined ? undefined : { signal },
    );
    const status = response;
    if (!isRecord(status) || status.command_id !== original.command_id
      || hasMismatch(status, "command_type", original.command_type)
      || hasMismatch(status, "target_type", original.target_type)
      || hasMismatch(status, "target_id", original.target_id)
      || hasMismatch(status, "expected_version", original.expected_version)
      || (status.result !== null && (!isRecord(status.result)
        || status.result.command_id !== original.command_id))) {
      throw new AssignmentUnavailableError();
    }
    return status as CommandStatusResponse;
  } catch (error) {
    if (error instanceof AssignmentUnavailableError) throw error;
    if (error instanceof Error && error.name === "AbortError") throw error;
    throw new AssignmentUnavailableError();
  }
}

function cloneJsonObject(value: Record<string, unknown>): Record<string, unknown> {
  let serialized: string | undefined;
  try {
    serialized = JSON.stringify(value);
  } catch {
    throw new AssignmentUnavailableError();
  }
  if (serialized === undefined) throw new AssignmentUnavailableError();
  let clone: unknown;
  try {
    clone = JSON.parse(serialized);
  } catch {
    throw new AssignmentUnavailableError();
  }
  if (!isRecord(clone)) throw new AssignmentUnavailableError();
  return clone;
}

function deepFreeze<T>(value: T): T {
  if (typeof value !== "object" || value === null || Object.isFrozen(value)) return value;
  for (const child of Object.values(value)) deepFreeze(child);
  return Object.freeze(value);
}

function hasMismatch<K extends "command_type" | "target_type" | "target_id" | "expected_version" >(
  status: Record<string, unknown>,
  key: K,
  original: CommandRequest[K],
): boolean {
  return status[key] !== undefined && status[key] !== original;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
