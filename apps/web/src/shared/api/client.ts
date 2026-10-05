import type { paths } from "./schema";
import { localSessionRequest } from "./localSession";

type GetOperation<Path extends keyof paths> = paths[Path] extends {
  get: infer Operation;
}
  ? Operation
  : never;

type JsonBody<Operation> = Operation extends { responses: infer Responses }
  ? Responses extends { 200: infer Response }
    ? Response extends { content: infer Content }
      ? Content extends { "application/json": infer Body }
        ? Body
        : unknown
      : unknown
    : unknown
  : unknown;

export class ApiProblem extends Error {
  readonly status: number;
  readonly code: string;
  readonly detail: string;
  readonly details: Record<string, unknown>;
  readonly correlationId: string | null;

  constructor(
    status: number,
    code: string,
    detail: string,
    details: Record<string, unknown>,
    correlationId: string | null,
  ) {
    super(detail);
    this.name = "ApiProblem";
    this.status = status;
    this.code = code;
    this.detail = detail;
    this.details = details;
    this.correlationId = correlationId;
  }
}

export async function apiGet<Path extends keyof paths>(
  path: Path,
  init?: RequestInit,
): Promise<JsonBody<GetOperation<Path>>> {
  const response = await fetch(path as string, {
    ...init,
    method: "GET",
  });
  return parseResponse<JsonBody<GetOperation<Path>>>(response);
}

export async function apiGetAt<Path extends keyof paths>(
  path: string,
  contractPath: Path,
  init?: RequestInit,
): Promise<JsonBody<GetOperation<Path>>> {
  void contractPath;
  const response = await fetch(path, {
    ...init,
    method: "GET",
  });
  return parseResponse<JsonBody<GetOperation<Path>>>(response);
}

export async function apiRequestJson<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  if (!headers.has("Accept")) headers.set("Accept", "application/json");
  if (init.body !== undefined && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const response = await fetch(path, {
    ...localSessionRequest({ ...init, headers }),
  });
  return parseResponse<T>(response);
}

export async function apiGetBlobAt<Path extends keyof paths>(
  path: string,
  contractPath: Path,
  init?: RequestInit,
): Promise<Blob> {
  void contractPath;
  const response = await fetch(path, {
    ...init,
    method: "GET",
  });
  if (!response.ok) {
    throw await toApiProblem(response);
  }
  return response.blob();
}

export async function apiCommand<Response = unknown, Path extends keyof paths = keyof paths>(
  path: Path,
  init: RequestInit,
): Promise<Response> {
  const response = await fetch(path as string, localSessionRequest(init));
  return parseResponse<Response>(response);
}

export function createCommandIdentity() {
  return {
    commandId: crypto.randomUUID(),
    idempotencyKey: crypto.randomUUID(),
  };
}

async function parseResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    throw await toApiProblem(response);
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

async function toApiProblem(response: Response): Promise<ApiProblem> {
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    body = undefined;
  }
  const error = isRecord(body) && isRecord(body.error) ? body.error : null;
  const code = error && typeof error.code === "string" ? error.code : "http_error";
  const detail =
    error && typeof error.message === "string"
      ? error.message
      : response.statusText || "Request failed";
  const details =
    error && isRecord(error.details) ? error.details : {};
  const correlationId = response.headers.get("X-Correlation-Id");
  return new ApiProblem(response.status, code, detail, details, correlationId);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
