import { apiCommand, apiGetAt, createCommandIdentity } from "@/shared/api/client";
import type { components, paths } from "@/shared/api/schema";

import type { CollaborationDecisionPage, CollaborationDetail } from "./types";

const COLLABORATION_DETAIL_PATH =
  "/api/v1/workspaces/{workspace_id}/tasks/{root_task_id}/collaboration" as const;
const COLLABORATION_DECISIONS_PATH =
  "/api/v1/workspaces/{workspace_id}/tasks/{root_task_id}/collaboration/decisions" as const;

export type CollaborationDecisionQuery = components["schemas"]["CollaborationDecisionPageResponse"];
type CommandRequest = components["schemas"]["CommandRequest"];
type CommandResponse = components["schemas"]["CommandResponse"];

export function collaborationDetailQueryKey(workspaceId: string, rootTaskId: string) {
  return ["collaboration-detail", workspaceId, rootTaskId] as const;
}

export function collaborationDecisionsQueryKey(workspaceId: string, rootTaskId: string) {
  return ["collaboration-decisions", workspaceId, rootTaskId] as const;
}

export async function getCollaborationDetail(
  workspaceId: string,
  rootTaskId: string,
  signal?: AbortSignal,
): Promise<CollaborationDetail> {
  const path = `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/tasks/${encodeURIComponent(rootTaskId)}/collaboration`;
  const init = signal === undefined ? undefined : { signal };
  return apiGetAt(path, COLLABORATION_DETAIL_PATH as keyof paths, init);
}

export async function getCollaborationDecisions(
  workspaceId: string,
  rootTaskId: string,
  options: { cursor?: string; limit?: number; signal?: AbortSignal } = {},
): Promise<CollaborationDecisionPage> {
  const params = new URLSearchParams({
    limit: String(options.limit ?? 20),
  });
  if (options.cursor !== undefined) {
    params.set("cursor", options.cursor);
  }
  const path = `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/tasks/${encodeURIComponent(rootTaskId)}/collaboration/decisions?${params.toString()}`;
  const init = options.signal === undefined ? undefined : { signal: options.signal };
  return apiGetAt(path, COLLABORATION_DECISIONS_PATH as keyof paths, init);
}

export async function pauseCollaborationRelease(
  workspaceId: string,
  workerItemId: string,
  expectedWorkerItemVersion: number,
  targetPlanGeneration: number,
  reason: string | null,
): Promise<CommandResponse> {
  return executeGovernanceCommand(workspaceId, {
    commandType: "PauseCollaborationRelease",
    targetType: "worker_item",
    targetId: workerItemId,
    expectedVersion: expectedWorkerItemVersion,
    payload: {
      reason,
      target_plan_generation: targetPlanGeneration,
      transport: "web",
    },
  });
}

export async function approveCollaborationRelease(
  workspaceId: string,
  proposalId: string,
  expectedProposalVersion: number,
  reason: string | null,
  expectedInputSelection?: {
    id: string | null | undefined;
    hash: string | null | undefined;
  },
): Promise<CommandResponse> {
  const payload: Record<string, unknown> = { reason, transport: "web" };
  if (expectedInputSelection?.id && expectedInputSelection.hash) {
    payload.expected_input_selection_id = expectedInputSelection.id;
    payload.expected_input_selection_hash = expectedInputSelection.hash;
  }
  return executeGovernanceCommand(workspaceId, {
    commandType: "ApproveCollaborationReleaseProposal",
    targetType: "collaboration_release_proposal",
    targetId: proposalId,
    expectedVersion: expectedProposalVersion,
    payload,
  });
}

export async function rejectCollaborationRelease(
  workspaceId: string,
  proposalId: string,
  expectedProposalVersion: number,
  reason: string | null,
): Promise<CommandResponse> {
  return executeGovernanceCommand(workspaceId, {
    commandType: "RejectCollaborationReleaseProposal",
    targetType: "collaboration_release_proposal",
    targetId: proposalId,
    expectedVersion: expectedProposalVersion,
    payload: { reason, transport: "web" },
  });
}

export async function resumeAutomaticCollaboration(
  workspaceId: string,
  holdId: string,
  expectedHoldVersion: number,
  reason: string | null,
): Promise<CommandResponse> {
  return executeGovernanceCommand(workspaceId, {
    commandType: "ResumeAutomaticCollaboration",
    targetType: "collaboration_governance_hold",
    targetId: holdId,
    expectedVersion: expectedHoldVersion,
    payload: { reason, transport: "web" },
  });
}

async function executeGovernanceCommand(
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
