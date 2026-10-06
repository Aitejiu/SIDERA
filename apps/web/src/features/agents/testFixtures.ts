import type { Connection, Credential, ProfileDraft, ProfileRef, ProfileView, ProfileWork, ResourceDraft, ResourceRef, ResourceView, RuntimeDescriptor, RuntimeSnapshot } from "./types";

export const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
export const PROFILE_ID = "30000000-0000-0000-0000-000000000001";
export const RUNTIME_ID = "40000000-0000-0000-0000-000000000001";
export const PROFILE_REF: ProfileRef = { workspace_id: WORKSPACE_ID, profile_id: PROFILE_ID, revision: 1 };
export const PROFILE_DRAFT: ProfileDraft = {
  name: "Reusable reviewer", description: "Reviews proposed changes", instructions: "Reviewed instructions",
  runtime_ref: { workspace_id: WORKSPACE_ID, runtime_id: RUNTIME_ID }, resource_refs: [], model_id: null,
  thinking: null, max_active: 1, environment: {}, secret_refs: [], native_settings: {},
};
export const PROFILE: ProfileView = {
  profile: { ref: PROFILE_REF, draft: PROFILE_DRAFT, config_hash: "a".repeat(64), created_at: "2026-10-05T12:00:00Z" },
  status: { profile_id: PROFILE_ID, enabled: true, version: 1 },
};
const supported = { status: "supported", reason_code: "controlled_configuration", constraints: {} } as const;
export const RUNTIME: RuntimeSnapshot = {
  ref: PROFILE_DRAFT.runtime_ref, harness_id: "controlled-settings", executable: "/owned/bin/runtime",
  actual_version: "1", installed: true, enabled: true, version: 1, protocol_state: "ready",
  auth_state: "ready", capabilities: { mcp: supported }, checked_at: "2026-10-05T12:00:00Z",
  limits: { max_active: 1, max_sessions: 2 },
};
export const DESCRIPTOR: RuntimeDescriptor = {
  harness_id: RUNTIME.harness_id, display_name: "Controlled runtime", model_options: [], thinking_options: [],
  field_schema_version: 1,
  fields: [
    { key: "model_id", label: "模型", kind: "text", options: [], minimum: null, maximum: null, support: supported },
    { key: "thinking", label: "思考等级", kind: "select", options: [], minimum: null, maximum: null, support: { status: "unconfirmed", reason_code: "thinking_not_confirmed", constraints: {} } },
    { key: "native_settings.temperature", label: "Temperature", kind: "number", options: [], minimum: 0, maximum: 2, support: supported },
    { key: "native_settings.compact", label: "Compact", kind: "toggle", options: [], minimum: null, maximum: null, support: supported },
    { key: "native_settings.mode", label: "Mode", kind: "select", options: ["review", "build"], minimum: null, maximum: null, support: supported },
  ],
};

export function profileAt(revision: number, draft: ProfileDraft = PROFILE_DRAFT): ProfileView {
  return { ...PROFILE, profile: { ...PROFILE.profile, ref: { ...PROFILE_REF, revision }, draft } };
}

export const PROFILE_WORK: ProfileWork = {
  conversations: [],
  task_work: {
    next_cursor: null,
    items: [{
      workspace_id: WORKSPACE_ID, kind: "assignment", work_id: "50000000-0000-0000-0000-000000000001",
      profile_ref: PROFILE_REF, title: "Review actual Worker item", status: "running",
      updated_at: "2026-10-05T12:10:00Z", root_task_id: "60000000-0000-0000-0000-000000000001",
      worker_item_id: "50000000-0000-0000-0000-000000000001", target_type: "worker_item",
      binding_version: 2, execution_id: "70000000-0000-0000-0000-000000000001",
      run_id: "80000000-0000-0000-0000-000000000001", link_status: "accepted", event_cursor: 7,
    }],
  },
};

export const CREDENTIAL: Credential = {
  workspace_id: WORKSPACE_ID, credential_id: "a0000000-0000-0000-0000-000000000001", alias: "Controlled API credential",
  source: "macos_keychain", status: "active", version: 1, replaces_id: null,
};
export const CONNECTION: Connection = {
  workspace_id: WORKSPACE_ID, connection_id: "b0000000-0000-0000-0000-000000000001", revision: 1,
  name: "Trusted controlled connection", provider_id: "controlled", protocol: "openai_compatible",
  base_url: "http://127.0.0.1:19001/v1", network_scope: "loopback", allowed_roles: ["main_agent", "worker", "planner"],
  credential_id: CREDENTIAL.credential_id, availability: "configured",
};
export const RESOURCE_REF: ResourceRef = { workspace_id: WORKSPACE_ID, resource_id: "90000000-0000-0000-0000-000000000001", revision: 1 };
export const RESOURCE_DRAFT: ResourceDraft = { name: "Reviewed skill", kind: "skill", settings: { body: "Original inline skill" }, secret_refs: [] };
export const RESOURCE: ResourceView = {
  resource: { ref: RESOURCE_REF, draft: RESOURCE_DRAFT, content_hash: "b".repeat(64), created_at: "2026-10-05T12:00:00Z" },
  status: { resource_id: RESOURCE_REF.resource_id, enabled: true, version: 1 },
};

export function resourceAt(revision: number, draft: ResourceDraft = RESOURCE_DRAFT): ResourceView {
  return { ...RESOURCE, resource: { ...RESOURCE.resource, ref: { ...RESOURCE_REF, revision }, draft } };
}
