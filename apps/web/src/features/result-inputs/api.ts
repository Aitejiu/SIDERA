import { apiCommand, apiGetAt, createCommandIdentity } from "@/shared/api/client";
import type { components, paths } from "@/shared/api/schema";

export type WorkerResultInputsView = components["schemas"]["WorkerResultInputsResponse"];
export type ResultInputAction = WorkerResultInputsView["available_actions"][number];
type CommandRequest = components["schemas"]["CommandRequest"];
type CommandResponse = components["schemas"]["CommandResponse"];

const RESULT_INPUTS_PATH =
  "/api/v1/workspaces/{workspace_id}/worker-items/{worker_item_id}/result-inputs" as const;

export function workerResultInputsQueryKey(workspaceId: string, workerItemId: string) {
  return ["worker-result-inputs", workspaceId, workerItemId] as const;
}

export async function getWorkerResultInputs(
  workspaceId: string,
  workerItemId: string,
  options: { executionRequestId?: string; signal?: AbortSignal } = {},
): Promise<WorkerResultInputsView> {
  const params = new URLSearchParams();
  if (options.executionRequestId !== undefined) {
    params.set("execution_request_id", options.executionRequestId);
  }
  const query = params.toString();
  const path = `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/worker-items/${encodeURIComponent(workerItemId)}/result-inputs${query ? `?${query}` : ""}`;
  return apiGetAt(
    path,
    RESULT_INPUTS_PATH as keyof paths,
    options.signal === undefined ? undefined : { signal: options.signal },
  );
}

/**
 * Assembly retry is not a new client-side state machine. The query already
 * returns the exact assembly request identity and fingerprint; this adapter
 * only turns those facts into the existing command descriptor shape.
 */
export function retryContextAssemblyAction(
  view: WorkerResultInputsView,
): ResultInputAction | null {
  const delivery = view.delivery;
  if (
    delivery.assembly_request_id === null
    || delivery.assembly_version === null
    || delivery.assembly_request_fingerprint === null
    || !["blocked", "retry_wait"].includes(delivery.assembly_status ?? "")
  ) {
    return null;
  }
  return {
    action: "retry_context_assembly",
    command_type: "RetryContextAssembly",
    target_type: "context_assembly",
    target_id: delivery.assembly_request_id,
    expected_version: delivery.assembly_version,
    expected_fingerprint: delivery.assembly_request_fingerprint,
    expected_plan_generation: view.plan_generation,
    expected_input_selection_id: null,
    expected_input_selection_hash: null,
  };
}

export function resultInputActions(view: WorkerResultInputsView): ResultInputAction[] {
  const actions = [...view.available_actions];
  const retry = retryContextAssemblyAction(view);
  if (retry !== null && !actions.some((action) => action.command_type === retry.command_type)) {
    actions.push(retry);
  }
  return actions;
}

export async function executeResultInputAction(
  workspaceId: string,
  action: ResultInputAction,
  reason: string | null = null,
): Promise<CommandResponse> {
  if (action.expected_version === null) {
    throw new Error("当前结果输入操作缺少目标版本，请刷新后重试");
  }
  const payload = payloadForAction(action, reason);
  const identity = createCommandIdentity();
  const command: CommandRequest = {
    command_id: identity.commandId,
    workspace_id: workspaceId,
    command_type: action.command_type,
    actor: { type: "local", id: "aitejiu" },
    target_type: action.target_type as CommandRequest["target_type"],
    target_id: action.target_id,
    expected_version: action.expected_version,
    idempotency_key: identity.idempotencyKey,
    correlation_id: crypto.randomUUID(),
    payload,
  };
  return apiCommand<CommandResponse>("/api/v1/commands", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(command),
  });
}

function payloadForAction(
  action: ResultInputAction,
  reason: string | null,
): CommandRequest["payload"] {
  switch (action.command_type) {
    case "ApproveCollaborationReleaseProposal":
      return {
        transport: "web",
        reason,
        expected_input_selection_id: action.expected_input_selection_id,
        expected_input_selection_hash: action.expected_input_selection_hash,
      } satisfies components["schemas"]["ApproveCollaborationReleaseProposalPayload"];
    case "RejectCollaborationReleaseProposal":
      return {
        transport: "web",
        reason,
      } satisfies components["schemas"]["RejectCollaborationReleaseProposalPayload"];
    case "PauseCollaborationRelease":
      if (action.expected_plan_generation === null) {
        throw new Error("当前结果输入操作缺少计划代际");
      }
      return {
        transport: "web",
        reason,
        target_plan_generation: action.expected_plan_generation,
      } satisfies components["schemas"]["PauseCollaborationReleasePayload"];
    case "ResumeAutomaticCollaboration":
      return {
        transport: "web",
        reason,
      } satisfies components["schemas"]["ResumeAutomaticCollaborationPayload"];
    case "RetryContextAssembly":
      if (action.expected_fingerprint === null) {
        throw new Error("当前结果输入操作缺少组装请求指纹");
      }
      return {
        request_fingerprint: action.expected_fingerprint,
      } satisfies components["schemas"]["RetryContextAssemblyPayload"];
    default:
      throw new Error(`当前结果输入操作暂不支持：${action.action}`);
  }
}
