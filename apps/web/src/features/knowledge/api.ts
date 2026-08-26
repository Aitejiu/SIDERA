import { apiRequestJson, createCommandIdentity } from "@/shared/api/client";
import type { paths as KnowledgePaths } from "@/shared/api/knowledge-schema";

import type {
  KnowledgeCandidateDetail,
  KnowledgeCandidateList,
  KnowledgeCommandResponse,
  KnowledgeCommandStatus,
  KnowledgePageDetail,
  KnowledgePageList,
  KnowledgeRevision,
  KnowledgeRevisionDiff,
  KnowledgeSearchResponse,
  KnowledgeSourceList,
  SubmitCandidatePayload,
  SubmitKnowledgeCandidateCommand,
} from "./types";

const KNOWLEDGE_API_BASE = "/knowledge-api";

const KNOWLEDGE_PAGES_PATH =
  "/api/v1/workspaces/{workspace_id}/knowledge/pages" as keyof KnowledgePaths;
const KNOWLEDGE_PAGE_PATH =
  "/api/v1/workspaces/{workspace_id}/knowledge/pages/{page_id}" as keyof KnowledgePaths;
const KNOWLEDGE_REVISION_PATH =
  "/api/v1/workspaces/{workspace_id}/knowledge/pages/{page_id}/revisions/{revision_id}" as keyof KnowledgePaths;
const KNOWLEDGE_SEARCH_PATH =
  "/api/v1/workspaces/{workspace_id}/knowledge/search" as keyof KnowledgePaths;
const KNOWLEDGE_CANDIDATES_PATH =
  "/api/v1/workspaces/{workspace_id}/knowledge/candidates" as keyof KnowledgePaths;
const KNOWLEDGE_CANDIDATE_PATH =
  "/api/v1/workspaces/{workspace_id}/knowledge/candidates/{candidate_id}" as keyof KnowledgePaths;
const KNOWLEDGE_SOURCES_PATH =
  "/api/v1/workspaces/{workspace_id}/knowledge/sources" as keyof KnowledgePaths;
const KNOWLEDGE_COMMAND_STATUS_PATH =
  "/api/v1/workspaces/{workspace_id}/commands/{command_id}" as keyof KnowledgePaths;

export function knowledgePagesQueryKey(workspaceId: string) {
  return ["knowledge", workspaceId, "pages"] as const;
}

export function knowledgeSearchQueryKey(
  workspaceId: string,
  query: string,
  tags: readonly string[] = [],
  sourceTypes: readonly string[] = [],
) {
  return ["knowledge", workspaceId, "search", query, tags, sourceTypes] as const;
}

export function knowledgePageQueryKey(workspaceId: string, pageId: string) {
  return ["knowledge", workspaceId, "page", pageId] as const;
}

export function knowledgeRevisionQueryKey(
  workspaceId: string,
  pageId: string,
  revisionId: string,
) {
  return ["knowledge", workspaceId, "revision", pageId, revisionId] as const;
}

export function knowledgeCandidatesQueryKey(
  workspaceId: string,
  filters: Record<string, string | undefined> = {},
) {
  return ["knowledge", workspaceId, "candidates", filters] as const;
}

export function knowledgeCandidateQueryKey(workspaceId: string, candidateId: string) {
  return ["knowledge", workspaceId, "candidate", candidateId] as const;
}

export function knowledgeSourcesQueryKey(workspaceId: string) {
  return ["knowledge", workspaceId, "sources"] as const;
}

export function knowledgeCommandQueryKey(workspaceId: string, commandId: string) {
  return ["knowledge", workspaceId, "command", commandId] as const;
}

export async function listKnowledgePages(
  workspaceId: string,
  options: { includeArchived?: boolean; cursor?: string; limit?: number; signal?: AbortSignal } = {},
): Promise<KnowledgePageList> {
  const query = new URLSearchParams({
    include_archived: String(options.includeArchived ?? false),
    limit: String(options.limit ?? 50),
  });
  if (options.cursor !== undefined) query.set("cursor", options.cursor);
  return knowledgeGet<KnowledgePageList>(
    `${workspacePath(workspaceId)}/knowledge/pages?${query.toString()}`,
    KNOWLEDGE_PAGES_PATH,
    options.signal,
  );
}

export async function searchKnowledge(
  workspaceId: string,
  queryText: string,
  options: {
    tags?: readonly string[];
    sourceTypes?: readonly string[];
    status?: string;
    limit?: number;
    signal?: AbortSignal;
  } = {},
): Promise<KnowledgeSearchResponse> {
  const query = new URLSearchParams({
    q: queryText,
    status: options.status ?? "active",
    limit: String(options.limit ?? 20),
  });
  for (const tag of options.tags ?? []) query.append("tags", tag);
  for (const sourceType of options.sourceTypes ?? []) query.append("source_types", sourceType);
  return knowledgeGet<KnowledgeSearchResponse>(
    `${workspacePath(workspaceId)}/knowledge/search?${query.toString()}`,
    KNOWLEDGE_SEARCH_PATH,
    options.signal,
  );
}

