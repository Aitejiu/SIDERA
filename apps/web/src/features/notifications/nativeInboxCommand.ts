import type { CommandRequest } from "../assignments/types";
import { buildNativeCommand } from "../assignments/commands";
import type { LocalSession } from "../local-session/api";

export interface NativeInboxScope { workspaceId: string; principalId: string }
type ThreadIntent = { threadId: string; threadVersion: number; latestFactId: string };
export type NativeInboxIntent = ThreadIntent & (
  | { kind: "mark_read" }
  | { kind: "archive" }
  | { kind: "requeue"; deliveryId: string; routeId: string; routeVersion: number; reason: string }
);
export type NativeInboxOutcome =
  | { kind: "mark_read"; threadId: string; threadVersion: number; latestFactId: string }
  | { kind: "archive"; threadId: string; threadVersion: number; organizationStatus: "archived" }
  | { kind: "requeue"; deliveryId: string; previousDeliveryId: string; routeVersion: number;
      deliveryRound: number; fencingToken: number; deliveryStatus: "pending" };
export type NativeInboxStatus = "idle" | "pending" | "unknown" | "applied" | "rejected";
export type NativeInboxRejection = "operator_required" | "version_conflict" | "invalid_transition" | "validation_error" | "command_rejected";
export interface NativeInboxDraft {
  schema_version: 1;
  scope: NativeInboxScope;
  intent: NativeInboxIntent | null;
  original: CommandRequest | null;
  status: NativeInboxStatus;
  outcome: NativeInboxOutcome | null;
  rejection_code: NativeInboxRejection | null;
}
export type NativeInboxStorageError = "unavailable" | "invalid";
export type NativeInboxLoad = { ok: true; draft: NativeInboxDraft } | { ok: false; error: NativeInboxStorageError };
export type NativeInboxSave = { ok: true } | { ok: false; error: NativeInboxStorageError };

export function emptyNativeInboxDraft(scope: NativeInboxScope): NativeInboxDraft {
  return freeze({ schema_version: 1, scope: { ...scope }, intent: null, original: null, status: "idle", outcome: null, rejection_code: null });
}
export function nativeInboxDraftKey(scope: NativeInboxScope) {
  return `sidera.native-inbox.v1:${encodeURIComponent(scope.workspaceId)}:${encodeURIComponent(scope.principalId)}`;
}
export function freezeNativeInboxIntent(value: unknown): NativeInboxIntent | null {
  if (!record(value) || typeof value.kind !== "string" || !uuid(value.threadId)
    || !positive(value.threadVersion) || !uuid(value.latestFactId)) return null;
  const base = { threadId: value.threadId, threadVersion: value.threadVersion, latestFactId: value.latestFactId };
  if ((value.kind === "mark_read" || value.kind === "archive")
    && keys(value, ["kind", "threadId", "threadVersion", "latestFactId"])) {
    return freeze({ ...base, kind: value.kind });
  }
  if (value.kind !== "requeue" || !keys(value, ["kind", "threadId", "threadVersion", "latestFactId", "deliveryId", "routeId", "routeVersion", "reason"])
    || !uuid(value.deliveryId) || !uuid(value.routeId) || !positive(value.routeVersion) || typeof value.reason !== "string") return null;
  const reason = value.reason.trim();
  if (!reason || reason.length > 256 || Array.from(reason).some(character => character.charCodeAt(0) < 32 || character.charCodeAt(0) === 127)
    || /api[_ -]?key|authorization|bearer|password|secret|token/i.test(reason)) return null;
  return freeze({ ...base, kind: "requeue", deliveryId: value.deliveryId, routeId: value.routeId, routeVersion: value.routeVersion, reason });
}
export function buildNativeInboxOriginal(scope: NativeInboxScope, intent: NativeInboxIntent, session: LocalSession): CommandRequest {
  const captured = freezeNativeInboxIntent(intent);
  if (!validScope(scope) || !captured || session.principal_id !== scope.principalId) throw new Error("Inbox command unavailable");
  return buildNativeCommand({
    session, workspaceId: scope.workspaceId,
    commandType: commandType(captured), targetType: captured.kind === "requeue" ? "notification_delivery" : "notification_thread",
    targetId: captured.kind === "requeue" ? captured.deliveryId : captured.threadId,
    expectedVersion: captured.kind === "requeue" ? null : captured.threadVersion, payload: payload(captured),
  });
}
export function reviewNativeInboxResult(draft: NativeInboxDraft, result: unknown): NativeInboxDraft {
  if (!draft.original || !draft.intent) return draft;
  const correlated = record(result) && result.command_id === draft.original.command_id;
  return freeze({ ...draft, status: correlated ? "applied" : "unknown",
    outcome: correlated ? mapOutcome(draft.intent, result) : null, rejection_code: null });
}
export function reviewNativeInboxJournal(draft: NativeInboxDraft, journal: unknown): NativeInboxDraft | null {
  const original = draft.original;
  if (!original || !draft.intent || !record(journal) || journal.command_id !== original.command_id
    || typeof journal.status !== "string" || !["pending", "applied", "rejected"].includes(journal.status)) return null;
  for (const key of ["command_type", "target_type", "target_id", "expected_version"] as const) {
    if (journal[key] !== undefined && journal[key] !== original[key]) return null;
  }
  if (journal.payload !== undefined && journal.payload !== null && !matchesPayload(journal.payload, draft.intent)) return null;
  if (journal.result !== undefined && journal.result !== null
    && (!record(journal.result) || journal.result.command_id !== original.command_id)) return null;
  if (journal.status === "applied") return freeze({ ...draft, status: "applied", outcome: mapOutcome(draft.intent, journal.result), rejection_code: null });
  if (journal.status === "rejected") return freeze({ ...draft, status: "rejected", outcome: null,
    rejection_code: rejection(journal.error_code) ? journal.error_code : "command_rejected" });
  return freeze({ ...draft, status: "pending", outcome: null, rejection_code: null });
}
export function unresolvedNativeInboxDraft(draft: NativeInboxDraft) { return draft.status === "pending" || draft.status === "unknown" || (draft.status === "applied" && draft.outcome === null); }
export function loadNativeInboxDraft(scope: NativeInboxScope): NativeInboxLoad {
  try {
    if (!validScope(scope)) return { ok: false, error: "invalid" };
    const serialized = localStorage.getItem(nativeInboxDraftKey(scope));
    if (serialized === null) return { ok: true, draft: emptyNativeInboxDraft(scope) };
    const value: unknown = JSON.parse(serialized);
    return validDraft(value, scope) ? { ok: true, draft: freeze(value) } : { ok: false, error: "invalid" };
  } catch { return { ok: false, error: "unavailable" }; }
}
export function saveNativeInboxDraft(scope: NativeInboxScope, draft: NativeInboxDraft): NativeInboxSave {
  try {
    if (!validDraft(draft, scope)) return { ok: false, error: "invalid" };
    localStorage.setItem(nativeInboxDraftKey(scope), JSON.stringify(draft));
    return { ok: true };
  } catch { return { ok: false, error: "unavailable" }; }
}

