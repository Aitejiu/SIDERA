import type { AgentExecutionView, NativeWorkerTraceView, ProfileRef } from "@/features/assignments/types";
import { ApiProblem, apiGetAt } from "@/shared/api/client";
import type { Execution, ExecutionSession, NativeRunEvent } from "./types";

export interface TraceSelectionAddress {
  rootTaskId: string;
  executionId: string;
  sessionId: string | null;
}

export interface NativeTracePin {
  workspaceId: string;
  workerItemId: string;
  executionId: string;
  principalId: string;
  runId: string;
  profileRef: ProfileRef;
}

export class NativeTraceUnavailableError extends Error {
  constructor(readonly kind: "loading" | "denied" | "unsupported" | "unavailable" = "unavailable") {
    super("Trace data is unavailable.");
    this.name = "NativeTraceUnavailableError";
  }
}

export type WorkerTraceSelection =
  | { mode: "native"; pin: NativeTracePin }
  | { mode: "legacy"; session: ExecutionSession };

export async function readSavedTraceExecution(
  workspaceId: string, executionId: string, signal?: AbortSignal,
): Promise<AgentExecutionView> {
  if (!isUuid(workspaceId) || !isUuid(executionId)) throw new NativeTraceUnavailableError();
  try {
    const value: unknown = await apiGetAt(
      `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/agent-executions/${encodeURIComponent(executionId)}`,
      "/api/v1/workspaces/{workspace_id}/agent-executions/{execution_id}",
      signal === undefined ? undefined : { signal },
    );
    if (!isSavedExecution(value) || value.workspace_id !== workspaceId || value.execution_id !== executionId) {
      throw new NativeTraceUnavailableError();
    }
    return value;
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") throw error;
    throw traceReadError(error);
  }
}

export function reviewTraceSelection(input: {
  workspaceId: string;
  address: TraceSelectionAddress;
  principalId: string;
  execution: Execution | undefined;
  agentExecution: AgentExecutionView | undefined;
  session: ExecutionSession | null;
}): WorkerTraceSelection {
  const { workspaceId, address, principalId, execution, agentExecution: saved, session } = input;
  if (!isUuid(workspaceId) || !isUuid(address.rootTaskId) || !isUuid(address.executionId)
    || !isText(principalId) || !execution || !isSavedExecution(saved)
    || execution.workspace_id !== workspaceId || execution.id !== address.executionId
    || !isUuid(execution.task_id) || !isNullableUuid(execution.worker_item_id)
    || saved.workspace_id !== workspaceId || saved.execution_id !== address.executionId
    || saved.subtask_id !== execution.task_id || saved.worker_item_id !== execution.worker_item_id
    || (saved.root_task_id !== null && saved.root_task_id !== address.rootTaskId)) {
    throw new NativeTraceUnavailableError();
  }

  const summaries = [
    [execution.assigned_worker_id, execution.assigned_worker_summary],
    [execution.pinned_worker_id, execution.pinned_worker_summary],
  ] as const;
  for (const [id, summary] of summaries) {
    if (!isNullableUuid(id) || (summary !== null && (!summary || summary.id !== id || !isText(summary.adapter_type)))) {
      throw new NativeTraceUnavailableError();
    }
  }
  const adapters = summaries.flatMap(([, summary]) => summary === null ? [] : [summary.adapter_type]);
  if (session !== null && (session.id !== address.sessionId || session.workspace_id !== workspaceId
    || session.execution_id !== address.executionId || !isText(session.adapter_session_id)
    || !summaries.some(([id]) => id === session.worker_id && isUuid(id)))) {
    throw new NativeTraceUnavailableError();
  }

  const hasNativePins = saved.profile_ref !== null || saved.run_id !== null
    || saved.binding_version !== null || saved.link_status !== null || saved.event_cursor !== null;
  const nativeEvidence = hasNativePins || adapters.includes("agent-run")
    || session?.adapter_session_id.startsWith("agent-run:") === true;
  if (nativeEvidence) {
    if (!isUuid(saved.worker_item_id) || saved.root_task_id !== address.rootTaskId
      || !isProfileRef(saved.profile_ref, workspaceId) || !isPositiveInteger(saved.binding_version)
      || !isUuid(saved.run_id) || !isText(saved.link_status)
      || adapters.some((adapter) => adapter !== "agent-run")
      || (session !== null && session.adapter_session_id !== `agent-run:${saved.run_id}`)) {
      throw new NativeTraceUnavailableError();
    }
    return { mode: "native", pin: { workspaceId, workerItemId: saved.worker_item_id,
      executionId: address.executionId, principalId, runId: saved.run_id,
      profileRef: { ...saved.profile_ref } } };
  }
  if (session === null || !isUuid(address.sessionId) || adapters.length === 0
    || session.adapter_session_id.startsWith("agent-run:")
    || !summaries.some(([id, summary]) => id === session.worker_id && summary !== null)) {
    throw new NativeTraceUnavailableError();
  }
  return { mode: "legacy", session };
}