export function getKnowledgePage(
  workspaceId: string,
  pageId: string,
  signal?: AbortSignal,
): Promise<KnowledgePageDetail>;
export function getKnowledgePage(
  workspaceId: string,
  pageId: string,
  revisionId: string,
  signal?: AbortSignal,
): Promise<KnowledgeRevision>;
export function getKnowledgePage(
  workspaceId: string,
  pageId: string,
  revisionIdOrSignal?: string | AbortSignal,
  signal?: AbortSignal,
): Promise<KnowledgePageDetail | KnowledgeRevision> {
  const revisionId = typeof revisionIdOrSignal === "string" ? revisionIdOrSignal : undefined;
  const requestSignal = typeof revisionIdOrSignal === "string" ? signal : revisionIdOrSignal;
  const path = revisionId === undefined
    ? `${workspacePath(workspaceId)}/knowledge/pages/${encodeURIComponent(pageId)}`
    : `${workspacePath(workspaceId)}/knowledge/pages/${encodeURIComponent(pageId)}/revisions/${encodeURIComponent(revisionId)}`;
  return knowledgeGet<KnowledgePageDetail | KnowledgeRevision>(
    path,
    revisionId === undefined ? KNOWLEDGE_PAGE_PATH : KNOWLEDGE_REVISION_PATH,
    requestSignal,
  );
}

export async function listKnowledgeCandidates(
  workspaceId: string,
  options: {
    status?: string;
    sourceTaskId?: string;
    sourceSubtaskId?: string;
    sourceWorkerItemId?: string;
    sourceExecutionId?: string;
    cursor?: string;
    limit?: number;
    signal?: AbortSignal;
  } = {},
): Promise<KnowledgeCandidateList> {
  const query = new URLSearchParams({ limit: String(options.limit ?? 50) });
  const values: Array<[string, string | undefined]> = [
    ["status", options.status],
    ["source_task_id", options.sourceTaskId],
    ["source_subtask_id", options.sourceSubtaskId],
    ["source_worker_item_id", options.sourceWorkerItemId],
    ["source_execution_id", options.sourceExecutionId],
    ["cursor", options.cursor],
  ];
  for (const [key, value] of values) {
    if (value !== undefined) query.set(key, value);
  }
  return knowledgeGet<KnowledgeCandidateList>(
    `${workspacePath(workspaceId)}/knowledge/candidates?${query.toString()}`,
    KNOWLEDGE_CANDIDATES_PATH,
    options.signal,
  );
}

export function getKnowledgeCandidate(
  workspaceId: string,
  candidateId: string,
  signal?: AbortSignal,
): Promise<KnowledgeCandidateDetail> {
  return knowledgeGet<KnowledgeCandidateDetail>(
    `${workspacePath(workspaceId)}/knowledge/candidates/${encodeURIComponent(candidateId)}`,
    KNOWLEDGE_CANDIDATE_PATH,
    signal,
  );
}

export function listKnowledgeSources(
  workspaceId: string,
  options: { availability?: string; cursor?: string; limit?: number; signal?: AbortSignal } = {},
): Promise<KnowledgeSourceList> {
  const query = new URLSearchParams({ limit: String(options.limit ?? 50) });
  if (options.availability !== undefined) query.set("availability", options.availability);
  if (options.cursor !== undefined) query.set("cursor", options.cursor);
  return knowledgeGet<KnowledgeSourceList>(
    `${workspacePath(workspaceId)}/knowledge/sources?${query.toString()}`,
    KNOWLEDGE_SOURCES_PATH,
    options.signal,
  );
}

export function getKnowledgeDiff(
  workspaceId: string,
  pageId: string,
  fromRevisionId: string,
  toRevisionId: string,
  signal?: AbortSignal,
): Promise<KnowledgeRevisionDiff> {
  const query = new URLSearchParams({
    from_revision_id: fromRevisionId,
    to_revision_id: toRevisionId,
  });
  return knowledgeGet<KnowledgeRevisionDiff>(
    `${workspacePath(workspaceId)}/knowledge/pages/${encodeURIComponent(pageId)}/diff?${query.toString()}`,
    "/api/v1/workspaces/{workspace_id}/knowledge/pages/{page_id}/diff" as keyof KnowledgePaths,
    signal,
  );
}

export function buildSubmitKnowledgeCandidateCommand({
  workspaceId,
  payload,
  actorId = "aitejiu",
}: {
  workspaceId: string;
  payload: SubmitCandidatePayload;
  actorId?: string;
}): SubmitKnowledgeCandidateCommand {
  const identity = createCommandIdentity();
  return {
    command_id: identity.commandId,
    workspace_id: workspaceId,
    command_type: "SubmitKnowledgeCandidate",
    target_type: "knowledge_candidate",
    actor: { type: "local_owner", id: actorId },
    target_id: payload.candidate_id,
    expected_version: null,
    idempotency_key: identity.idempotencyKey,
    correlation_id: crypto.randomUUID(),
    payload,
  };
}

export function submitKnowledgeCandidate(
  command: SubmitKnowledgeCandidateCommand,
  signal?: AbortSignal,
): Promise<KnowledgeCommandResponse> {
  const init: RequestInit = {
    method: "POST",
    body: JSON.stringify(command),
  };
  if (signal !== undefined) init.signal = signal;
  return knowledgeRequest<KnowledgeCommandResponse>("/api/v1/commands", init);
}

export function getKnowledgeCommandStatus(
  workspaceId: string,
  commandId: string,
  signal?: AbortSignal,
): Promise<KnowledgeCommandStatus> {
  return knowledgeGet<KnowledgeCommandStatus>(
    `${workspacePath(workspaceId)}/commands/${encodeURIComponent(commandId)}`,
    KNOWLEDGE_COMMAND_STATUS_PATH,
    signal,
  );
}

async function knowledgeGet<T>(
  path: string,
  _contractPath: keyof KnowledgePaths,
  signal?: AbortSignal,
): Promise<T> {
  return knowledgeRequest<T>(path, signal === undefined ? {} : { signal });
}

function knowledgeRequest<T>(path: string, init: RequestInit): Promise<T> {
  return apiRequestJson<T>(`${KNOWLEDGE_API_BASE}${path}`, init);
}

function workspacePath(workspaceId: string): string {
  return `/api/v1/workspaces/${encodeURIComponent(workspaceId)}`;
}
