import { extractInitialAssignmentTargets } from "@/features/assignments/proposalAssignments";
import type { ProposalAssignmentTarget } from "@/features/assignments/types";
import type { PlanningProposal, PlanningRequest, PlanningRun } from "./types";
import type { ProposalReviewBinding } from "./ProposalActions";

export type PlanningMode = "legacy" | "native_profile" | "unavailable";

export function detectPlanningMode(
  request: PlanningRequest,
  runs: readonly PlanningRun[],
): PlanningMode {
  if (!isRecord(request) || !nonblank(request.id) || !nonblank(request.workspace_id)
    || !isRecord(request.profile)) return "unavailable";

  const profile = request.profile;
  if (profile.id !== request.planning_profile_id || profile.workspace_id !== request.workspace_id) {
    return "unavailable";
  }

  const profileMode = readMode(profile.execution_mode);
  if (profileMode === null) return "unavailable";

  if (request.current_run_id === null) {
    return profileMode === "legacy" ? "legacy" : "unavailable";
  }
  if (!nonblank(request.current_run_id)) return "unavailable";

  const active = request.active_run;
  const selected = isRecord(active) && active.id === request.current_run_id
    ? active
    : runs.find((run) => isRecord(run) && run.id === request.current_run_id);
  if (!isRecord(selected)) return profileMode === "legacy" ? "legacy" : "unavailable";
  if (selected.id !== request.current_run_id
    || selected.workspace_id !== request.workspace_id
    || selected.planning_request_id !== request.id) return "unavailable";

  const runMode = readMode(selected.execution_mode);
  if (runMode === null || runMode !== profileMode) return "unavailable";
  return profileMode;
}

export function reviewInitialNativeProposal(input: {
  workspaceId: string;
  requestId: string;
  request: PlanningRequest;
  proposal: PlanningProposal;
  runs: readonly PlanningRun[];
}):
  | { ok: true; binding: ProposalReviewBinding; targets: readonly ProposalAssignmentTarget[] }
  | { ok: false; reason: string } {
  try {
    const { workspaceId, requestId, request, proposal, runs } = input;
    if (!nonblank(workspaceId) || !nonblank(requestId)
      || request.workspace_id !== workspaceId || request.id !== requestId
      || request.kind !== "create" || request.status !== "proposal_ready"
      || detectPlanningMode(request, runs) !== "native_profile"
      || !positiveInteger(request.version)
      || !positiveInteger(request.planning_profile_version)
      || !nonblank(request.planning_profile_id)
      || !positiveInteger(request.current_proposal_revision)
      || !nonblank(request.current_proposal_content_hash)
      || proposal.planning_request_id !== requestId
      || proposal.planning_run_id !== request.current_run_id
      || !nonblank(proposal.id)
      || proposal.kind !== "create"
      || proposal.revision !== request.current_proposal_revision
      || !positiveInteger(proposal.revision)
      || proposal.content_hash !== request.current_proposal_content_hash
      || !nonblank(proposal.content_hash)) return unavailableReview();

    const targets = extractInitialAssignmentTargets(proposal);
    if (targets.length === 0) return unavailableReview();

    const binding = deepFreeze({
      requestVersion: request.version,
      proposalRevision: proposal.revision,
      proposalContentHash: proposal.content_hash,
      profileId: request.planning_profile_id,
      profileVersion: request.planning_profile_version,
    });
    return { ok: true, binding, targets: deepFreeze(targets.map((target) => ({ ...target }))) };
  } catch {
    return unavailableReview();
  }
}

function readMode(value: unknown): "legacy" | "native_profile" | null {
  if (value === "legacy" || value === undefined) return "legacy";
  return value === "native_profile" ? "native_profile" : null;
}

function positiveInteger(value: unknown): value is number {
  return Number.isInteger(value) && (value as number) > 0;
}

function nonblank(value: unknown): value is string {
  return typeof value === "string" && value.length > 0 && value.trim() === value;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function deepFreeze<T>(value: T): T {
  if (typeof value !== "object" || value === null || Object.isFrozen(value)) return value;
  for (const child of Object.values(value)) deepFreeze(child);
  return Object.freeze(value);
}

function unavailableReview(): { ok: false; reason: string } {
  return { ok: false, reason: "规划方案事实不可用" };
}
