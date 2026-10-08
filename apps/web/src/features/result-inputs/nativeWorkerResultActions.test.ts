import { describe, expect, it, vi } from "vitest";

import type { CommandRequest, CommandResponse } from "../assignments/types";
import type { WorkerWorkspace } from "../worker-items/types";
import {
  freezeWorkerResultActionReview,
  loadNativeWorkerResultActionDraft,
  nativeWorkerResultActionDraftKey,
  reviewWorkerGovernanceAction,
  reviewWorkerResultActionResult,
  saveNativeWorkerResultActionDraft,
  workerResultActionFingerprint,
} from "./nativeWorkerResultActions";
import type { NativeWorkerResultActionDraft, WorkerResultActionScope } from "./nativeWorkerResultActions";

const scope: WorkerResultActionScope = {
  workspace_id: "20000000-0000-0000-0000-000000000001",
  principal_id: "10000000-0000-0000-0000-000000000001",
  root_task_id: "30000000-0000-0000-0000-000000000001",
  worker_item_id: "40000000-0000-0000-0000-000000000001",
};

function workspaceFixture(): WorkerWorkspace {
  return {
    workspace_id: scope.workspace_id,
    worker_item: {
      id: scope.worker_item_id, root_task_id: scope.root_task_id, stable_key: "worker",
      title: "Worker", responsibility: "Review results", status: "active", version: 9,
      context_version: 4, acknowledged_context_version: 4, updated_at: "2026-10-07T00:00:00Z",
    },
    current_placement: {
      worker_item_key: "worker", subtask_key: "summary", subtask_task_id: scope.root_task_id,
      role: "implementer", capability_requirements: [], plan_generation: 2,
      plan_snapshot_id: "b0000000-0000-0000-0000-000000000004",
    },
    current_runtime: null, current_step: null, next_action: null, latest_checkpoint: null,
    placement_history: [], execution_history: [], artifacts: [], evidence: [],
    usage: { input_tokens: 0, output_tokens: 0, cached_tokens: 0, cost_microunits: 0 },
    can_intervene: false, read_only_reason: "no_active_session", intervention_context: null,
    trace_descriptor: null, last_change_cursor: 1, as_of: "2026-10-07T00:00:00Z",
    collaboration: {
      enabled: true, state: "waiting_release", waiting_summary: null, upstream_links: [],
      governance_history: [], governance_mode: "paused", release_state: "proposed",
      attention_state: "decision_required", available_actions: ["pause", "approve", "reject", "resume_automatic"],
      active_hold_id: "b0000000-0000-0000-0000-000000000001", hold_resource_version: 3,
      active_proposal: {
        proposal_id: "b0000000-0000-0000-0000-000000000002", resource_version: 8,
        status: "pending", summary: "Review", reason_code: "ready", target_request_version: 17,
        created_at: "2026-10-07T00:00:00Z", input_selection_id: "b0000000-0000-0000-0000-000000000003",
        input_selection_hash: "a".repeat(64),
      },
    },
  };
}

function draftFixture(): NativeWorkerResultActionDraft {
  const action = reviewWorkerGovernanceAction(workspaceFixture(), "pause");
  if (action === null) throw new Error("Missing Pause fixture");
  return {
    schema_version: 1, scope, review: { action, reason: "Review first" }, original: pauseOriginalFixture(),
    status: "unknown", aggregate_id: null, aggregate_version: null, outcome: null, rejection_code: null,
  };
}

function pauseOriginalFixture(): CommandRequest {
  return {
    command_id: "c0000000-0000-0000-0000-000000000002",
    workspace_id: "20000000-0000-0000-0000-000000000001",
    command_type: "PauseCollaborationRelease",
    actor: { type: "user", id: "10000000-0000-0000-0000-000000000001" },
    target_type: "worker_item",
    target_id: "40000000-0000-0000-0000-000000000001",
    expected_version: 9,
    idempotency_key: "pause-original",
    correlation_id: "c0000000-0000-0000-0000-000000000003",
    payload: { transport: "web", reason: "Review first", target_plan_generation: 2 },
  };
}

