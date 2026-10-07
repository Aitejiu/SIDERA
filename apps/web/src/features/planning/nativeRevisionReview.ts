import { assignmentTargetKey } from "@/features/assignments/api";
import { extractRevisionAssignmentTargets } from "@/features/assignments/proposalAssignments";
import type { AgentBindingView, ProposalAssignmentTarget } from "@/features/assignments/types";
import type { TaskView } from "@/features/tasks/types";
import type { PlanningRequest, PlanningRun, RevisionDiff } from "./types";
import type { ProposalReviewBinding } from "./ProposalActions";
import { detectPlanningMode } from "./nativePlanningReview";

export interface RevisionReviewBinding extends ProposalReviewBinding {
  rootTaskId: string;
  diffHash: string;
  baseSnapshotId: string;
  baseContentHash: string;
  baseGeneration: number;
  baseRootTaskVersion: number;
  targetsFingerprint: string;
  reusedBindingsFingerprint: string;
}

type NativeRevisionReviewResult =
  | { ok: true; binding: RevisionReviewBinding; targets: readonly ProposalAssignmentTarget[] }
  | { ok: false; reason: string };

const UNAVAILABLE = "规划方案事实不可用";

export function reviewNativeRevision(input: {
  workspaceId: string;
  rootTaskId: string;
  requestId: string;
  request: PlanningRequest;
  diff: RevisionDiff;
  rootTask: TaskView;
  runs: readonly PlanningRun[];
  reusedBindings: ReadonlyMap<string, AgentBindingView>;
}): NativeRevisionReviewResult {
  try {
    const { workspaceId, rootTaskId, requestId, request, diff, rootTask, runs, reusedBindings } = input;
    if (!nonblank(workspaceId) || !nonblank(rootTaskId) || !nonblank(requestId)
      || request.workspace_id !== workspaceId || request.id !== requestId
      || request.kind !== "revision" || request.status !== "proposal_ready"
      || detectPlanningMode(request, runs) !== "native_profile"
      || !positiveInteger(request.version)
      || !positiveInteger(request.planning_profile_version)
      || !nonblank(request.planning_profile_id)
      || !positiveInteger(request.current_proposal_revision)
      || !nonblank(request.current_proposal_content_hash)
      || !selectedNativeRun(request, runs)
      || rootTask.workspace_id !== workspaceId || rootTask.id !== rootTaskId
      || !positiveInteger(rootTask.version)
      || diff.workspace_id !== workspaceId || diff.planning_request_id !== requestId
      || diff.root_task_id !== rootTaskId
      || diff.proposal_revision !== request.current_proposal_revision
      || diff.proposal_content_hash !== request.current_proposal_content_hash
      || !positiveInteger(diff.proposal_revision)
      || !nonblank(diff.proposal_content_hash) || !nonblank(diff.diff_hash)) return unavailable();

    const requestBase = readBase(request.revision_base);
    const requestFreshness = readFreshness(request.revision_freshness);
    const diffBase = readBase(diff.base);
    const diffFreshness = readFreshness(diff.freshness);
    if (requestBase === null || requestBase.workspace_id !== workspaceId || requestBase.root_task_id !== rootTaskId
      || requestFreshness === null || diffBase === null || diffFreshness === null
      || !sameBase(requestBase, requestFreshness.base)
      || !sameBase(requestBase, diffBase)
      || !sameBase(requestBase, diffFreshness.base)
      || requestFreshness.base_fresh !== true || diffFreshness.base_fresh !== true
      || !freshnessMatchesBase(requestFreshness, requestBase)
      || !freshnessMatchesBase(diffFreshness, requestBase)
      || rootTask.version !== requestBase.root_task_version) return unavailable();

    const targets = extractRevisionAssignmentTargets(diff);
    const reusePins = targets.filter((target) => target.strategy === "reuse").map((target) => {
      if (target.existingId === null) throw new Error(UNAVAILABLE);
      const binding = reusedBindings.get(assignmentTargetKey(target.kind, target.existingId));
      validateReuseBinding(binding, workspaceId, target);
      return {
        kind: target.kind,
        targetId: target.existingId,
        version: binding.version,
        assignee: { kind: binding.assignee.kind, id: binding.assignee.id },
        profile_ref: binding.profile_ref === null ? null : {
          workspace_id: binding.profile_ref.workspace_id,
          profile_id: binding.profile_ref.profile_id,
          revision: binding.profile_ref.revision,
        },
      };
    });

    const binding = deepFreeze({
      requestVersion: request.version,
      proposalRevision: request.current_proposal_revision,
      proposalContentHash: request.current_proposal_content_hash,
      profileId: request.planning_profile_id,
      profileVersion: request.planning_profile_version,
      rootTaskId,
      diffHash: diff.diff_hash,
      baseSnapshotId: requestBase.plan_snapshot_id,
      baseContentHash: requestBase.plan_content_hash,
      baseGeneration: requestBase.plan_generation,
      baseRootTaskVersion: requestBase.root_task_version,
      targetsFingerprint: fingerprint(targets.map((target) => ({
        kind: target.kind,
        key: target.key,
        parent: target.subtaskKey,
        identity_strategy: target.strategy,
        existing_id: target.existingId,
      }))),
      reusedBindingsFingerprint: fingerprint(reusePins),
    });
    return {
      ok: true,
      binding,
      targets: deepFreeze(targets.map((target) => ({ ...target }))),
    };
  } catch {
    return unavailable();
  }
}