function mapOutcome(intent: NativeInboxIntent, result: unknown): NativeInboxOutcome | null {
  if (!record(result) || !record(result.data) || !Array.isArray(result.event_ids) || !result.event_ids.every(uuid)) return null;
  const data = result.data;
  if (intent.kind === "requeue") {
    if (!uuid(result.aggregate_id) || result.aggregate_id === intent.deliveryId || data.notification_delivery_id !== result.aggregate_id
      || data.previous_delivery_id !== intent.deliveryId || data.route_version !== intent.routeVersion || data.delivery_status !== "pending"
      || !positive(data.fencing_token) || result.aggregate_version !== data.fencing_token || !positive(data.delivery_round)) return null;
    return { kind: "requeue", deliveryId: result.aggregate_id, previousDeliveryId: intent.deliveryId, routeVersion: intent.routeVersion,
      deliveryRound: data.delivery_round, fencingToken: data.fencing_token, deliveryStatus: "pending" };
  }
  if (result.aggregate_id !== intent.threadId || data.notification_thread_id !== intent.threadId
    || !threadVersion(result.aggregate_version, intent) || data.thread_version !== result.aggregate_version) return null;
  if (intent.kind === "mark_read") return data.latest_fact_id === intent.latestFactId
    ? { kind: "mark_read", threadId: intent.threadId, threadVersion: result.aggregate_version, latestFactId: intent.latestFactId } : null;
  return data.organization_status === "archived"
    ? { kind: "archive", threadId: intent.threadId, threadVersion: result.aggregate_version, organizationStatus: "archived" } : null;
}

