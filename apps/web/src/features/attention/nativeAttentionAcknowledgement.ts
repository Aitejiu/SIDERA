import type { CommandRequest } from "../assignments/types";
import type { components } from "@/shared/api/schema";

export type NativeAttentionItem = components["schemas"]["TaskConsoleAttentionProjectionResponse"];
export interface NativeAttentionScope {
  workspaceId: string;
  principalId: string;
  rootTaskId: string;
}
export interface NativeAttentionIntent {
  rootTaskVersion: number;
  attentionKey: string;
  sourceCorrelationId: string;
  sourceResourceVersion: number;
}
export type NativeAttentionStatus = "idle" | "pending" | "unknown" | "applied" | "rejected";
export interface NativeAttentionDraft {
  schema_version: 1;
  scope: NativeAttentionScope;
  intent: NativeAttentionIntent | null;
  original: CommandRequest | null;
  status: NativeAttentionStatus;
  outcome: "acknowledged" | null;
  rejection_code: "command_rejected" | null;
}
export type NativeAttentionStorageError = "invalid_draft" | "storage_unavailable";
type ResultReview = Pick<NativeAttentionDraft, "status" | "outcome" | "rejection_code">;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function nativeAttentionDraftKey(scope: NativeAttentionScope): string {
  return `sidera.native-attention.v1:${encodeURIComponent(scope.workspaceId)}:${encodeURIComponent(scope.principalId)}:${encodeURIComponent(scope.rootTaskId)}`;
}

export function emptyNativeAttentionDraft(scope: NativeAttentionScope): NativeAttentionDraft {
  return { schema_version: 1, scope, intent: null, original: null, status: "idle", outcome: null, rejection_code: null };
}

export function freezeNativeAttentionIntent(
  item: Pick<NativeAttentionItem, "key" | "correlation_id" | "resource_version" | "kind">,
  displayedRootVersion: number | undefined,
): NativeAttentionIntent | null {
  if (item.kind === "collaboration_release_proposal" || !positiveInteger(displayedRootVersion)
    || !nonblank(item.key) || !uuid(item.correlation_id) || !positiveInteger(item.resource_version)) return null;
  return Object.freeze({
    rootTaskVersion: displayedRootVersion, attentionKey: item.key,
    sourceCorrelationId: item.correlation_id, sourceResourceVersion: item.resource_version,
  });
}

export function nativeAttentionPayload(intent: NativeAttentionIntent): CommandRequest["payload"] {
  return {
    attention_key: intent.attentionKey, source_correlation_id: intent.sourceCorrelationId,
    source_resource_version: intent.sourceResourceVersion,
  };
}

export function loadNativeAttentionDraft(scope: NativeAttentionScope):
  { ok: true; draft: NativeAttentionDraft } | { ok: false; error: NativeAttentionStorageError } {
  try {
    if (!validScope(scope)) return { ok: false, error: "invalid_draft" };
    const stored = globalThis.localStorage.getItem(nativeAttentionDraftKey(scope));
    const draft: unknown = stored === null ? emptyNativeAttentionDraft(scope) : JSON.parse(stored);
    if (!validDraft(draft, scope)) return { ok: false, error: "invalid_draft" };
    return { ok: true, draft: freeze(draft) };
  } catch { return { ok: false, error: "storage_unavailable" }; }
}

export function saveNativeAttentionDraft(scope: NativeAttentionScope, draft: NativeAttentionDraft):
  { ok: true } | { ok: false; error: NativeAttentionStorageError } {
  try {
    if (!validDraft(draft, scope)) return { ok: false, error: "invalid_draft" };
    const serialized = JSON.stringify(draft);
    if (!validDraft(JSON.parse(serialized), scope)) return { ok: false, error: "invalid_draft" };
    globalThis.localStorage.setItem(nativeAttentionDraftKey(scope), serialized);
    return { ok: true };
  } catch { return { ok: false, error: "storage_unavailable" }; }
}

export function reviewNativeAttentionResult(original: CommandRequest, response: unknown): ResultReview {
  if (original.command_type !== "AcknowledgeAttentionItem" || original.target_type !== "task"
    || !positiveInteger(original.expected_version) || !record(response)
    || response.command_id !== original.command_id || response.aggregate_id !== original.target_id
    || response.aggregate_version !== original.expected_version
    || !Array.isArray(response.event_ids) || !response.event_ids.every(uuid)
    || (response.data !== undefined && !record(response.data))) {
    return { status: "unknown", outcome: null, rejection_code: null };
  }
  return { status: "applied", outcome: "acknowledged", rejection_code: null };
}