describe("native WorkerItem result mapping", () => {
  it("maps Pause to its new hold rather than the WorkerItem aggregate", () => {
    const original = pauseOriginalFixture();
    const response = {
      command_id: original.command_id,
      aggregate_id: "b0000000-0000-0000-0000-000000000001",
      aggregate_version: 1,
      event_ids: [],
      data: {
        hold_id: "b0000000-0000-0000-0000-000000000001",
        hold_version: 1,
        governance_mode: "paused",
      },
    } satisfies CommandResponse;
    expect(reviewWorkerResultActionResult(original, response)).toMatchObject({
      status: "applied", aggregate_version: 1, outcome: "paused",
    });
    expect(reviewWorkerResultActionResult(original, {
      ...response, command_id: "c0000000-0000-0000-0000-000000000001",
    })).toMatchObject({ status: "unknown", outcome: null });
  });

  it.each([
    ["ApproveCollaborationReleaseProposal", "collaboration_release_proposal", { proposal_status: "approved", result: "applied" }, "approved"],
    ["ApproveCollaborationReleaseProposal", "collaboration_release_proposal", { proposal_status: "stale", result: "stale" }, "stale"],
    ["RejectCollaborationReleaseProposal", "collaboration_release_proposal", { proposal_status: "rejected" }, "rejected"],
    ["ResumeAutomaticCollaboration", "collaboration_governance_hold", { hold_version: 10, governance_mode: "automatic" }, "automatic"],
    ["RetryContextAssembly", "context_assembly", { status: "queued" }, "queued"],
  ] as const)("maps %s to the source-established %s outcome", (commandType, targetType, facts, outcome) => {
    const original = { ...pauseOriginalFixture(), command_type: commandType, target_type: targetType };
    const response: CommandResponse = {
      command_id: original.command_id,
      aggregate_id: original.target_id ?? "",
      aggregate_version: 10,
      event_ids: [],
      data: {
        proposal_id: original.target_id,
        hold_id: original.target_id,
        context_assembly_request_id: original.target_id,
        ...facts,
      },
    };
    expect(reviewWorkerResultActionResult(original, response)).toEqual({
      status: "applied", aggregate_id: original.target_id, aggregate_version: 10, outcome,
    });
    for (const malformed of [
      { ...response, aggregate_id: "b0000000-0000-0000-0000-000000000009" },
      { ...response, aggregate_version: 11 },
      { ...response, data: {} },
      { ...response, data: { ...response.data, proposal_id: "foreign", hold_id: "foreign", context_assembly_request_id: "foreign" } },
    ]) {
      expect(reviewWorkerResultActionResult(original, malformed)).toMatchObject({ status: "applied", outcome: null });
    }
    expect(reviewWorkerResultActionResult(original, { ...response, command_id: "foreign" })).toMatchObject({ status: "unknown", outcome: null });
  });

  it("retains correlated handling without inventing a business outcome", () => {
    const original = pauseOriginalFixture();
    expect(reviewWorkerResultActionResult(original, {
      command_id: original.command_id, aggregate_id: original.target_id ?? "", aggregate_version: 10, event_ids: [],
    })).toMatchObject({ status: "applied", outcome: null });
  });
});

