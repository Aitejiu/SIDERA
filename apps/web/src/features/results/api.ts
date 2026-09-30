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