function validDraft(value: unknown, scope: NativeInboxScope): value is NativeInboxDraft {
  if (!validScope(scope) || !record(value) || !keys(value, ["schema_version", "scope", "intent", "original", "status", "outcome", "rejection_code"])
    || value.schema_version !== 1 || !validScope(value.scope) || value.scope.workspaceId !== scope.workspaceId || value.scope.principalId !== scope.principalId
    || typeof value.status !== "string" || !["idle", "pending", "unknown", "applied", "rejected"].includes(value.status)) return false;
  if (value.status === "idle") return value.intent === null && value.original === null && value.outcome === null && value.rejection_code === null;
  const intent = freezeNativeInboxIntent(value.intent);
  if (!intent || (intent.kind === "requeue" && record(value.intent) && value.intent.reason !== intent.reason)
    || !validOriginal(value.original, scope, intent)) return false;
  if (value.status === "pending" || value.status === "unknown") return value.outcome === null && value.rejection_code === null;
  if (value.status === "rejected") return value.outcome === null && rejection(value.rejection_code);
  return value.rejection_code === null && (value.outcome === null || validOutcome(value.outcome, intent));
}
function validOriginal(value: unknown, scope: NativeInboxScope, intent: NativeInboxIntent): boolean {
  return record(value) && keys(value, ["command_id", "workspace_id", "command_type", "actor", "target_type", "target_id", "expected_version", "idempotency_key", "correlation_id", "payload"])
    && uuid(value.command_id) && uuid(value.idempotency_key) && uuid(value.correlation_id) && value.workspace_id === scope.workspaceId
    && record(value.actor) && keys(value.actor, ["type", "id"]) && value.actor.type === "user" && value.actor.id === scope.principalId
    && value.command_type === commandType(intent) && value.target_type === (intent.kind === "requeue" ? "notification_delivery" : "notification_thread")
    && value.target_id === (intent.kind === "requeue" ? intent.deliveryId : intent.threadId)
    && value.expected_version === (intent.kind === "requeue" ? null : intent.threadVersion) && matchesPayload(value.payload, intent);
}
function validOutcome(value: unknown, intent: NativeInboxIntent): boolean {
  if (!record(value) || typeof value.kind !== "string" || value.kind !== intent.kind) return false;
  if (intent.kind === "requeue") return keys(value, ["kind", "deliveryId", "previousDeliveryId", "routeVersion", "deliveryRound", "fencingToken", "deliveryStatus"])
    && uuid(value.deliveryId) && value.deliveryId !== intent.deliveryId && value.previousDeliveryId === intent.deliveryId
    && value.routeVersion === intent.routeVersion && positive(value.deliveryRound) && positive(value.fencingToken) && value.deliveryStatus === "pending";
  return value.threadId === intent.threadId && threadVersion(value.threadVersion, intent)
    && (intent.kind === "mark_read"
      ? keys(value, ["kind", "threadId", "threadVersion", "latestFactId"]) && value.latestFactId === intent.latestFactId
      : keys(value, ["kind", "threadId", "threadVersion", "organizationStatus"]) && value.organizationStatus === "archived");
}
function commandType(intent: NativeInboxIntent) {
  return intent.kind === "mark_read" ? "MarkNotificationThreadRead" : intent.kind === "archive" ? "ArchiveNotificationThread" : "RequeueNotificationDelivery";
}
function payload(intent: NativeInboxIntent): Record<string, unknown> {
  return intent.kind === "mark_read" ? { expected_latest_fact_id: intent.latestFactId } : intent.kind === "archive" ? {}
    : { expected_route_version: intent.routeVersion, expected_status: "exhausted", reason: intent.reason };
}
function matchesPayload(value: unknown, intent: NativeInboxIntent): boolean {
  const expected = payload(intent);
  return record(value) && keys(value, Object.keys(expected)) && Object.entries(expected).every(([key, item]) => value[key] === item);
}
function validScope(value: unknown): value is NativeInboxScope {
  return record(value) && keys(value, ["workspaceId", "principalId"]) && uuid(value.workspaceId)
    && typeof value.principalId === "string" && value.principalId.trim().length > 0 && value.principalId.length <= 256;
}
function threadVersion(value: unknown, intent: NativeInboxIntent): value is number {
  return positive(value) && (value === intent.threadVersion || value === intent.threadVersion + 1);
}
function rejection(value: unknown): value is NativeInboxRejection {
  return typeof value === "string" && ["operator_required", "version_conflict", "invalid_transition", "validation_error", "command_rejected"].includes(value);
}
function positive(value: unknown): value is number { return typeof value === "number" && Number.isSafeInteger(value) && value > 0; }
function uuid(value: unknown): value is string { return typeof value === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value); }
function record(value: unknown): value is Record<string, unknown> { return typeof value === "object" && value !== null && !Array.isArray(value); }
function keys(value: Record<string, unknown>, expected: string[]): boolean { return Object.keys(value).length === expected.length && expected.every(key => Object.hasOwn(value, key)); }
function freeze<T>(value: T): T {
  if (typeof value !== "object" || value === null || Object.isFrozen(value)) return value;
  for (const child of Object.values(value)) freeze(child);
  return Object.freeze(value);
}
