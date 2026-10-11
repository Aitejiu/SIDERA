import { buildNativeCommand } from "@/features/assignments/commands";
import type { CommandRequest } from "@/features/assignments/types";
import type { LocalSession } from "@/features/local-session/api";
import { apiCommand, apiGetAt, apiRequestJson, createCommandIdentity } from "@/shared/api/client";
import type { components, paths } from "@/shared/api/schema";

export type TaskResultOverview = components["schemas"]["TaskResultOverviewResponse"];
export type TaskResultDetail = components["schemas"]["TaskResultDetailResponse"];
export type ResultAction = TaskResultOverview["available_actions"][number];
export type ResultCommandResponse = components["schemas"]["CommandResponse"];
export type ResultCommandIdentity = ReturnType<typeof createCommandIdentity>;

export type AcceptanceCriterionDecision = {
  criterion_key: string;
  decision: "accept_passed" | "manual_passed" | "exception_accepted";
  reason: string | null;
};

export type CompleteTaskPayload = {
  candidate_id: string;
  candidate_hash: string;
  acceptance_basis_hash: string;
  expected_result_version: number;
  criteria: AcceptanceCriterionDecision[];
};

export type RequestTaskReworkPayload = {
  candidate_id: string;
  candidate_hash: string;
  mode: "automatic" | "user";
  issue_keys: string[];
  target_worker_item_ids: string[];
  impact_worker_item_ids: string[];
  reason: string;
  suggestion: {
    candidate_id: string;
    candidate_hash: string;
    issue_keys: string[];
    target_worker_item_ids: string[];
    reason: string;
  } | null;
};

export type AuthorizeAdditionalReworkPayload = {
  additional_rounds: number;
  additional_seconds: number;
  reason: string;
};

export type RetryResultVerificationPayload = {
  verification_operation_id: string;
  expected_operation_version: number;
  expected_result_version: number;
  candidate_id: string;
  candidate_hash: string;
  reason: string;
};

export type ReintegrateTaskResultPayload = {
  source_candidate_id: string;
  source_candidate_hash: string;
  replacement_result_ids: string[];
  expected_result_version: number;
  target_observation_id: string | null;
  reason: string;
};

export type ResultActionInput =
  | { command_type: "CompleteTask"; payload: CompleteTaskPayload }
  | { command_type: "RequestTaskRework"; payload: RequestTaskReworkPayload }
  | { command_type: "AuthorizeAdditionalRework"; payload: AuthorizeAdditionalReworkPayload }
  | { command_type: "RetryResultVerification"; payload: RetryResultVerificationPayload }
  | { command_type: "ReintegrateTaskResult"; payload: ReintegrateTaskResultPayload };

export type NativeResultOperationInput = Exclude<ResultActionInput, { command_type: "CompleteTask" }>;

export function buildNativeResultOperationCommand(
  workspaceId: string,
  rootTaskId: string,
  workerItemId: string,
  action: ResultAction,
  input: NativeResultOperationInput,
  session: LocalSession,
): CommandRequest {
  const retry = input.command_type === "RetryResultVerification";
  if (!nativeResource(workspaceId) || !nativeResource(rootTaskId) || !nativeResource(workerItemId)
    || action.command_type !== input.command_type
    || action.target_type !== (retry ? "result_operation" : "task")
    || !nativeResource(action.target_id) || (!retry && action.target_id !== rootTaskId)
    || !nativePositive(action.expected_version) || !nativePositive(action.expected_result_version)
    || (!retry && action.expected_version !== action.expected_result_version)
    || !(action.candidate_id === null || nativeResource(action.candidate_id))
    || !(action.candidate_hash === null || nativeHash(action.candidate_hash))
    || !(action.plan_content_hash === null || nativeHash(action.plan_content_hash))
    || !(action.expected_fingerprint === null || (nativeHash(action.expected_fingerprint)
      && action.expected_fingerprint === action.candidate_hash))
    || typeof action.requires_reason !== "boolean"
    || !nativeResourceList(action.allowed_worker_item_ids) || !nativeResourceList(action.replacement_result_ids)
    || !nativeOperationPayloadMatches(input, action, workerItemId)) {
    throw new Error("结果操作已变化，请重新审阅");
  }
  return buildNativeCommand({
    session, workspaceId, commandType: input.command_type,
    targetType: retry ? "result_operation" : "task", targetId: action.target_id,
    expectedVersion: action.expected_version, payload: input.payload,
  });
}

