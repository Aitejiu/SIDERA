import {
  apiCommand,
  apiGetAt,
  createCommandIdentity,
} from "@/shared/api/client";
import type { paths } from "@/shared/api/schema";

import type {
  ActiveHandoff,
  CancelWorkerHandoffPayload,
  CommandRequest,
  CommandResponse,
  ConfirmWorkerHandoffPayload,
  HandoffCandidateList,
  HandoffProposal,
  HandoffTransition,
  PrepareWorkerHandoffPayload,
  RecoverWorkerHandoffPayload,
  RecoverWorkerTransitionPayload,
  WorkerActivityPage,
  WorkerTransition,
  WorkerWorkspace,
} from "./types";

export function workerWorkspaceQueryKey(workspaceId: string, workerItemId: string) {
  return ["worker-item-workspace", workspaceId, workerItemId] as const;
}

export function workerActivityQueryKey(workspaceId: string, workerItemId: string) {
  return ["worker-item-activity", workspaceId, workerItemId] as const;
}

export function handoffCandidatesQueryKey(workspaceId: string, workerItemId: string) {
  return ["worker-item-handoff-candidates", workspaceId, workerItemId] as const;
}

export function handoffProposalQueryKey(workspaceId: string, proposalId: string) {
  return ["worker-item-handoff-proposal", workspaceId, proposalId] as const;
}

export function handoffTransitionQueryKey(workspaceId: string, transitionId: string) {
  return ["worker-item-handoff-transition", workspaceId, transitionId] as const;
}

export function workerHandoffQueryKey(workspaceId: string, workerItemId: string) {
  return ["worker-item-handoff", workspaceId, workerItemId] as const;
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

export async function getHandoffCandidates(
  workspaceId: string,
  workerItemId: string,
  signal?: AbortSignal,
): Promise<HandoffCandidateList> {
  const path = `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/worker-items/${encodeURIComponent(workerItemId)}/handoff-candidates`;
  const init = signal === undefined ? undefined : { signal };
  return apiGetAt(
    path,
    "/api/v1/workspaces/{workspace_id}/worker-items/{worker_item_id}/handoff-candidates" as keyof paths,
    init,
  );
}

export async function getHandoffProposal(
  workspaceId: string,
  proposalId: string,
  signal?: AbortSignal,
): Promise<HandoffProposal> {
  const path = `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/handoff-proposals/${encodeURIComponent(proposalId)}`;
  const init = signal === undefined ? undefined : { signal };
  return apiGetAt(
    path,
    "/api/v1/workspaces/{workspace_id}/handoff-proposals/{proposal_id}" as keyof paths,
    init,
  );
}

export async function getHandoffTransition(
  workspaceId: string,
  transitionId: string,
  signal?: AbortSignal,
): Promise<HandoffTransition> {
  const path = `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/handoff-transitions/${encodeURIComponent(transitionId)}`;
  const init = signal === undefined ? undefined : { signal };
  return apiGetAt(
    path,
    "/api/v1/workspaces/{workspace_id}/handoff-transitions/{transition_id}" as keyof paths,
    init,
  );
}

export async function getWorkerHandoff(
  workspaceId: string,
  workerItemId: string,
  signal?: AbortSignal,
): Promise<ActiveHandoff | null> {
  const path = `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/worker-items/${encodeURIComponent(workerItemId)}/handoff`;
  const init = signal === undefined ? undefined : { signal };
  return apiGetAt(
    path,
    "/api/v1/workspaces/{workspace_id}/worker-items/{worker_item_id}/handoff" as keyof paths,
    init,
  );
}

export async function prepareWorkerHandoff(
  workspaceId: string,
  workerItemId: string,
  expectedVersion: number,
  payload: PrepareWorkerHandoffPayload,
): Promise<CommandResponse> {
  return executeHandoffCommand(workspaceId, {
    commandType: "PrepareWorkerHandoff",
    targetType: "worker_item",
    targetId: workerItemId,
    expectedVersion,
    payload,
  });
}

export async function confirmWorkerHandoff(
  workspaceId: string,
  proposal: Pick<HandoffProposal, "id" | "version" | "proposal_hash">,
  expectedWorkerItemVersion: number,
): Promise<CommandResponse> {
  const payload: ConfirmWorkerHandoffPayload = {
    proposal_id: proposal.id,
    expected_proposal_version: proposal.version,
    expected_proposal_hash: proposal.proposal_hash,
    expected_worker_item_version: expectedWorkerItemVersion,
  };
  return executeHandoffCommand(workspaceId, {
    commandType: "ConfirmWorkerHandoff",
    targetType: "handoff_proposal",
    targetId: proposal.id,
    expectedVersion: proposal.version,
    payload,
  });
}

export async function cancelWorkerHandoff(
  workspaceId: string,
  proposal: Pick<HandoffProposal, "id" | "version">,
  reason: string,
): Promise<CommandResponse> {
  const payload: CancelWorkerHandoffPayload = { reason };
  return executeHandoffCommand(workspaceId, {
    commandType: "CancelWorkerHandoff",
    targetType: "handoff_proposal",
    targetId: proposal.id,
    expectedVersion: proposal.version,
    payload,
  });
}

export async function recoverWorkerHandoff(
  workspaceId: string,
  transition: Pick<HandoffTransition, "id" | "version">,
  strategy: RecoverWorkerHandoffPayload["strategy"],
  reason: string,
  evidenceRefs: string[],
  replacementTargetWorkerId: string | null = null,
): Promise<CommandResponse> {
  const payload: RecoverWorkerHandoffPayload = {
    transition_id: transition.id,
    expected_transition_version: transition.version,
    strategy,
    reason,
    evidence_refs: evidenceRefs,
    replacement_target_worker_id: replacementTargetWorkerId,
  };
  return executeHandoffCommand(workspaceId, {
    commandType: "RecoverWorkerHandoff",
    targetType: "handoff_transition",
    targetId: transition.id,
    expectedVersion: transition.version,
    payload,
  });
}

async function executeHandoffCommand(
  workspaceId: string,
  input: {
    commandType: string;
    targetType: CommandRequest["target_type"];
    targetId: string;
    expectedVersion: number;
    payload: Record<string, unknown>;
  },
): Promise<CommandResponse> {
  const identity = createCommandIdentity();
  const command: CommandRequest = {
    command_id: identity.commandId,
    workspace_id: workspaceId,
    command_type: input.commandType,
    actor: { type: "local", id: "aitejiu" },
    target_type: input.targetType,
    target_id: input.targetId,
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

export async function getWorkerTransition(
  workspaceId: string,
  workerTransitionId: string,
  signal?: AbortSignal,
): Promise<WorkerTransition> {
  const path = `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/worker-transitions/${encodeURIComponent(workerTransitionId)}`;
  const init = signal === undefined ? undefined : { signal };
  return apiGetAt(
    path,
    "/api/v1/workspaces/{workspace_id}/worker-transitions/{worker_transition_id}" as keyof paths,
    init,
  );
}

export async function recoverWorkerTransition(
  workspaceId: string,
  transition: Pick<WorkerTransition, "worker_transition_id" | "version">,
  payload: Pick<RecoverWorkerTransitionPayload, "strategy" | "reason" | "evidence_refs">,
): Promise<CommandResponse> {
  const identity = createCommandIdentity();
  const command: CommandRequest = {
    command_id: identity.commandId,
    workspace_id: workspaceId,
    command_type: "RecoverWorkerTransition",
    actor: { type: "local", id: "aitejiu" },
    target_type: "worker_transition",
    target_id: transition.worker_transition_id,
    expected_version: transition.version,
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