interface RevisionBasePins {
  workspace_id: string;
  root_task_id: string;
  plan_snapshot_id: string;
  plan_content_hash: string;
  plan_generation: number;
  root_task_version: number;
  schema_version: number;
}

interface RevisionFreshnessPins {
  base: RevisionBasePins;
  base_fresh: boolean;
  current_plan_snapshot_id: string | null;
  current_plan_content_hash: string | null;
  current_plan_generation: number | null;
  current_root_task_version: number | null;
}

function readBase(value: unknown): RevisionBasePins | null {
  if (!isRecord(value) || !nonblank(value.workspace_id) || !nonblank(value.root_task_id)
    || !nonblank(value.plan_snapshot_id) || !nonblank(value.plan_content_hash)
    || !positiveInteger(value.plan_generation) || !positiveInteger(value.root_task_version)
    || value.schema_version !== 1) return null;
  return {
    workspace_id: value.workspace_id,
    root_task_id: value.root_task_id,
    plan_snapshot_id: value.plan_snapshot_id,
    plan_content_hash: value.plan_content_hash,
    plan_generation: value.plan_generation,
    root_task_version: value.root_task_version,
    schema_version: value.schema_version,
  };
}

function readFreshness(value: unknown): RevisionFreshnessPins | null {
  if (!isRecord(value)) return null;
  const base = readBase(value.base);
  if (base === null || typeof value.base_fresh !== "boolean") return null;
  const currentPlanSnapshotId = value.current_plan_snapshot_id;
  const currentPlanContentHash = value.current_plan_content_hash;
  const currentPlanGeneration = value.current_plan_generation;
  const currentRootTaskVersion = value.current_root_task_version;
  if (!(currentPlanSnapshotId === null || nonblank(currentPlanSnapshotId))
    || !(currentPlanContentHash === null || nonblank(currentPlanContentHash))
    || !(currentPlanGeneration === null || positiveInteger(currentPlanGeneration))
    || !(currentRootTaskVersion === null || positiveInteger(currentRootTaskVersion))) return null;
  return {
    base,
    base_fresh: value.base_fresh,
    current_plan_snapshot_id: currentPlanSnapshotId,
    current_plan_content_hash: currentPlanContentHash,
    current_plan_generation: currentPlanGeneration,
    current_root_task_version: currentRootTaskVersion,
  };
}

function sameBase(left: RevisionBasePins, right: RevisionBasePins): boolean {
  return left.workspace_id === right.workspace_id
    && left.root_task_id === right.root_task_id
    && left.plan_snapshot_id === right.plan_snapshot_id
    && left.plan_content_hash === right.plan_content_hash
    && left.plan_generation === right.plan_generation
    && left.root_task_version === right.root_task_version
    && left.schema_version === right.schema_version;
}

function freshnessMatchesBase(freshness: RevisionFreshnessPins, base: RevisionBasePins): boolean {
  return freshness.current_plan_snapshot_id === base.plan_snapshot_id
    && freshness.current_plan_content_hash === base.plan_content_hash
    && freshness.current_plan_generation === base.plan_generation
    && freshness.current_root_task_version === base.root_task_version;
}

function selectedNativeRun(request: PlanningRequest, runs: readonly PlanningRun[]): boolean {
  const runId = request.current_run_id;
  if (!nonblank(runId)) return false;
  const selected = request.active_run?.id === runId
    ? request.active_run
    : runs.find((run) => run.id === runId);
  return selected !== undefined && selected.id === runId
    && selected.workspace_id === request.workspace_id
    && selected.planning_request_id === request.id
    && selected.execution_mode === "native_profile"
    && positiveInteger(selected.version);
}

function validateReuseBinding(
  value: AgentBindingView | undefined,
  workspaceId: string,
  target: ProposalAssignmentTarget,
): asserts value is AgentBindingView {
  if (value === undefined || value.workspace_id !== workspaceId
    || value.target_type !== target.kind || value.target_id !== target.existingId
    || !positiveInteger(value.version) || !isRecord(value.assignee)
    || !nonblank(value.assignee.id)) throw new Error(UNAVAILABLE);
  if (value.assignee.kind === "human") {
    if (value.profile_ref !== null) throw new Error(UNAVAILABLE);
    return;
  }
  if (value.assignee.kind !== "profile" || value.profile_ref === null
    || value.profile_ref.workspace_id !== workspaceId
    || value.profile_ref.profile_id !== value.assignee.id
    || !positiveInteger(value.profile_ref.revision)) throw new Error(UNAVAILABLE);
}

function fingerprint(records: readonly Record<string, unknown>[]): string {
  const canonical = records
    .map(canonicalValue)
    .sort((left, right) => JSON.stringify(left).localeCompare(JSON.stringify(right)));
  return JSON.stringify(canonical);
}

function canonicalValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalValue);
  if (!isRecord(value)) return value;
  return Object.fromEntries(Object.entries(value)
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([key, child]) => [key, canonicalValue(child)]));
}

function deepFreeze<T>(value: T): T {
  if (typeof value !== "object" || value === null || Object.isFrozen(value)) return value;
  for (const child of Object.values(value)) deepFreeze(child);
  return Object.freeze(value);
}

function unavailable(): { ok: false; reason: string } {
  return { ok: false, reason: UNAVAILABLE };
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
