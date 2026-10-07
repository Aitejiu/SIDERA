import type { AssigneeValue, CommandRequest } from "@/features/assignments/types";
import { assignmentTargetKey } from "@/features/assignments/api";
import type { ProposalReviewBinding } from "./ProposalActions";
import type { RevisionReviewBinding } from "./nativeRevisionReview";

export interface CreatePlanningDraftScope {
  kind: "create";
  workspace_id: string;
  request_id: string;
  principal_id: string;
}

export interface RevisionPlanningDraftScope {
  kind: "revision";
  workspace_id: string;
  request_id: string;
  principal_id: string;
  root_task_id: string;
}

export type PlanningDraftScope = CreatePlanningDraftScope | RevisionPlanningDraftScope;

interface PlanningDraftFields<Review> {
  schema_version: 1;
  review: Review | null;
  choices: readonly (readonly [string, AssigneeValue])[];
  original: CommandRequest | null;
  status: "idle" | "pending" | "unknown" | "applied" | "rejected";
}

export type PlanningConfirmationDraft =
  | (CreatePlanningDraftScope & PlanningDraftFields<ProposalReviewBinding>)
  | (RevisionPlanningDraftScope & PlanningDraftFields<RevisionReviewBinding>);

type DraftResult =
  | { ok: true; draft: PlanningConfirmationDraft | null }
  | { ok: false; reason: string };

const STORAGE_PREFIX = "sidera.native-planning.confirmation.v1";
const INVALID_DRAFT = "本地确认草稿不可用";
const WRITE_FAILED = "无法保存本地确认草稿";
const COMMAND_TYPES = new Set([
  "ConfirmPlanningProposal",
  "CancelPlanningRequest",
  "AnswerPlanningClarification",
  "RequestProposalRevision",
  "RejectPlanningProposal",
]);
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function planningDraftKey(scope: PlanningDraftScope): string {
  const ordinaryKey = `${STORAGE_PREFIX}:${encodeURIComponent(scope.workspace_id)}:${encodeURIComponent(scope.request_id)}:${encodeURIComponent(scope.principal_id)}`;
  return scope.kind === "revision"
    ? `${ordinaryKey}:revision:${encodeURIComponent(scope.root_task_id)}`
    : ordinaryKey;
}

export function loadPlanningDraft(scope: PlanningDraftScope): DraftResult {
  if (!validScope(scope)) return { ok: false, reason: INVALID_DRAFT };
  try {
    const stored = window.localStorage.getItem(planningDraftKey(scope));
    if (stored === null) return { ok: true, draft: null };
    const parsed: unknown = JSON.parse(stored);
    if (!isRecord(parsed) || !validDraft(scope, parsed)) return { ok: false, reason: INVALID_DRAFT };
    return { ok: true, draft: deepFreeze(copyDraft(parsed as unknown as PlanningConfirmationDraft)) };
  } catch {
    return { ok: false, reason: INVALID_DRAFT };
  }
}

export function savePlanningDraft(
  scope: PlanningDraftScope,
  draft: PlanningConfirmationDraft,
): { ok: true } | { ok: false; reason: string } {
  if (!validScope(scope) || !isRecord(draft) || !validDraft(scope, draft)) {
    return { ok: false, reason: INVALID_DRAFT };
  }
  try {
    window.localStorage.setItem(planningDraftKey(scope), JSON.stringify(copyDraft(draft)));
    return { ok: true };
  } catch {
    return { ok: false, reason: WRITE_FAILED };
  }
}

function validDraft(scope: PlanningDraftScope, value: Record<string, unknown>): boolean {
  return scope.kind === "create"
    ? validCreateDraft(scope, value)
    : validRevisionDraft(scope, value);
}