function nativeOperationPayloadMatches(input: NativeResultOperationInput, action: ResultAction, workerItemId: string): boolean {
  const payload = input.payload;
  if (typeof payload.reason !== "string" || payload.reason.trim().length === 0) return false;
  switch (input.command_type) {
    case "RequestTaskRework": {
      const value = input.payload;
      return nativeKeys(value, ["candidate_id", "candidate_hash", "mode", "issue_keys", "target_worker_item_ids", "impact_worker_item_ids", "reason", "suggestion"])
        && nativeResource(value.candidate_id) && nativeHash(value.candidate_hash)
        && value.candidate_id === action.candidate_id && value.candidate_hash === action.candidate_hash
        && value.mode === "user" && value.suggestion === null
        && Array.isArray(value.issue_keys) && value.issue_keys.every(key => typeof key === "string" && key.trim().length > 0)
        && nativeSingleWorker(value.target_worker_item_ids, workerItemId)
        && nativeSingleWorker(value.impact_worker_item_ids, workerItemId)
        && (action.allowed_worker_item_ids.length === 0 || action.allowed_worker_item_ids.includes(workerItemId));
    }
    case "AuthorizeAdditionalRework": {
      const value = input.payload;
      return nativeKeys(value, ["additional_rounds", "additional_seconds", "reason"])
        && nativeNonnegative(value.additional_rounds) && nativeNonnegative(value.additional_seconds)
        && (value.additional_rounds > 0 || value.additional_seconds > 0);
    }
    case "RetryResultVerification": {
      const value = input.payload;
      return nativeKeys(value, ["verification_operation_id", "expected_operation_version", "expected_result_version", "candidate_id", "candidate_hash", "reason"])
        && value.verification_operation_id === action.target_id
        && value.expected_operation_version === action.expected_version
        && value.expected_result_version === action.expected_result_version
        && nativeResource(value.candidate_id) && nativeHash(value.candidate_hash)
        && value.candidate_id === action.candidate_id && value.candidate_hash === action.candidate_hash;
    }
    case "ReintegrateTaskResult": {
      const value = input.payload;
      return nativeKeys(value, ["source_candidate_id", "source_candidate_hash", "replacement_result_ids", "expected_result_version", "target_observation_id", "reason"])
        && nativeResource(value.source_candidate_id) && nativeHash(value.source_candidate_hash)
        && value.source_candidate_id === action.candidate_id && value.source_candidate_hash === action.candidate_hash
        && value.expected_result_version === action.expected_result_version
        && nativeResourceList(value.replacement_result_ids) && value.replacement_result_ids.length > 0
        && value.replacement_result_ids.length === action.replacement_result_ids.length
        && value.replacement_result_ids.every((id, index) => id === action.replacement_result_ids[index])
        && (value.target_observation_id === null || nativeResource(value.target_observation_id));
    }
    default: return false;
  }
}

function nativeKeys(value: Record<string, unknown>, keys: string[]): boolean {
  return Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
}

function nativeResource(value: unknown): value is string {
  return typeof value === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);
}

function nativeHash(value: unknown): value is string {
  return typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
}

function nativePositive(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0;
}

function nativeNonnegative(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

function nativeResourceList(value: unknown): value is string[] {
  return Array.isArray(value) && value.every(nativeResource) && new Set(value).size === value.length;
}

function nativeSingleWorker(value: unknown, workerItemId: string): boolean {
  return Array.isArray(value) && value.length === 1 && value[0] === workerItemId;
}

const TASK_RESULTS_PATH =
  "/api/v1/workspaces/{workspace_id}/tasks/{root_task_id}/results" as const;

export type AcceptanceBasisResponse = {
  candidate_id: string;
  candidate_hash: string;
  expected_result_version: number;
  acceptance_basis_hash: string;
};

const RESULT_REFRESH_INTERVAL_MS = 1_000;

export function resultRefreshInterval(
  view: TaskResultOverview | undefined,
): number | false {
  if (view === undefined) return false;
  if (["applying", "finalizing", "integrating", "reworking", "verifying"].includes(view.result_stage)) {
    return RESULT_REFRESH_INTERVAL_MS;
  }
  if (view.current_candidate === null && view.result_stage !== "completed") {
    return RESULT_REFRESH_INTERVAL_MS;
  }
  return false;
}

export function taskResultsQueryKey(workspaceId: string, rootId: string) {
  return ["task-results", workspaceId, rootId] as const;
}

export async function getTaskResults(
  workspaceId: string,
  rootId: string,
  options: { cursor?: string; limit?: number; signal?: AbortSignal } = {},
): Promise<TaskResultOverview> {
  const query = new URLSearchParams();
  if (options.cursor !== undefined) query.set("cursor", options.cursor);
  if (options.limit !== undefined) query.set("limit", String(options.limit));
  const suffix = query.size > 0 ? `?${query.toString()}` : "";
  const path = `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/tasks/${encodeURIComponent(rootId)}/results${suffix}`;
  return apiGetAt(
    path,
    TASK_RESULTS_PATH as keyof paths,
    options.signal === undefined ? undefined : { signal: options.signal },
  );
}

export async function getTaskResultAcceptanceBasis(
  workspaceId: string,
  rootId: string,
  candidateId: string,
  criteria: AcceptanceCriterionDecision[],
): Promise<AcceptanceBasisResponse> {
  const path = `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/tasks/${encodeURIComponent(rootId)}/results/${encodeURIComponent(candidateId)}/acceptance-basis`;
  return apiRequestJson<AcceptanceBasisResponse>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ criteria }),
  });
}

