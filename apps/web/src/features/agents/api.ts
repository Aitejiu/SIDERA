import type { components } from "@/shared/api/product-schema";
import { localSessionRequest } from "@/shared/api/localSession";
import type {
  ConfigValidation, ConfigurationResult, Connection, Credential, CredentialRevocation,
  CredentialWrite, CredentialWriteInput, Defaults, DefaultsBody, NativeRunSummary,
  ProfileDraft, ProfileList, ProfileRef, ProfileStatus, ProfileView, ProfileWork,
  ResourceDraft, ResourceList, ResourceRef, ResourceStatus, ResourceView,
  RuntimeCandidate, RuntimeDescriptor, RuntimeRef, RuntimeSnapshot,
} from "./types";

export class AgentApiProblem extends Error {
  constructor(readonly status: number, readonly code: string) {
    super(code === "local_session_required" || code === "management_unauthorized" ? "需要本地会话"
      : code === "permission_denied" || code === "management_scope_denied" ? "当前会话无权读取此工作区"
        : code === "version_conflict" || code === "revision_conflict" ? "配置版本已变化"
          : "无法确认服务返回的结果");
    this.name = "AgentApiProblem";
  }
}

const base = (workspaceId: string) => `/agents/v1/workspaces/${encodeURIComponent(workspaceId)}`;
const product = (workspaceId: string) => `/product/v1/workspaces/${encodeURIComponent(workspaceId)}`;
const credentials = (workspaceId: string) => `/model-config/v1/workspaces/${encodeURIComponent(workspaceId)}`;
const json = (method: string, body: unknown): RequestInit => ({ method, body: JSON.stringify(body) });

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

async function response(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (init.body !== undefined) headers.set("Content-Type", "application/json");
  try {
    return await fetch(path, localSessionRequest({ ...init, headers, cache: "no-store" }));
  } catch {
    throw new AgentApiProblem(0, "network_unavailable");
  }
}

async function problem(reply: Response): Promise<AgentApiProblem> {
  let body: unknown;
  try { body = await reply.json(); } catch { body = null; }
  const detail = record(body) ? record(body.detail) ? body.detail : record(body.error) ? body.error : body : null;
  const code = detail && typeof detail.code === "string" && /^[a-z_]{1,80}$/.test(detail.code)
    ? detail.code : `http_${reply.status}`;
  return new AgentApiProblem(reply.status, code);
}

export async function agentRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  const reply = await response(path, init);
  if (!reply.ok) throw await problem(reply);
  return await reply.json() as T;
}

async function configuration(path: string, operationId: string, init: RequestInit): Promise<ConfigurationResult> {
  const reply = await response(path, init);
  if (!reply.ok && ![403, 404, 409, 422].includes(reply.status)) throw await problem(reply);
  const body: unknown = await reply.json();
  if (!record(body) || body.operationId !== operationId || !record(body.target_ref)
    || !["claimed", "dispatching", "succeeded", "reconciled", "rejected", "unknown"].includes(String(body.state))) {
    throw new AgentApiProblem(reply.status, "invalid_response");
  }
  return body as ConfigurationResult;
}

