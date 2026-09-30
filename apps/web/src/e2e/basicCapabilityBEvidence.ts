type JsonRecord = Record<string, unknown>;

export type PlanTransitionEvidenceStatus = "confirmed" | "converged" | "unknown" | "failed";

export function isIntegrationDispatchStalled(input: {
  sourceStatuses: readonly string[];
  integrationHasExecution: boolean;
  stalledForMs: number;
}): boolean {
  return input.sourceStatuses.length > 0
    && input.sourceStatuses.every((status) => status === "succeeded")
    && !input.integrationHasExecution
    && input.stalledForMs >= 180_000;
}

export function isWorkerWorkspaceTerminalForIntervention(value: unknown): boolean {
  if (!isRecord(value) || !isRecord(value.current_runtime)) return false;
  const runtime = value.current_runtime;
  const hasSession = typeof runtime.session_id === "string";
  return hasSession && (
    ["succeeded", "failed", "canceled", "cancelled", "lost"].includes(String(runtime.execution_status))
    || ["closed", "lost"].includes(String(runtime.session_status))
  );
}

export function isWritableWorkerWorkspaceSnapshot(value: unknown): value is JsonRecord {
  if (!isRecord(value)
    || value.can_intervene !== true
    || value.read_only_reason !== null
    || !isRecord(value.intervention_context)
    || !isRecord(value.current_runtime)) return false;

  const context = value.intervention_context;
  const runtime = value.current_runtime;
  return typeof context.execution_id === "string"
    && context.execution_id === runtime.execution_id
    && typeof context.session_id === "string"
    && context.session_id === runtime.session_id
    && runtime.session_status === "active"
    && !["succeeded", "failed", "canceled", "cancelled", "lost"].includes(String(runtime.execution_status))
    && runtime.input_mode !== "unsupported";
}

export function taskGraphSubtaskKeys(nodes: readonly unknown[]): string[] {
  const keys: string[] = [];
  for (const node of nodes) {
    if (!isRecord(node) || (node.kind !== "child" && node.kind !== "integration")) continue;
    if (typeof node.subtask_key !== "string" || node.subtask_key.trim().length === 0) {
      throw new Error("task_graph_subtask_key_invalid");
    }
    keys.push(node.subtask_key);
  }
  return keys;
}

export function findExactExecutionInput(
  rows: readonly unknown[],
  expected: {
    clientIdempotencyKey: string;
    executionId: string;
    sessionId: string;
  },
): JsonRecord | null {
  const matches = rows.filter((row): row is JsonRecord => (
    isRecord(row) && row.client_idempotency_key === expected.clientIdempotencyKey
  ));
  if (matches.length > 1) throw new Error("intervention_input_identity_ambiguous");
  const input = matches[0];
  if (input === undefined) return null;
  if (input.execution_id !== expected.executionId || input.session_id !== expected.sessionId) {
    throw new Error("intervention_input_context_mismatch");
  }
  return input;
}

export function candidateHistoryItems(value: unknown): JsonRecord[] {
  if (!isRecord(value)
    || !Array.isArray(value.items)
    || (value.next_cursor !== null && typeof value.next_cursor !== "string")
    || value.items.some((item) => !isRecord(item))) {
    throw new Error("result_candidate_history_invalid");
  }
  return value.items;
}

export function findPostInputTurnCompletionSequence(
  events: readonly unknown[],
  afterSequence: number,
): number | null {
  const ordered = events
    .filter((event): event is JsonRecord => (
      isRecord(event)
      && Number.isInteger(event.sequence)
      && typeof event.sequence === "number"
      && event.sequence > afterSequence
    ))
    .sort((left, right) => Number(left.sequence) - Number(right.sequence));

  for (const event of ordered) {
    if (event.title === "回合已完成") {
      return Number(event.sequence);
    }
  }
  return null;
}

export function unaffectedSourceResultIds(
  sourceResults: readonly unknown[],
  targetWorkerItemId: string,
): string[] {
  const ids = new Set<string>();
  for (const source of sourceResults) {
    if (!isRecord(source)
      || typeof source.snapshot_id !== "string"
      || typeof source.worker_item_id !== "string") {
      throw new Error("result_source_identity_invalid");
    }
    if (source.worker_item_id !== targetWorkerItemId) ids.add(source.snapshot_id);
  }
  return [...ids].sort();
}

export function classifyPlanTransitionStatus(value: unknown): PlanTransitionEvidenceStatus {
  if (!isRecord(value)) return "unknown";
  if (value.status === "converged") return "converged";
  if (value.status === "converging") return "confirmed";
  if (value.status !== "degraded" || !Array.isArray(value.workers)) return "unknown";

  const workers = value.workers.filter(isRecord);
  if (workers.some((worker) => worker.status === "blocked_unknown" || worker.writer_state === "unknown")) {
    return "unknown";
  }
  if (workers.some((worker) => worker.status === "failed")) return "failed";
  return "unknown";
}

function isRecord(value: unknown): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