export function buildNativeTaskAcceptanceCommand(
  workspaceId: string,
  action: ResultAction,
  payload: CompleteTaskPayload,
  session: LocalSession,
): CommandRequest {
  const resourceId = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  if (action.command_type !== "CompleteTask" || action.target_type !== "task"
    || action.target_id === null || !resourceId.test(action.target_id)
    || !Number.isSafeInteger(action.expected_version) || (action.expected_version ?? 0) < 1
    || action.candidate_id === null || action.candidate_hash === null
    || payload.candidate_id !== action.candidate_id || payload.candidate_hash !== action.candidate_hash
    || payload.expected_result_version !== action.expected_result_version) {
    throw new Error("结果验收动作已变化，请重新审阅");
  }
  return buildNativeCommand({
    session,
    workspaceId,
    commandType: "CompleteTask",
    targetType: "task",
    targetId: action.target_id,
    expectedVersion: action.expected_version,
    payload,
  });
}

export async function executeTaskResultAction(
  workspaceId: string,
  action: ResultAction,
  input: ResultActionInput,
  identity: ResultCommandIdentity = createCommandIdentity(),
): Promise<ResultCommandResponse> {
  if (action.command_type !== input.command_type) {
    throw new Error("结果动作与命令输入不一致，请刷新后重试");
  }
  if (action.target_type !== "task" && action.target_type !== "result_operation") {
    throw new Error("结果动作目标不受支持，请刷新后重试");
  }
  if (input.command_type === "CompleteTask"
    || input.command_type === "RequestTaskRework"
    || input.command_type === "RetryResultVerification") {
    if (action.candidate_id === null || action.candidate_hash === null) {
      throw new Error("结果动作缺少候选版本，请刷新后重试");
    }
    if (input.payload.candidate_id !== action.candidate_id) {
      throw new Error("结果候选已变化，请刷新后重试");
    }
    if (input.payload.candidate_hash !== action.candidate_hash) {
      throw new Error("结果候选指纹已变化，请刷新后重试");
    }
  }
  if (input.command_type === "ReintegrateTaskResult") {
    if (action.candidate_id === null || action.candidate_hash === null) {
      throw new Error("结果动作缺少候选版本，请刷新后重试");
    }
    if (input.payload.source_candidate_id !== action.candidate_id) {
      throw new Error("结果候选已变化，请刷新后重试");
    }
    if (input.payload.source_candidate_hash !== action.candidate_hash) {
      throw new Error("结果候选指纹已变化，请刷新后重试");
    }
  }
  if (
    input.command_type === "CompleteTask"
    && input.payload.expected_result_version !== action.expected_result_version
  ) {
    throw new Error("结果版本已变化，请刷新后重试");
  }
  if (
    input.command_type === "RetryResultVerification"
    && input.payload.expected_operation_version !== action.expected_version
  ) {
    throw new Error("检查操作版本已变化，请刷新后重试");
  }
  if (
    input.command_type === "ReintegrateTaskResult"
    && input.payload.expected_result_version !== action.expected_result_version
  ) {
    throw new Error("结果版本已变化，请刷新后重试");
  }

  const command: components["schemas"]["CommandRequest"] = {
    command_id: identity.commandId,
    workspace_id: workspaceId,
    command_type: action.command_type,
    actor: { type: "local", id: "aitejiu" },
    target_type: action.target_type as "task" | "result_operation",
    target_id: action.target_id,
    expected_version: action.expected_version,
    idempotency_key: identity.idempotencyKey,
    correlation_id: crypto.randomUUID(),
    payload: input.payload,
  };
  return apiCommand<ResultCommandResponse>("/api/v1/commands", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(command),
  });
}
