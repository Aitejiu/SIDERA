import type { CommandRequest } from "@/features/assignments/types";
import type { WorkContextDetail } from "@/features/context/types";
import type { PlanningDraftScope } from "./planningConfirmationDraft";
import type { PlanningRequest, PlanningRun, ReviseFailedPlanningRequestPayload } from "./types";

export interface FailedPlanningFeedbackBasis {
  scope: PlanningDraftScope;
  requestVersion: number;
  failedRunId: string;
  failedRunVersion: number;
  attempt: number;
  briefRevision: number;
  workContextId: string;
  workContextVersion: number;
  workContextRevisionId: string;
  workContextRevisionNumber: number;
  workContextContentHash: string;
}

export type FailedPlanningFeedbackReview =
  | { ok: true; basis: FailedPlanningFeedbackBasis }
  | { ok: false; reason: string };
export type FailedPlanningFeedbackStorageResult = { ok: true } | { ok: false; reason: string };
export type FailedPlanningFeedbackDraftResult =
  | { ok: true; feedback: string }
  | { ok: false; reason: string };

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const HASH = /^[0-9a-f]{64}$/;
const PREFIX = "sidera:failed-planning-feedback:v1";
const READ_ERROR = "无法读取补充要求草稿";
const WRITE_ERROR = "无法保存补充要求草稿";

export function reviewFailedPlanningFeedback({ scope, request, runs, context }: {
  scope: PlanningDraftScope;
  request: PlanningRequest;
  runs: readonly PlanningRun[] | undefined;
  context: WorkContextDetail | undefined;
}): FailedPlanningFeedbackReview {
  const unavailable = (reason: string): FailedPlanningFeedbackReview => ({ ok: false, reason });
  if (!validScope(scope) || request.workspace_id !== scope.workspace_id || request.id !== scope.request_id
    || request.kind !== scope.kind || request.status !== "failed"
    || !positive(request.version) || !positive(request.current_brief_revision)
    || !uuid(request.planning_profile_id) || !positive(request.planning_profile_version)
    || request.profile?.execution_mode !== "native_profile" || request.profile.status !== "active"
    || request.profile.id !== request.planning_profile_id || request.profile.workspace_id !== scope.workspace_id
    || request.profile.version !== request.planning_profile_version
    || !validRef(request.profile.planner_agent_ref, scope.workspace_id)
    || !uuid(request.current_run_id)) return unavailable("无法核实失败规划请求，请重新读取");
  if (scope.kind === "revision" && (request.revision_base?.workspace_id !== scope.workspace_id
    || request.revision_base.root_task_id !== scope.root_task_id)) return unavailable("计划调整的根任务不匹配");
  if (!Array.isArray(runs)) return unavailable("正在读取失败运行");
  const seen = new Set<string>();
  for (const run of runs) {
    if (!record(run) || !uuid(run.id) || !positive(run.version) || seen.has(run.id)
      || run.workspace_id !== scope.workspace_id || run.planning_request_id !== scope.request_id) {
      return unavailable("运行记录不匹配，请重新读取");
    }
    seen.add(run.id);
  }
  const run = runs.find((item) => item.id === request.current_run_id);
  if (run === undefined || run.execution_mode !== "native_profile" || run.status !== "failed"
    || run.result_kind !== "failure" || run.provider_ref !== "agent-run"
    || !nonblank(run.finished_at) || !Number.isFinite(Date.parse(run.finished_at))
    || !positive(run.attempt) || run.brief_revision !== request.current_brief_revision
    || !uuid(run.operation_id) || !sameRef(run.planner_agent_ref, request.profile.planner_agent_ref)) {
    return unavailable("无法核实当前失败运行，请重新读取");
  }
  const native = run.native_run;
  if (native?.status !== "closed" || native.closed !== true || !uuid(native.run_id)
    || !uuid(native.response_id) || !positive(native.event_cursor)
    || native.start_operation_id !== run.operation_id) return unavailable("失败运行的关闭状态尚未核实");
  if (request.active_run != null && !sameRun(request.active_run, run)) {
    return unavailable("失败运行快照已变化，请重新读取");
  }
  if (context === undefined) return unavailable("请重新读取需求上下文");
  const revision = context.current_revision;
  if (!uuid(context.id) || context.workspace_id !== scope.workspace_id || context.status !== "active"
    || !positive(context.version) || !uuid(context.current_revision_id) || !positive(context.current_revision_number)
    || revision?.id !== context.current_revision_id || revision.revision_number !== context.current_revision_number
    || revision.work_context_id !== context.id || revision.workspace_id !== scope.workspace_id
    || revision.brief_planning_request_id !== scope.request_id || revision.brief_revision !== request.current_brief_revision
    || typeof revision.content_hash !== "string" || !HASH.test(revision.content_hash)
    || (scope.kind === "create" ? context.planning_request_id !== scope.request_id
      : context.root_task_id !== scope.root_task_id)) return unavailable("需求上下文已变化，请重新核对");
  return { ok: true, basis: {
    scope: { ...scope }, requestVersion: request.version, failedRunId: run.id, failedRunVersion: run.version,
    attempt: run.attempt, briefRevision: run.brief_revision, workContextId: context.id,
    workContextVersion: context.version, workContextRevisionId: revision.id,
    workContextRevisionNumber: revision.revision_number, workContextContentHash: revision.content_hash,
  } };
}