export function reviewNativeAttentionJournal(original: CommandRequest, journal: unknown): ResultReview | null {
  if (!record(journal) || journal.command_id !== original.command_id
    || journal.command_type !== original.command_type || journal.target_type !== original.target_type
    || journal.target_id !== original.target_id || journal.expected_version !== original.expected_version
    || (journal.payload !== undefined && !samePayload(journal.payload, original.payload))) return null;
  if (journal.status === "applied") {
    if (journal.result === null) return { status: "applied", outcome: null, rejection_code: null };
    const result = reviewNativeAttentionResult(original, journal.result);
    return result.outcome === "acknowledged" ? result : null;
  }
  if (journal.status === "rejected" && journal.result === null) {
    return { status: "rejected", outcome: null, rejection_code: "command_rejected" };
  }
  return null;
}

export function unresolvedNativeAttentionDraft(draft: NativeAttentionDraft): boolean {
  return draft.status === "pending" || draft.status === "unknown"
    || (draft.status === "applied" && draft.outcome === null);
}

function validDraft(value: unknown, scope: NativeAttentionScope): value is NativeAttentionDraft {
  if (!validScope(scope) || !record(value)
    || !exact(value, ["schema_version", "scope", "intent", "original", "status", "outcome", "rejection_code"])
    || value.schema_version !== 1 || !validScope(value.scope)
    || value.scope.workspaceId !== scope.workspaceId || value.scope.principalId !== scope.principalId
    || value.scope.rootTaskId !== scope.rootTaskId || typeof value.status !== "string"
    || !["idle", "pending", "unknown", "applied", "rejected"].includes(value.status)
    || !(value.outcome === null || typeof value.outcome === "string" && value.outcome === "acknowledged")
    || !(value.rejection_code === null || typeof value.rejection_code === "string" && value.rejection_code === "command_rejected")) return false;
  if (value.status === "idle") {
    return value.intent === null && value.original === null && value.outcome === null && value.rejection_code === null;
  }
  if (!validIntent(value.intent) || !validOriginal(value.original, scope, value.intent)) return false;
  if (value.status === "rejected") return value.outcome === null && value.rejection_code === "command_rejected";
  return value.rejection_code === null && (value.status === "applied" || value.outcome === null);
}

function validOriginal(value: unknown, scope: NativeAttentionScope, intent: NativeAttentionIntent): value is CommandRequest {
  return record(value)
    && exact(value, ["command_id", "workspace_id", "command_type", "actor", "target_type", "target_id", "expected_version", "idempotency_key", "correlation_id", "payload"])
    && uuid(value.command_id) && uuid(value.correlation_id) && nonblank(value.idempotency_key)
    && value.workspace_id === scope.workspaceId && value.target_id === scope.rootTaskId
    && value.command_type === "AcknowledgeAttentionItem" && value.target_type === "task"
    && value.expected_version === intent.rootTaskVersion
    && record(value.actor) && exact(value.actor, ["type", "id"])
    && value.actor.type === "user" && value.actor.id === scope.principalId
    && samePayload(value.payload, nativeAttentionPayload(intent));
}

function validIntent(value: unknown): value is NativeAttentionIntent {
  return record(value) && exact(value, ["rootTaskVersion", "attentionKey", "sourceCorrelationId", "sourceResourceVersion"])
    && positiveInteger(value.rootTaskVersion) && nonblank(value.attentionKey)
    && uuid(value.sourceCorrelationId) && positiveInteger(value.sourceResourceVersion);
}

function validScope(value: unknown): value is NativeAttentionScope {
  return record(value) && exact(value, ["workspaceId", "principalId", "rootTaskId"])
    && uuid(value.workspaceId) && uuid(value.rootTaskId) && nonblank(value.principalId);
}

function samePayload(left: unknown, right: unknown): boolean {
  return record(left) && record(right)
    && exact(left, ["attention_key", "source_correlation_id", "source_resource_version"])
    && exact(right, ["attention_key", "source_correlation_id", "source_resource_version"])
    && nonblank(left.attention_key) && uuid(left.source_correlation_id) && positiveInteger(left.source_resource_version)
    && left.attention_key === right.attention_key && left.source_correlation_id === right.source_correlation_id
    && left.source_resource_version === right.source_resource_version;
}

function freeze<T>(value: T): T {
  if (typeof value !== "object" || value === null || Object.isFrozen(value)) return value;
  for (const child of Object.values(value)) freeze(child);
  return Object.freeze(value);
}

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function exact(value: Record<string, unknown>, keys: readonly string[]): boolean {
  return Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
}
function nonblank(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}
function uuid(value: unknown): value is string {
  return typeof value === "string" && UUID.test(value);
}
function positiveInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0;
}