function validCreateDraft(scope: CreatePlanningDraftScope, value: Record<string, unknown>): boolean {
  if (!hasExactKeys(value, ["kind", "workspace_id", "request_id", "principal_id", "schema_version", "review", "choices", "original", "status"])
    || value.kind !== "create" || value.workspace_id !== scope.workspace_id
    || value.request_id !== scope.request_id || value.principal_id !== scope.principal_id
    || value.schema_version !== 1 || !validCreateReview(value.review)
    || !Array.isArray(value.choices)
    || !["idle", "pending", "unknown", "applied", "rejected"].includes(String(value.status))) return false;

  const choiceKeys = new Set<string>();
  for (const pair of value.choices) {
    if (!Array.isArray(pair) || pair.length !== 2 || typeof pair[0] !== "string"
      || !canonicalTargetKey(pair[0]) || !validAssignee(pair[1], scope.workspace_id)
      || choiceKeys.has(pair[0])) return false;
    choiceKeys.add(pair[0]);
  }

  if (value.original === null) return value.status === "idle";
  if (!validOriginal(scope, value.review, value.original, value.status, value.choices)) return false;
  return value.status !== "idle";
}

function validRevisionDraft(scope: RevisionPlanningDraftScope, value: Record<string, unknown>): boolean {
  if (!hasExactKeys(value, ["kind", "workspace_id", "request_id", "principal_id", "root_task_id", "schema_version", "review", "choices", "original", "status"])
    || value.kind !== "revision" || value.workspace_id !== scope.workspace_id
    || value.request_id !== scope.request_id || value.principal_id !== scope.principal_id
    || value.root_task_id !== scope.root_task_id || value.schema_version !== 1
    || !validRevisionReview(value.review, scope)
    || !Array.isArray(value.choices)
    || !["idle", "pending", "unknown", "applied", "rejected"].includes(String(value.status))) return false;

  const targetPins = value.review === null ? null : readTargetFingerprint(value.review.targetsFingerprint);
  if (value.review !== null && targetPins === null) return false;
  if (value.review === null && value.choices.length !== 0) return false;
  const choiceKeys = new Set<string>();
  for (const pair of value.choices) {
    if (!Array.isArray(pair) || pair.length !== 2 || typeof pair[0] !== "string"
      || !canonicalTargetKey(pair[0]) || !validAssignee(pair[1], scope.workspace_id)
      || choiceKeys.has(pair[0])) return false;
    if (targetPins !== null && !targetPins.some((target) => target.strategy !== "reuse"
      && assignmentTargetKey(target.kind, target.key) === pair[0])) return false;
    choiceKeys.add(pair[0]);
  }

  if (value.original === null) return value.status === "idle";
  if (!validOriginal(scope, value.review, value.original, value.status, value.choices)) return false;
  return value.status !== "idle";
}

function validCreateReview(value: unknown): value is ProposalReviewBinding | null {
  if (value === null) return true;
  return isRecord(value)
    && hasExactKeys(value, ["requestVersion", "proposalRevision", "proposalContentHash", "profileId", "profileVersion"])
    && positiveInteger(value.requestVersion)
    && positiveInteger(value.proposalRevision)
    && nonblank(value.proposalContentHash)
    && nonblank(value.profileId)
    && positiveInteger(value.profileVersion);
}

function validAssignee(value: unknown, workspaceId: string): value is AssigneeValue {
  if (!isRecord(value) || !hasExactKeys(value, ["kind", "id", "profileRef"]) || !nonblank(value.id)) return false;
  if (value.kind === "human") return value.profileRef === null;
  if (value.kind !== "profile" || !isRecord(value.profileRef)
    || !hasExactKeys(value.profileRef, ["workspace_id", "profile_id", "revision"])) return false;
  return value.profileRef.workspace_id === workspaceId
    && value.profileRef.profile_id === value.id
    && positiveInteger(value.profileRef.revision);
}