export function failedPlanningFeedbackPayload(
  basis: FailedPlanningFeedbackBasis, feedback: string,
): ReviseFailedPlanningRequestPayload {
  if (!validBasis(basis) || typeof feedback !== "string" || feedback.trim().length === 0) {
    throw new Error("补充要求或规划依据不可用");
  }
  return {
    failed_planning_run_id: basis.failedRunId,
    expected_failed_planning_run_version: basis.failedRunVersion,
    expected_work_context_version: basis.workContextVersion,
    work_context_revision_id: basis.workContextRevisionId,
    work_context_content_hash: basis.workContextContentHash,
    feedback: feedback.trim(),
  };
}

export function failedPlanningFeedbackDraftKey(basis: FailedPlanningFeedbackBasis): string {
  return textKey(basis.scope, basis.failedRunId, basis.failedRunVersion);
}

export function loadFailedPlanningFeedbackDraft(basis: FailedPlanningFeedbackBasis): FailedPlanningFeedbackDraftResult {
  if (!validBasis(basis)) return { ok: false, reason: READ_ERROR };
  try {
    const stored = window.localStorage.getItem(failedPlanningFeedbackDraftKey(basis));
    if (stored === null) return { ok: true, feedback: "" };
    const value: unknown = JSON.parse(stored);
    if (!record(value) || !exactKeys(value, Object.keys(textValue(basis, "")))
      || typeof value.feedback !== "string"
      || !Object.entries(textValue(basis, "")).every(([key, pin]) => key === "feedback" || value[key] === pin)) {
      return { ok: false, reason: READ_ERROR };
    }
    return { ok: true, feedback: value.feedback };
  } catch { return { ok: false, reason: READ_ERROR }; }
}

export function saveFailedPlanningFeedbackDraft(
  basis: FailedPlanningFeedbackBasis, feedback: string,
): FailedPlanningFeedbackStorageResult {
  if (!validBasis(basis) || typeof feedback !== "string") return { ok: false, reason: WRITE_ERROR };
  try {
    window.localStorage.setItem(failedPlanningFeedbackDraftKey(basis), JSON.stringify(textValue(basis, feedback)));
    return { ok: true };
  } catch { return { ok: false, reason: WRITE_ERROR }; }
}

