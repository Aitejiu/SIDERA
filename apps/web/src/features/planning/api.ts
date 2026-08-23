import { apiCommand, apiGetAt, createCommandIdentity } from "@/shared/api/client";
import type { paths } from "@/shared/api/schema";

import type {
  PlanningBriefPage,
  AnswerPlanningClarificationPayload,
  CancelPlanningRequestPayload,
  ConfirmPlanningProposalPayload,
  PlanningClarificationPage,
  PlanningCommandRequest,
  PlanningCommandResponse,
  PlanningMaterialization,
  PlanningProfilePage,
  PlanningProposal,
  PlanningRequest,
  PlanningRunPage,
  PlanningClarificationAnswer,
  RejectPlanningProposalPayload,
  RequestProposalRevisionPayload,
  SubmitPlanningRequestPayload,
} from "./types";

const PLANNING_PROFILES_PATH = "/api/v1/workspaces/{workspace_id}/planning-profiles" as const;
const PLANNING_REQUEST_PATH = "/api/v1/workspaces/{workspace_id}/planning-requests/{request_id}" as const;

export function planningProfilesQueryKey(workspaceId: string) {
  return ["planning-profiles", workspaceId] as const;
}

export function planningRequestQueryKey(workspaceId: string, requestId: string) {
  return ["planning-request", workspaceId, requestId] as const;
}

export function planningBriefsQueryKey(workspaceId: string, requestId: string) {
  return ["planning-briefs", workspaceId, requestId] as const;
}

export function planningRunsQueryKey(workspaceId: string, requestId: string) {
  return ["planning-runs", workspaceId, requestId] as const;
}

export function planningClarificationsQueryKey(workspaceId: string, requestId: string) {
  return ["planning-clarifications", workspaceId, requestId] as const;
}

export function planningProposalQueryKey(
  workspaceId: string,
  requestId: string,
  revision: number,
) {
  return ["planning-proposal", workspaceId, requestId, revision] as const;
}

export function planningMaterializationQueryKey(workspaceId: string, requestId: string) {
  return ["planning-materialization", workspaceId, requestId] as const;
}

export async function listPlanningProfiles(
  workspaceId: string,
  signal?: AbortSignal,
): Promise<PlanningProfilePage> {
  const path = `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/planning-profiles`;
  const init = signal === undefined ? undefined : { signal };
  return apiGetAt(path, PLANNING_PROFILES_PATH as keyof paths, init);
}

export async function getPlanningRequest(
  workspaceId: string,
  requestId: string,
  signal?: AbortSignal,
): Promise<PlanningRequest> {
  const path = `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/planning-requests/${encodeURIComponent(requestId)}`;
  const init = signal === undefined ? undefined : { signal };
  return apiGetAt(path, PLANNING_REQUEST_PATH as keyof paths, init);
}

export async function listPlanningBriefs(
  workspaceId: string,
  requestId: string,
  signal?: AbortSignal,
): Promise<PlanningBriefPage> {
  return getPlanningSubresource(
    workspaceId,
    requestId,
    "briefs",
    "/api/v1/workspaces/{workspace_id}/planning-requests/{request_id}/briefs" as keyof paths,
    signal,
  );
}

export async function listPlanningRuns(
  workspaceId: string,
  requestId: string,
  signal?: AbortSignal,
): Promise<PlanningRunPage> {
  return getPlanningSubresource(
    workspaceId,
    requestId,
    "runs",
    "/api/v1/workspaces/{workspace_id}/planning-requests/{request_id}/runs" as keyof paths,
    signal,
  );
}

export async function listPlanningClarifications(
  workspaceId: string,
  requestId: string,
  signal?: AbortSignal,
): Promise<PlanningClarificationPage> {
  return getPlanningSubresource(
    workspaceId,
    requestId,
    "clarifications",
    "/api/v1/workspaces/{workspace_id}/planning-requests/{request_id}/clarifications" as keyof paths,
    signal,
  );
}

export async function getPlanningProposal(
  workspaceId: string,
  requestId: string,
  revision: number,
  signal?: AbortSignal,
): Promise<PlanningProposal> {
  const path = `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/planning-requests/${encodeURIComponent(requestId)}/proposals/${revision}`;
  const init = signal === undefined ? undefined : { signal };
  return apiGetAt(
    path,
    "/api/v1/workspaces/{workspace_id}/planning-requests/{request_id}/proposals/{revision}" as keyof paths,
    init,
  );
}

export async function getPlanningMaterialization(
  workspaceId: string,
  requestId: string,
  signal?: AbortSignal,
): Promise<PlanningMaterialization> {
  const path = `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/planning-requests/${encodeURIComponent(requestId)}/materialization`;
  const init = signal === undefined ? undefined : { signal };
  return apiGetAt(
    path,
    "/api/v1/workspaces/{workspace_id}/planning-requests/{request_id}/materialization" as keyof paths,
    init,
  );
}