function validOriginal(
  scope: PlanningDraftScope,
  review: unknown,
  value: unknown,
  status: unknown,
  choices?: unknown,
): value is CommandRequest {
  if (!isRecord(value) || !hasExactKeys(value, [
    "actor", "command_id", "command_type", "correlation_id", "expected_version",
    "idempotency_key", "payload", "target_id", "target_type", "workspace_id",
  ])) return false;
  if (!UUID.test(String(value.command_id)) || !UUID.test(String(value.correlation_id))
    || !UUID.test(String(value.idempotency_key))
    || value.workspace_id !== scope.workspace_id || value.target_type !== "planning_request"
    || value.target_id !== scope.request_id || !positiveInteger(value.expected_version)
    || !isRecord(value.actor) || !hasExactKeys(value.actor, ["type", "id"])
    || value.actor.type !== "user" || value.actor.id !== scope.principal_id
    || !isRecord(value.payload)) return false;

  if (scope.kind === "revision") {
    return value.command_type === "ConfirmPlanRevision"
      && validRevisionPayload(scope, review, value.expected_version, value.payload, status, choices);
  }
  if (!COMMAND_TYPES.has(String(value.command_type))) return false;

  switch (value.command_type) {
    case "ConfirmPlanningProposal":
      return validConfirmPayload(review, value.expected_version, value.payload, status);
    case "CancelPlanningRequest":
      return hasExactKeys(value.payload, []);
    case "AnswerPlanningClarification":
      return validAnswers(value.payload);
    case "RequestProposalRevision":
      return validFeedback(value.payload);
    case "RejectPlanningProposal":
      return validRejection(value.payload);
    default:
      return false;
  }
}

function validConfirmPayload(
  review: unknown,
  expectedVersion: unknown,
  payload: Record<string, unknown>,
  status: unknown,
): boolean {
  if (!isRecord(review) || !validCreateReview(review) || review === null
    || !positiveInteger(expectedVersion)
    || !hasExactKeys(payload, ["proposal_revision", "proposal_content_hash", "profile_id", "profile_version", "assignee_bindings"])
    || !positiveInteger(payload.proposal_revision)
    || !nonblank(payload.proposal_content_hash)
    || !nonblank(payload.profile_id) || !positiveInteger(payload.profile_version)
    || !Array.isArray(payload.assignee_bindings) || payload.assignee_bindings.length === 0) return false;
  if ((status === "pending" || status === "unknown")
    && (review.requestVersion !== expectedVersion
      || payload.proposal_revision !== review.proposalRevision
      || payload.proposal_content_hash !== review.proposalContentHash
      || payload.profile_id !== review.profileId
      || payload.profile_version !== review.profileVersion)) return false;

  let previous = "";
  const targets = new Set<string>();
  for (const raw of payload.assignee_bindings) {
    if (!isRecord(raw) || !hasExactKeys(raw, [
      "target_type", "target_key", "assignee_kind", "assignee_id", "profile_revision", "expected_binding_version",
    ])) return false;
    if ((raw.target_type !== "subtask" && raw.target_type !== "worker_item")
      || !nonblank(raw.target_key) || raw.target_key.trim() !== raw.target_key
      || (raw.assignee_kind !== "human" && raw.assignee_kind !== "profile")
      || !nonblank(raw.assignee_id) || raw.assignee_id.trim() !== raw.assignee_id
      || raw.expected_binding_version !== null) return false;
    if (raw.assignee_kind === "human" ? raw.profile_revision !== null : !positiveInteger(raw.profile_revision)) return false;
    const canonical = assignmentTargetKey(raw.target_type, raw.target_key);
    if (targets.has(canonical)) return false;
    targets.add(canonical);
    const order = `${raw.target_type === "subtask" ? "0" : "1"}:${raw.target_key}`;
    if (previous !== "" && previous >= order) return false;
    previous = order;
  }
  return true;
}

function validRevisionReview(
  value: unknown,
  scope: RevisionPlanningDraftScope,
): value is RevisionReviewBinding | null {
  if (value === null) return true;
  if (!isRecord(value) || !hasExactKeys(value, [
    "requestVersion", "proposalRevision", "proposalContentHash", "profileId", "profileVersion",
    "rootTaskId", "diffHash", "baseSnapshotId", "baseContentHash", "baseGeneration",
    "baseRootTaskVersion", "targetsFingerprint", "reusedBindingsFingerprint",
  ]) || !positiveInteger(value.requestVersion) || !positiveInteger(value.proposalRevision)
    || !nonblank(value.proposalContentHash) || !nonblank(value.profileId)
    || !positiveInteger(value.profileVersion) || value.rootTaskId !== scope.root_task_id
    || !nonblank(value.diffHash) || !UUID.test(String(value.baseSnapshotId))
    || !nonblank(value.baseContentHash) || !positiveInteger(value.baseGeneration)
    || !positiveInteger(value.baseRootTaskVersion) || typeof value.targetsFingerprint !== "string"
    || typeof value.reusedBindingsFingerprint !== "string") return false;

  const targets = readTargetFingerprint(value.targetsFingerprint);
  const reusePins = readReuseFingerprint(value.reusedBindingsFingerprint, scope.workspace_id);
  if (targets === null || reusePins === null) return false;
  const reuseTargets = targets.filter((target) => target.strategy === "reuse");
  return reuseTargets.length === reusePins.length && reuseTargets.every((target) => reusePins.some((pin) =>
    pin.kind === target.kind && pin.targetId === target.existing_id));
}