export const listProfiles = (ws: string, signal?: AbortSignal) => agentRequest<ProfileList>(base(ws) + "/profiles", signal ? { signal } : {});
export const readProfile = (ws: string, id: string, revision?: number, signal?: AbortSignal) => agentRequest<ProfileView>(base(ws) + `/profiles/${encodeURIComponent(id)}${revision === undefined ? "" : `?revision=${revision}`}`, signal ? { signal } : {});
export const saveProfile = (ws: string, operationId: string, draft: ProfileDraft, expected_ref?: ProfileRef) => configuration(base(ws) + (expected_ref ? `/profiles/${expected_ref.profile_id}/revisions` : "/profiles"), operationId, json("POST", { operationId, draft, ...(expected_ref ? { expected_ref } : {}) }));
export const setProfileEnabled = (ref: ProfileRef, enabled: boolean, expected_version: number) => agentRequest<ProfileStatus>(base(ref.workspace_id) + `/profiles/${ref.profile_id}/status`, json("PUT", { enabled, expected_version }));
export const validateProfile = (ref: ProfileRef) => agentRequest<ConfigValidation>(base(ref.workspace_id) + `/profiles/${ref.profile_id}/validation?revision=${ref.revision}`);
export const listProfileWork = (ws: string, id: string, cursor?: string, limit = 50) => {
  const query = new URLSearchParams({ limit: String(limit) });
  if (cursor !== undefined) query.set("cursor", cursor);
  return agentRequest<ProfileWork>(base(ws) + `/profiles/${encodeURIComponent(id)}/work?${query}`);
};
export const readWorkRun = (ws: string, kind: "assignment" | "execution" | "planning", id: string, targetType?: "subtask" | "worker_item") => agentRequest<NativeRunSummary>(`/api/v1/workspaces/${encodeURIComponent(ws)}/profile-work/${kind}/${encodeURIComponent(id)}/run${targetType ? `?target_type=${targetType}` : ""}`);
export const listResources = (ws: string) => agentRequest<ResourceList>(base(ws) + "/resources");
export const readResource = (ws: string, id: string, revision?: number) => agentRequest<ResourceView>(base(ws) + `/resources/${encodeURIComponent(id)}${revision === undefined ? "" : `?revision=${revision}`}`);
export const saveResource = (ws: string, operationId: string, draft: ResourceDraft, expected_ref?: ResourceRef) => configuration(base(ws) + (expected_ref ? `/resources/${expected_ref.resource_id}/revisions` : "/resources"), operationId, json("POST", { operationId, draft, ...(expected_ref ? { expected_ref } : {}) }));
export const setResourceEnabled = (ref: ResourceRef, enabled: boolean, expected_version: number) => agentRequest<ResourceStatus>(base(ref.workspace_id) + `/resources/${ref.resource_id}/status`, json("PUT", { enabled, expected_version }));
export const readConfigurationRequest = (ws: string, operationId: string) => configuration(product(ws) + `/configuration-requests?${new URLSearchParams({ operationId })}`, operationId, {});
export const listRuntimes = (ws: string) => agentRequest<components["schemas"]["RuntimeListView"]>(base(ws) + "/runtimes");
export const listDescriptors = (ws: string) => agentRequest<RuntimeDescriptor[]>(base(ws) + "/runtime-descriptors");
export const discoverRuntimes = (ws: string) => agentRequest<RuntimeCandidate[]>(base(ws) + "/runtimes/discovery", { method: "POST" });
export const registerRuntime = (ws: string, body: components["schemas"]["RuntimeRegisterBody"]) => agentRequest<RuntimeSnapshot>(base(ws) + "/runtimes", json("POST", body));
export const readRuntime = (ref: RuntimeRef) => agentRequest<RuntimeSnapshot>(base(ref.workspace_id) + `/runtimes/${ref.runtime_id}`);
export const refreshRuntime = (ref: RuntimeRef) => agentRequest<RuntimeSnapshot>(base(ref.workspace_id) + `/runtimes/${ref.runtime_id}/refresh`, json("POST", { ref }));
export const setRuntimeLimits = (ref: RuntimeRef, max_active: number, max_sessions: number, expected_version: number) => agentRequest<RuntimeSnapshot>(base(ref.workspace_id) + `/runtimes/${ref.runtime_id}/limits`, json("PUT", { ref, max_active, max_sessions, expected_version }));
export const setRuntimeEnabled = (ref: RuntimeRef, enabled: boolean, expected_version: number) => agentRequest<RuntimeSnapshot>(base(ref.workspace_id) + `/runtimes/${ref.runtime_id}/status`, json("PUT", { ref, enabled, expected_version }));
export const readDefaults = (ws: string) => agentRequest<Defaults>(product(ws) + "/defaults");
export const writeDefaults = (ws: string, body: DefaultsBody) => agentRequest<Defaults>(product(ws) + "/defaults", json("PUT", body));
export const listCredentials = (ws: string) => agentRequest<Credential[]>(credentials(ws) + "/credentials");
export const listConnections = (ws: string) => agentRequest<Connection[]>(credentials(ws) + "/connections");
export const writeCredential = (ws: string, body: CredentialWriteInput) => agentRequest<CredentialWrite>(credentials(ws) + "/credential-writes", json("POST", body));
export const readCredentialWrite = (ws: string, operationId: string) => agentRequest<CredentialWrite>(credentials(ws) + `/credential-writes/${encodeURIComponent(operationId)}`);
export const recoverCredentialWrite = (ws: string, operationId: string) => agentRequest<CredentialWrite>(credentials(ws) + `/credential-writes/${encodeURIComponent(operationId)}/recover`, { method: "POST" });
export const revokeCredential = (ws: string, id: string, expected_version: number) => agentRequest<CredentialRevocation>(credentials(ws) + `/credentials/${encodeURIComponent(id)}/revoke`, json("POST", { expected_version }));