describe("native WorkerItem action review and storage", () => {
  it.each(["pending", "unknown", "applied", "rejected"] as const)("rejects non-literal durable status %s", status => {
    const invalid = { ...draftFixture(), status: [status], rejection_code: null } as unknown as NativeWorkerResultActionDraft;
    expect(loadNativeWorkerResultActionDraft(scope, { getItem: () => JSON.stringify(invalid) })).toEqual({ ok: false, error: "invalid_draft" });
    const setItem = vi.fn();
    expect(saveNativeWorkerResultActionDraft(scope, invalid, { setItem })).toEqual({ ok: false, error: "invalid_draft" });
    expect(setItem).not.toHaveBeenCalled();
  });

  it.each(["pending", "unknown", "applied", "rejected"] as const)("preserves literal durable status %s", status => {
    const draft: NativeWorkerResultActionDraft = { ...draftFixture(), status, rejection_code: status === "rejected" ? "command_rejected" : null };
    expect(loadNativeWorkerResultActionDraft(scope, { getItem: () => JSON.stringify(draft) })).toEqual({ ok: true, draft });
    const setItem = vi.fn();
    expect(saveNativeWorkerResultActionDraft(scope, draft, { setItem })).toEqual({ ok: true });
    expect(setItem).toHaveBeenCalledOnce();
  });

  it("refuses a nonempty original with idle while preserving the empty idle slot", () => {
    expect(loadNativeWorkerResultActionDraft(scope, { getItem: () => JSON.stringify({ ...draftFixture(), status: "idle" }) })).toEqual({ ok: false, error: "invalid_draft" });
    const idle = { ...draftFixture(), status: "idle", original: null, review: null };
    expect(loadNativeWorkerResultActionDraft(scope, { getItem: () => JSON.stringify(idle) })).toEqual({ ok: true, draft: idle });
  });

  it("uses current WorkerItem, proposal, and hold versions independently of input capability", () => {
    const workspace = workspaceFixture();
    expect(reviewWorkerGovernanceAction(workspace, "pause")).toMatchObject({
      command_type: "PauseCollaborationRelease", target_id: scope.worker_item_id,
      expected_version: 9, expected_plan_generation: 2, expected_fingerprint: null,
    });
    for (const action of ["approve", "reject"] as const) {
      expect(reviewWorkerGovernanceAction(workspace, action)).toMatchObject({
        target_id: "b0000000-0000-0000-0000-000000000002", expected_version: 8,
        expected_fingerprint: "a".repeat(64), expected_plan_generation: 2,
        expected_input_selection_id: "b0000000-0000-0000-0000-000000000003",
        expected_input_selection_hash: "a".repeat(64),
      });
    }
    expect(reviewWorkerGovernanceAction(workspace, "resume_automatic")).toMatchObject({
      target_type: "collaboration_governance_hold", expected_version: 3,
      expected_input_selection_id: null, expected_input_selection_hash: null,
    });
  });

  it("allows jointly absent or null selection, but refuses partial or invalid descriptors", () => {
    const workspace = workspaceFixture();
    const proposal = workspace.collaboration?.active_proposal;
    if (!proposal) throw new Error("Missing proposal");
    delete proposal.input_selection_id;
    delete proposal.input_selection_hash;
    expect(reviewWorkerGovernanceAction(workspace, "approve")).toMatchObject({
      expected_fingerprint: null, expected_input_selection_id: null, expected_input_selection_hash: null,
    });
    proposal.input_selection_id = null;
    expect(reviewWorkerGovernanceAction(workspace, "approve")).toBeNull();
    proposal.input_selection_hash = null;
    expect(reviewWorkerGovernanceAction(workspace, "approve")).not.toBeNull();
    proposal.resource_version = Number.NaN;
    expect(reviewWorkerGovernanceAction(workspace, "approve")).toBeNull();
    workspace.worker_item.version = 0;
    expect(reviewWorkerGovernanceAction(workspace, "pause")).toBeNull();
    if (workspace.collaboration) workspace.collaboration.available_actions = [];
    expect(reviewWorkerGovernanceAction(workspace, "resume_automatic")).toBeNull();
  });

  it("freezes the bounded public action and normalizes reason in its fingerprint", () => {
    const action = reviewWorkerGovernanceAction(workspaceFixture(), "pause");
    if (!action) throw new Error("Missing Pause");
    const review = freezeWorkerResultActionReview(action, "  Review first  ");
    expect(review).toMatchObject({ reason: "Review first" });
    if (!review) throw new Error("Missing review");
    action.expected_version = 10;
    expect(review.action.expected_version).toBe(9);
    expect(Object.isFrozen(review.action)).toBe(true);
    expect(workerResultActionFingerprint(review)).toBe(JSON.stringify([
      "pause", "PauseCollaborationRelease", "worker_item", scope.worker_item_id,
      9, null, 2, null, null, "Review first",
    ]));
    expect(freezeWorkerResultActionReview({ ...action, command_type: "RetryContextAssembly" })).toBeNull();
    expect(freezeWorkerResultActionReview(action, "x".repeat(2001))).toBeNull();
  });

  it("round trips only the exact one-slot scoped public draft", () => {
    const draft = draftFixture();
    const values = new Map<string, string>();
    const storage = { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => { values.set(key, value); } };
    expect(nativeWorkerResultActionDraftKey(scope)).toBe(`sidera.native-worker-result-action.v1:${scope.workspace_id}:${scope.principal_id}:${scope.root_task_id}:${scope.worker_item_id}`);
    expect(saveNativeWorkerResultActionDraft(scope, draft, storage)).toEqual({ ok: true });
    expect(loadNativeWorkerResultActionDraft(scope, storage)).toEqual({ ok: true, draft });
    expect(loadNativeWorkerResultActionDraft({ ...scope, principal_id: "other" }, storage)).toMatchObject({ ok: true, draft: { status: "idle", original: null } });
  });

  it.each(["principal_id", "root_task_id", "worker_item_id"] as const)("refuses a draft from another %s", (field) => {
    const draft = draftFixture();
    draft.scope = { ...scope, [field]: "foreign" };
    expect(loadNativeWorkerResultActionDraft(scope, { getItem: () => JSON.stringify(draft) })).toEqual({ ok: false, error: "invalid_draft" });
    expect(saveNativeWorkerResultActionDraft(scope, draft, { setItem: () => { throw new Error("must not write"); } })).toEqual({ ok: false, error: "invalid_draft" });
  });

  it.each([
    (draft: NativeWorkerResultActionDraft) => ({ ...draft, token: "private" }),
    (draft: NativeWorkerResultActionDraft) => ({ ...draft, scope: { ...draft.scope, csrf: "private" } }),
    (draft: NativeWorkerResultActionDraft) => ({ ...draft, review: { ...draft.review, raw: { secret: "private" } } }),
    (draft: NativeWorkerResultActionDraft) => ({ ...draft, review: { ...draft.review, action: { ...draft.review?.action, permission: true } } }),
    (draft: NativeWorkerResultActionDraft) => ({ ...draft, original: { ...draft.original, actor: { type: "local", id: scope.principal_id } } }),
    (draft: NativeWorkerResultActionDraft) => ({ ...draft, original: { ...draft.original, expected_version: 0 } }),
    (draft: NativeWorkerResultActionDraft) => ({ ...draft, original: { ...draft.original, payload: { ...draft.original?.payload, token: "private" } } }),
    (draft: NativeWorkerResultActionDraft) => ({ ...draft, original: { ...draft.original, payload: { transport: "web", reason: "changed", target_plan_generation: 2 } } }),
    (draft: NativeWorkerResultActionDraft) => ({ ...draft, original: { ...draft.original, payload: { transport: "web", reason: "Review first", target_plan_generation: 3 } } }),
  ])("rejects malformed or private nested draft data %#", (mutate) => {
    const draft = mutate(draftFixture());
    expect(loadNativeWorkerResultActionDraft(scope, { getItem: () => JSON.stringify(draft) })).toEqual({ ok: false, error: "invalid_draft" });
  });

  it("contains storage and JSON failures without exposing their details", () => {
    expect(loadNativeWorkerResultActionDraft(scope, { getItem: () => { throw new Error("secret"); } })).toEqual({ ok: false, error: "storage_unavailable" });
    expect(loadNativeWorkerResultActionDraft(scope, { getItem: () => "{" })).toEqual({ ok: false, error: "invalid_draft" });
    expect(saveNativeWorkerResultActionDraft(scope, draftFixture(), { setItem: () => { throw new Error("secret"); } })).toEqual({ ok: false, error: "storage_unavailable" });
  });
});