interface TargetFingerprintRecord {
  kind: "subtask" | "worker_item";
  key: string;
  parent: string | null;
  strategy: "create" | "derive" | "reuse";
  existing_id: string | null;
}

interface ReuseFingerprintRecord {
  kind: "subtask" | "worker_item";
  targetId: string;
  version: number;
}

function readTargetFingerprint(value: unknown): TargetFingerprintRecord[] | null {
  if (typeof value !== "string") return null;
  let parsed: unknown;
  try { parsed = JSON.parse(value); } catch { return null; }
  if (!Array.isArray(parsed) || canonicalRecords(parsed) !== value) return null;
  const targets: TargetFingerprintRecord[] = [];
  const keys = new Set<string>();
  const subtaskKeys = new Set<string>();
  for (const raw of parsed) {
    if (!isRecord(raw) || !hasExactKeys(raw, ["kind", "key", "parent", "identity_strategy", "existing_id"])
      || (raw.kind !== "subtask" && raw.kind !== "worker_item") || !nonblank(raw.key)
      || raw.key.trim() !== raw.key
      || (raw.identity_strategy !== "create" && raw.identity_strategy !== "derive" && raw.identity_strategy !== "reuse")
      || !(raw.existing_id === null || nonblank(raw.existing_id) && raw.existing_id.trim() === raw.existing_id)) return null;
    if ((raw.identity_strategy === "reuse") !== (raw.existing_id !== null)) return null;
    if (raw.kind === "subtask") {
      if (raw.parent !== null || subtaskKeys.has(raw.key)) return null;
      subtaskKeys.add(raw.key);
    } else if (!nonblank(raw.parent) || raw.parent.trim() !== raw.parent) return null;
    const targetKey = assignmentTargetKey(raw.kind, raw.key);
    if (keys.has(targetKey)) return null;
    keys.add(targetKey);
    targets.push({
      kind: raw.kind,
      key: raw.key,
      parent: raw.parent as string | null,
      strategy: raw.identity_strategy,
      existing_id: raw.existing_id as string | null,
    });
  }
  if (targets.some((target) => target.kind === "worker_item" && !subtaskKeys.has(target.parent!))) return null;
  return targets;
}

function readReuseFingerprint(value: unknown, workspaceId: string): ReuseFingerprintRecord[] | null {
  if (typeof value !== "string") return null;
  let parsed: unknown;
  try { parsed = JSON.parse(value); } catch { return null; }
  if (!Array.isArray(parsed) || canonicalRecords(parsed) !== value) return null;
  const pins: ReuseFingerprintRecord[] = [];
  const targets = new Set<string>();
  for (const raw of parsed) {
    if (!isRecord(raw) || !hasExactKeys(raw, ["kind", "targetId", "version", "assignee", "profile_ref"])
      || (raw.kind !== "subtask" && raw.kind !== "worker_item") || !nonblank(raw.targetId)
      || !positiveInteger(raw.version) || !isRecord(raw.assignee)
      || !hasExactKeys(raw.assignee, ["kind", "id"]) || !nonblank(raw.assignee.id)) return null;
    if (raw.assignee.kind === "human") {
      if (raw.profile_ref !== null) return null;
    } else if (raw.assignee.kind === "profile") {
      if (!isRecord(raw.profile_ref) || !hasExactKeys(raw.profile_ref, ["workspace_id", "profile_id", "revision"])
        || raw.profile_ref.workspace_id !== workspaceId || raw.profile_ref.profile_id !== raw.assignee.id
        || !positiveInteger(raw.profile_ref.revision)) return null;
    } else return null;
    const key = assignmentTargetKey(raw.kind, raw.targetId);
    if (targets.has(key)) return null;
    targets.add(key);
    pins.push({ kind: raw.kind, targetId: raw.targetId, version: raw.version });
  }
  return pins;
}