export function clearAppliedFailedPlanningFeedbackDraft(
  scope: PlanningDraftScope, original: CommandRequest,
): FailedPlanningFeedbackStorageResult {
  const payload = original.payload;
  if (!validScope(scope) || original.command_type !== "ReviseFailedPlanningRequest"
    || original.workspace_id !== scope.workspace_id || original.target_type !== "planning_request"
    || original.target_id !== scope.request_id || original.actor.type !== "user" || original.actor.id !== scope.principal_id
    || !uuid(original.command_id) || !uuid(original.idempotency_key) || !uuid(original.correlation_id)
    || !positive(original.expected_version) || !exactKeys(payload, [
      "failed_planning_run_id", "expected_failed_planning_run_version", "expected_work_context_version",
      "work_context_revision_id", "work_context_content_hash", "feedback",
    ]) || !uuid(payload.failed_planning_run_id) || !positive(payload.expected_failed_planning_run_version)
    || !positive(payload.expected_work_context_version) || !uuid(payload.work_context_revision_id)
    || typeof payload.work_context_content_hash !== "string" || !HASH.test(payload.work_context_content_hash)
    || !nonblank(payload.feedback)) return { ok: false, reason: WRITE_ERROR };
  try {
    window.localStorage.removeItem(textKey(scope, payload.failed_planning_run_id, payload.expected_failed_planning_run_version));
    return { ok: true };
  } catch { return { ok: false, reason: WRITE_ERROR }; }
}

function textKey(scope: PlanningDraftScope, runId: string, version: number): string {
  return [PREFIX, scope.kind, scope.workspace_id, scope.request_id, scope.principal_id,
    ...(scope.kind === "revision" ? [scope.root_task_id] : []), runId, String(version)]
    .map((value, index) => index === 0 ? value : encodeURIComponent(value)).join(":");
}

function textValue(basis: FailedPlanningFeedbackBasis, feedback: string) {
  return { schema_version: 1, ...basis.scope, failed_planning_run_id: basis.failedRunId,
    failed_planning_run_version: basis.failedRunVersion, feedback };
}

function validBasis(basis: FailedPlanningFeedbackBasis): boolean {
  return validScope(basis.scope) && uuid(basis.failedRunId) && positive(basis.failedRunVersion)
    && positive(basis.requestVersion) && positive(basis.attempt) && positive(basis.briefRevision)
    && uuid(basis.workContextId) && positive(basis.workContextVersion) && uuid(basis.workContextRevisionId)
    && positive(basis.workContextRevisionNumber) && HASH.test(basis.workContextContentHash);
}

function validScope(scope: PlanningDraftScope): boolean {
  return uuid(scope.workspace_id) && uuid(scope.request_id) && nonblank(scope.principal_id)
    && (scope.kind === "create" || scope.kind === "revision" && uuid(scope.root_task_id));
}

function sameRef(left: PlanningRun["planner_agent_ref"], right: PlanningRun["planner_agent_ref"]): boolean {
  return left != null && right != null && left.workspace_id === right.workspace_id
    && left.profile_id === right.profile_id && left.revision === right.revision;
}

function validRef(ref: PlanningRun["planner_agent_ref"], workspace: string): boolean {
  return ref != null && ref.workspace_id === workspace && uuid(ref.profile_id) && positive(ref.revision);
}

function sameRun(left: PlanningRun, right: PlanningRun): boolean {
  const keys = ["id", "workspace_id", "planning_request_id", "version", "attempt", "brief_revision", "operation_id",
    "execution_mode", "status", "result_kind", "provider_ref", "finished_at"] as const;
  const nativeKeys = ["status", "closed", "run_id", "response_id", "event_cursor", "start_operation_id"] as const;
  return keys.every((key) => left[key] === right[key]) && sameRef(left.planner_agent_ref, right.planner_agent_ref)
    && left.native_run != null && right.native_run != null
    && nativeKeys.every((key) => left.native_run?.[key] === right.native_run?.[key]);
}

function positive(value: unknown): value is number { return Number.isInteger(value) && (value as number) > 0; }
function nonblank(value: unknown): value is string { return typeof value === "string" && value.trim().length > 0; }
function uuid(value: unknown): value is string { return typeof value === "string" && UUID.test(value); }
function record(value: unknown): value is Record<string, unknown> { return typeof value === "object" && value !== null && !Array.isArray(value); }
function exactKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  return Object.keys(value).length === keys.length && keys.every((key) => Object.hasOwn(value, key));
}
