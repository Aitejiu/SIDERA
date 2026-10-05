import type {
  ChatProfileRef,
  MainAgentCapabilities,
  MainAgentErrorEnvelope,
  MainAgentHealth,
  MainAgentThread,
  MainAgentThreadList,
  MainAgentTurn,
} from "./types";
import { localSessionRequest } from "@/shared/api/localSession";

const API_BASE = "/main-agent/v1";

export class MainAgentApiProblem extends Error {
  readonly status: number;
  readonly code: string;
  readonly retryable: boolean;

  constructor(status: number, code: string, message: string, retryable: boolean) {
    super(message);
    this.name = "MainAgentApiProblem";
    this.status = status;
    this.code = code;
    this.retryable = retryable;
  }
}

export function mainAgentHealthQueryKey(workspaceId: string) {
  return ["main-agent", workspaceId, "health"] as const;
}

export function mainAgentThreadsQueryKey(workspaceId: string) {
  return ["main-agent", workspaceId, "threads"] as const;
}

export function mainAgentThreadQueryKey(workspaceId: string, threadId: string) {
  return ["main-agent", workspaceId, "thread", threadId] as const;
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (!headers.has("Accept")) headers.set("Accept", "application/json");
  if (init?.body !== undefined && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const response = await fetch(`${API_BASE}${path}`, {
    ...localSessionRequest({ ...init, headers }),
  });
  if (!response.ok) {
    throw await parseApiProblem(response);
  }
  return (await response.json()) as T;
}

async function parseApiProblem(response: Response): Promise<MainAgentApiProblem> {
  let parsed: unknown;
  try {
    parsed = (await response.json()) as unknown;
  } catch {
    parsed = undefined;
  }
  const envelope = isErrorEnvelope(parsed) ? parsed.error : undefined;
  return new MainAgentApiProblem(
    response.status,
    envelope?.code ?? `http_${response.status}`,
    envelope?.message ?? `Gateway request failed (${response.status})`,
    envelope?.retryable ?? response.status >= 500,
  );
}

function isErrorEnvelope(value: unknown): value is MainAgentErrorEnvelope {
  if (!isRecord(value) || !isRecord(value.error)) {
    return false;
  }
  return (
    typeof value.error.code === "string" &&
    typeof value.error.message === "string" &&
    typeof value.error.retryable === "boolean"
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

export function getHealth(signal?: AbortSignal): Promise<MainAgentHealth> {
  return requestJson<MainAgentHealth>("/health", requestOptions(signal));
}

export function getCapabilities(): Promise<MainAgentCapabilities> {
  return requestJson<MainAgentCapabilities>("/capabilities");
}

export function listThreads(
  cursor?: string,
  limit = 50,
  signal?: AbortSignal,
): Promise<MainAgentThreadList> {
  const query = new URLSearchParams({ limit: String(limit) });
  if (cursor !== undefined) {
    query.set("cursor", cursor);
  }
  return requestJson<MainAgentThreadList>(`/threads?${query.toString()}`, requestOptions(signal));
}

export function createThread(
  payload: Pick<MainAgentThread, "contextRefs"> & { title?: string | null; profileRef?: ChatProfileRef },
): Promise<MainAgentThread> {
  return requestJson<MainAgentThread>("/threads", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getThread(threadId: string, signal?: AbortSignal): Promise<MainAgentThread> {
  return requestJson<MainAgentThread>(
    `/threads/${encodeURIComponent(threadId)}`,
    requestOptions(signal),
  );
}

export function startTurn(
  threadId: string,
  input: string,
  operationId?: string,
): Promise<MainAgentTurn> {
  return requestJson<MainAgentTurn>(
    `/threads/${encodeURIComponent(threadId)}/turns`,
    {
      method: "POST",
      body: JSON.stringify({
        input,
        ...(operationId === undefined ? {} : { operationId }),
      }),
    },
  );
}

export function getTurn(
  threadId: string,
  turnId: string,
  signal?: AbortSignal,
): Promise<MainAgentTurn> {
  return requestJson<MainAgentTurn>(
    `/threads/${encodeURIComponent(threadId)}/turns/${encodeURIComponent(turnId)}`,
    requestOptions(signal),
  );
}

function requestOptions(signal: AbortSignal | undefined): RequestInit {
  return signal === undefined ? {} : { signal };
}

export function interruptTurn(
  threadId: string,
  turnId: string,
): Promise<MainAgentTurn> {
  return requestJson<MainAgentTurn>(
    `/threads/${encodeURIComponent(threadId)}/turns/${encodeURIComponent(turnId)}/interrupt`,
    { method: "POST" },
  );
}