function validRevisionPayload(
  scope: RevisionPlanningDraftScope,
  review: unknown,
  expectedVersion: unknown,
  payload: Record<string, unknown>,
  status: unknown,
  choices: unknown,
): boolean {
  const bindings = payload.assignee_bindings;
  if (!isRecord(review) || !validRevisionReview(review, scope) || review === null
    || !positiveInteger(expectedVersion)
    || !hasExactKeys(payload, [
      "expected_proposal_revision", "expected_proposal_content_hash", "expected_diff_hash",
      "expected_base_snapshot_id", "expected_base_content_hash", "profile_id", "profile_version",
      "assignee_bindings",
    ])
    || !positiveInteger(payload.expected_proposal_revision)
    || !nonblank(payload.expected_proposal_content_hash) || !nonblank(payload.expected_diff_hash)
    || !UUID.test(String(payload.expected_base_snapshot_id))
    || !nonblank(payload.expected_base_content_hash) || !nonblank(payload.profile_id)
    || !positiveInteger(payload.profile_version) || !Array.isArray(bindings)) return false;

  if ((status === "pending" || status === "unknown")
    && (review.requestVersion !== expectedVersion
      || payload.expected_proposal_revision !== review.proposalRevision
      || payload.expected_proposal_content_hash !== review.proposalContentHash
      || payload.expected_diff_hash !== review.diffHash
      || payload.expected_base_snapshot_id !== review.baseSnapshotId
      || payload.expected_base_content_hash !== review.baseContentHash
      || payload.profile_id !== review.profileId
      || payload.profile_version !== review.profileVersion)) return false;

  let previous = "";
  const targetKeys = new Set<string>();
  for (const raw of bindings) {
    if (!isRecord(raw) || !hasExactKeys(raw, [
      "target_type", "target_key", "assignee_kind", "assignee_id", "profile_revision", "expected_binding_version",
    ]) || (raw.target_type !== "subtask" && raw.target_type !== "worker_item")
      || !nonblank(raw.target_key) || raw.target_key.trim() !== raw.target_key
      || (raw.assignee_kind !== "human" && raw.assignee_kind !== "profile")
      || !nonblank(raw.assignee_id) || raw.assignee_id.trim() !== raw.assignee_id
      || raw.expected_binding_version !== null
      || (raw.assignee_kind === "human" ? raw.profile_revision !== null : !positiveInteger(raw.profile_revision))) return false;
    const targetKey = assignmentTargetKey(raw.target_type, raw.target_key);
    const order = `${raw.target_type === "subtask" ? "0" : "1"}:${raw.target_key}`;
    if (targetKeys.has(targetKey) || (previous !== "" && previous >= order)) return false;
    targetKeys.add(targetKey);
    previous = order;
  }

  if (status !== "pending" && status !== "unknown") return true;
  const targets = readTargetFingerprint(review.targetsFingerprint);
  if (targets === null) return false;
  const expectedNewTargets = targets.filter((target) => target.strategy !== "reuse").sort((left, right) => {
    const leftRank = left.kind === "subtask" ? 0 : 1;
    const rightRank = right.kind === "subtask" ? 0 : 1;
    return leftRank - rightRank
      || (left.key < right.key ? -1 : left.key > right.key ? 1 : 0);
  });
  if (expectedNewTargets.length !== bindings.length) return false;
  return expectedNewTargets.every((target, index) => {
    const raw = bindings[index];
    if (!isRecord(raw) || raw.target_type !== target.kind || raw.target_key !== target.key) return false;
    if (!Array.isArray(choices)) return false;
    const choicePair = choices.find((pair) => Array.isArray(pair) && pair[0] === assignmentTargetKey(target.kind, target.key));
    if (!Array.isArray(choicePair) || !isRecord(choicePair[1])) return false;
    const choice = choicePair[1];
    return choice.kind === raw.assignee_kind && choice.id === raw.assignee_id
      && (choice.kind === "human"
        ? choice.profileRef === null && raw.profile_revision === null
        : isRecord(choice.profileRef) && choice.profileRef.workspace_id === scope.workspace_id
          && choice.profileRef.profile_id === choice.id
          && choice.profileRef.revision === raw.profile_revision);
  });
}