export async function readNativeTracePage(
  pin: NativeTracePin,
  options: { afterSequence?: number; limit?: number; signal?: AbortSignal } = {},
): Promise<NativeWorkerTraceView> {
  const after = options.afterSequence ?? 0;
  const limit = options.limit ?? 100;
  if (!isUuid(pin.workspaceId) || !isUuid(pin.workerItemId) || !isUuid(pin.executionId)
    || !isUuid(pin.runId) || !isText(pin.principalId) || !isProfileRef(pin.profileRef, pin.workspaceId)
    || !isNonNegativeInteger(after) || !isPositiveInteger(limit) || limit > 500) {
    throw new NativeTraceUnavailableError();
  }
  const params = new URLSearchParams({ after_sequence: String(after), limit: String(limit) });
  try {
    const value: unknown = await apiGetAt(
      `/api/v1/workspaces/${encodeURIComponent(pin.workspaceId)}/worker-items/${encodeURIComponent(pin.workerItemId)}/executions/${encodeURIComponent(pin.executionId)}/native-trace?${params.toString()}`,
      "/api/v1/workspaces/{workspace_id}/worker-items/{worker_item_id}/executions/{execution_id}/native-trace",
      options.signal === undefined ? undefined : { signal: options.signal },
    );
    return validateNativeTracePage(value, pin, after, limit);
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") throw error;
    throw traceReadError(error, true);
  }
}

export function traceReadError(error: unknown, allowUnsupported = false): NativeTraceUnavailableError {
  if (error instanceof NativeTraceUnavailableError) return error;
  if (error instanceof ApiProblem) {
    if (error.status === 401 || error.status === 403) return new NativeTraceUnavailableError("denied");
    if (allowUnsupported && error.status === 503 && error.code === "native_trace_unsupported") {
      return new NativeTraceUnavailableError("unsupported");
    }
  }
  return new NativeTraceUnavailableError();
}

export function nativeTracePinKey(pin: NativeTracePin): string {
  return JSON.stringify([pin.workspaceId, pin.principalId, pin.workerItemId, pin.executionId,
    pin.runId, pin.profileRef.workspace_id, pin.profileRef.profile_id, pin.profileRef.revision]);
}

export function validateNativeTracePage(
  value: unknown, pin: NativeTracePin, after: number, limit = 100,
): NativeWorkerTraceView {
  if (!isRecord(value) || value.workspace_id !== pin.workspaceId || value.worker_item_id !== pin.workerItemId
    || value.execution_id !== pin.executionId || value.run_id !== pin.runId
    || !isProfileRef(value.profile_ref, pin.workspaceId)
    || value.profile_ref.profile_id !== pin.profileRef.profile_id
    || value.profile_ref.revision !== pin.profileRef.revision
    || !Array.isArray(value.events) || value.events.length > limit
    || !isNonNegativeInteger(value.next_sequence) || typeof value.has_more !== "boolean") {
    throw new NativeTraceUnavailableError();
  }
  let cursor = after;
  for (const event of value.events) {
    if (!isPublicEvent(event, pin.runId) || event.sequence <= cursor) throw new NativeTraceUnavailableError();
    cursor = event.sequence;
  }
  if (value.next_sequence !== cursor || (value.has_more && cursor <= after)) throw new NativeTraceUnavailableError();
  return value as NativeWorkerTraceView;
}

function isSavedExecution(value: unknown): value is AgentExecutionView {
  return isRecord(value) && isUuid(value.workspace_id) && isUuid(value.execution_id)
    && isUuid(value.subtask_id) && isNullableUuid(value.root_task_id) && isNullableUuid(value.worker_item_id)
    && isText(value.status) && isTimestamp(value.updated_at)
    && (value.profile_ref === null || isProfileRef(value.profile_ref, value.workspace_id))
    && (value.binding_version === null || isPositiveInteger(value.binding_version))
    && isNullableUuid(value.run_id) && (value.link_status === null || isText(value.link_status))
    && (value.event_cursor === null || isNonNegativeInteger(value.event_cursor));
}

function isPublicEvent(value: unknown, runId: string): value is NativeRunEvent {
  return isRecord(value) && value.run_id === runId && isPositiveInteger(value.sequence)
    && isText(value.kind) && isTimestamp(value.occurred_at)
    && isNullableUuid(value.operation_id) && isNullableUuid(value.response_id)
    && isRecord(value.data) && isJson(value.data);
}

function isJson(value: unknown): boolean {
  if (value === null || typeof value === "string" || typeof value === "boolean") return true;
  if (typeof value === "number") return Number.isFinite(value);
  if (Array.isArray(value)) return value.every(isJson);
  return isRecord(value) && Object.values(value).every(isJson);
}

function isProfileRef(value: unknown, workspaceId: string): value is ProfileRef {
  return isRecord(value) && value.workspace_id === workspaceId && isUuid(value.profile_id)
    && isPositiveInteger(value.revision);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function isText(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}
function isUuid(value: unknown): value is string {
  return typeof value === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);
}
function isNullableUuid(value: unknown): value is string | null {
  return value === null || isUuid(value);
}
function isNonNegativeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}
function isPositiveInteger(value: unknown): value is number {
  return isNonNegativeInteger(value) && value > 0;
}
function isTimestamp(value: unknown): value is string {
  return isText(value) && Number.isFinite(Date.parse(value));
}