export function buildSubmitPlanningRequestCommand({
  workspaceId,
  payload,
  actorId = "aitejiu",
  expectedVersion = null,
}: {
  workspaceId: string;
  payload: SubmitPlanningRequestPayload;
  actorId?: string;
  expectedVersion?: number | null;
}): PlanningCommandRequest {
  return buildPlanningCommand({
    workspaceId,
    actorId,
    commandType: "SubmitPlanningRequest",
    targetType: "workspace",
    targetId: null,
    expectedVersion,
    payload,
  });
}

export async function submitPlanningRequest(
  command: PlanningCommandRequest,
): Promise<PlanningCommandResponse> {
  return executePlanningCommand(command);
}

export function buildCancelPlanningRequestCommand({
  workspaceId,
  requestId,
  expectedVersion,
  actorId = "aitejiu",
}: {
  workspaceId: string;
  requestId: string;
  expectedVersion: number;
  actorId?: string;
}): PlanningCommandRequest {
  const payload: CancelPlanningRequestPayload = {};
  return buildPlanningCommand({
    workspaceId,
    actorId,
    commandType: "CancelPlanningRequest",
    targetType: "planning_request",
    targetId: requestId,
    expectedVersion,
    payload,
  });
}

export function buildAnswerPlanningClarificationCommand({
  workspaceId,
  requestId,
  expectedVersion,
  answers,
  actorId = "aitejiu",
}: {
  workspaceId: string;
  requestId: string;
  expectedVersion: number;
  answers: PlanningClarificationAnswer[];
  actorId?: string;
}): PlanningCommandRequest {
  const payload: AnswerPlanningClarificationPayload = { answers };
  return buildPlanningCommand({
    workspaceId,
    actorId,
    commandType: "AnswerPlanningClarification",
    targetType: "planning_request",
    targetId: requestId,
    expectedVersion,
    payload,
  });
}

export function buildRequestProposalRevisionCommand({
  workspaceId,
  requestId,
  expectedVersion,
  payload,
  actorId = "aitejiu",
}: {
  workspaceId: string;
  requestId: string;
  expectedVersion: number;
  payload: RequestProposalRevisionPayload;
  actorId?: string;
}): PlanningCommandRequest {
  return buildPlanningCommand({
    workspaceId,
    actorId,
    commandType: "RequestProposalRevision",
    targetType: "planning_request",
    targetId: requestId,
    expectedVersion,
    payload,
  });
}

export function buildRejectPlanningProposalCommand({
  workspaceId,
  requestId,
  expectedVersion,
  payload,
  actorId = "aitejiu",
}: {
  workspaceId: string;
  requestId: string;
  expectedVersion: number;
  payload: RejectPlanningProposalPayload;
  actorId?: string;
}): PlanningCommandRequest {
  return buildPlanningCommand({
    workspaceId,
    actorId,
    commandType: "RejectPlanningProposal",
    targetType: "planning_request",
    targetId: requestId,
    expectedVersion,
    payload,
  });
}

export function buildConfirmPlanningProposalCommand({
  workspaceId,
  requestId,
  expectedVersion,
  payload,
  actorId = "aitejiu",
}: {
  workspaceId: string;
  requestId: string;
  expectedVersion: number;
  payload: ConfirmPlanningProposalPayload;
  actorId?: string;
}): PlanningCommandRequest {
  return buildPlanningCommand({
    workspaceId,
    actorId,
    commandType: "ConfirmPlanningProposal",
    targetType: "planning_request",
    targetId: requestId,
    expectedVersion,
    payload,
  });
}

export function buildPlanningCommand({
  workspaceId,
  commandType,
  targetType,
  targetId,
  payload,
  expectedVersion = null,
  actorId = "aitejiu",
}: {
  workspaceId: string;
  commandType: string;
  targetType: PlanningCommandRequest["target_type"];
  targetId: string | null;
  payload: PlanningCommandRequest["payload"];
  expectedVersion?: number | null;
  actorId?: string;
}): PlanningCommandRequest {
  const identity = createCommandIdentity();
  return {
    command_id: identity.commandId,
    workspace_id: workspaceId,
    command_type: commandType,
    target_type: targetType,
    actor: { type: "local", id: actorId },
    target_id: targetId,
    expected_version: expectedVersion,
    idempotency_key: identity.idempotencyKey,
    correlation_id: crypto.randomUUID(),
    payload,
  };
}

export async function executePlanningCommand(
  command: PlanningCommandRequest,
): Promise<PlanningCommandResponse> {
  return apiCommand<PlanningCommandResponse, "/api/v1/commands">(
    "/api/v1/commands",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(command),
    },
  );
}

async function getPlanningSubresource<T>(
  workspaceId: string,
  requestId: string,
  resource: string,
  contractPath: keyof paths,
  signal: AbortSignal | undefined,
): Promise<T> {
  const path = `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/planning-requests/${encodeURIComponent(requestId)}/${resource}`;
  const init = signal === undefined ? undefined : { signal };
  return apiGetAt(path, contractPath, init) as Promise<T>;
}