function canonicalRecords(records: unknown[]): string | null {
  if (!records.every(isRecord)) return null;
  const normalized = records.map((record) => canonicalValue(record));
  normalized.sort((left, right) => JSON.stringify(left).localeCompare(JSON.stringify(right)));
  return JSON.stringify(normalized);
}

function canonicalValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalValue);
  if (!isRecord(value)) return value;
  return Object.fromEntries(Object.entries(value)
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([key, child]) => [key, canonicalValue(child)]));
}

function validAnswers(payload: Record<string, unknown>): boolean {
  if (!hasExactKeys(payload, ["answers"]) || !Array.isArray(payload.answers) || payload.answers.length === 0) return false;
  const ids = new Set<string>();
  return payload.answers.every((answer) => {
    if (!isRecord(answer) || !hasExactKeys(answer, ["clarification_id", "answer"])
      || !UUID.test(String(answer.clarification_id)) || !nonblank(answer.answer)) return false;
    if (ids.has(String(answer.clarification_id))) return false;
    ids.add(String(answer.clarification_id));
    return true;
  });
}

function validFeedback(payload: Record<string, unknown>): boolean {
  return hasExactKeys(payload, ["current_proposal_revision", "current_proposal_content_hash", "feedback"])
    && positiveInteger(payload.current_proposal_revision)
    && nonblank(payload.current_proposal_content_hash)
    && nonblank(payload.feedback);
}

function validRejection(payload: Record<string, unknown>): boolean {
  return hasExactKeys(payload, ["current_proposal_revision", "current_proposal_content_hash", "reason"])
    && positiveInteger(payload.current_proposal_revision)
    && nonblank(payload.current_proposal_content_hash)
    && nonblank(payload.reason);
}

function validScope(value: unknown): value is PlanningDraftScope {
  if (!isRecord(value) || !nonblank(value.workspace_id) || !nonblank(value.request_id)
    || !nonblank(value.principal_id)) return false;
  if (value.kind === "create") {
    return hasExactKeys(value, ["kind", "workspace_id", "request_id", "principal_id"]);
  }
  return value.kind === "revision"
    && hasExactKeys(value, ["kind", "workspace_id", "request_id", "principal_id", "root_task_id"])
    && UUID.test(String(value.root_task_id));
}

function canonicalTargetKey(value: string): boolean {
  try {
    const pair: unknown = JSON.parse(value);
    return Array.isArray(pair) && pair.length === 2
      && (pair[0] === "subtask" || pair[0] === "worker_item")
      && nonblank(pair[1]) && pair[1].trim() === pair[1]
      && value === JSON.stringify(pair);
  } catch {
    return false;
  }
}

function copyDraft(value: PlanningConfirmationDraft): PlanningConfirmationDraft {
  return JSON.parse(JSON.stringify(value)) as PlanningConfirmationDraft;
}

function deepFreeze<T>(value: T): T {
  if (typeof value !== "object" || value === null || Object.isFrozen(value)) return value;
  for (const child of Object.values(value)) deepFreeze(child);
  return Object.freeze(value);
}

function hasExactKeys(value: Record<string, unknown>, expected: readonly string[]): boolean {
  const actual = Object.keys(value).sort();
  const sortedExpected = [...expected].sort();
  return actual.length === sortedExpected.length
    && actual.every((key, index) => key === sortedExpected[index]);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function nonblank(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function positiveInteger(value: unknown): value is number {
  return Number.isInteger(value) && (value as number) > 0;
}
