import { describe, expect, it } from "vitest";

import type { AgentBindingView } from "@/features/assignments/types";
import type { TaskView } from "@/features/tasks/types";
import type { PlanningRequest, PlanningRun, RevisionDiff } from "./types";
import { reviewNativeRevision } from "./nativeRevisionReview";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const ROOT_TASK_ID = "30000000-0000-0000-0000-000000000001";
const REQUEST_ID = "40000000-0000-0000-0000-000000000001";
const RUN_ID = "50000000-0000-0000-0000-000000000001";
const PROFILE_ID = "60000000-0000-0000-0000-000000000001";
const SNAPSHOT_ID = "70000000-0000-0000-0000-000000000001";
const EXISTING_SUBTASK_ID = "80000000-0000-0000-0000-000000000001";
const HASH = "a".repeat(64);
const PROPOSAL_HASH = "b".repeat(64);
const DIFF_HASH = "c".repeat(64);

const base = {
  workspace_id: WORKSPACE_ID,
  root_task_id: ROOT_TASK_ID,
  plan_snapshot_id: SNAPSHOT_ID,
  plan_content_hash: HASH,
  plan_generation: 5,
  root_task_version: 11,
  schema_version: 1,
  captured_at: "2026-10-07T00:00:00Z",
};

const freshness = {
  base,
  base_fresh: true,
  checked_at: "2026-10-07T00:00:01Z",
  current_plan_content_hash: HASH,
  current_plan_generation: 5,
  current_plan_snapshot_id: SNAPSHOT_ID,
  current_root_task_version: 11,
  stale_reasons: [],
};

function run(overrides: Record<string, unknown> = {}): PlanningRun {
  return {
    id: RUN_ID,
    workspace_id: WORKSPACE_ID,
    planning_request_id: REQUEST_ID,
    execution_mode: "native_profile",
    version: 1,
    ...overrides,
  } as PlanningRun;
}

function request(overrides: Record<string, unknown> = {}): PlanningRequest {
  return {
    id: REQUEST_ID,
    workspace_id: WORKSPACE_ID,
    kind: "revision",
    status: "proposal_ready",
    version: 4,
    current_proposal_revision: 2,
    current_proposal_content_hash: PROPOSAL_HASH,
    current_run_id: RUN_ID,
    planning_profile_id: PROFILE_ID,
    planning_profile_version: 3,
    profile: {
      id: PROFILE_ID,
      workspace_id: WORKSPACE_ID,
      execution_mode: "native_profile",
    },
    active_run: run(),
    revision_base: base,
    revision_freshness: freshness,
    ...overrides,
  } as unknown as PlanningRequest;
}

function diff(overrides: Record<string, unknown> = {}): RevisionDiff {
  return {
    workspace_id: WORKSPACE_ID,
    planning_request_id: REQUEST_ID,
    root_task_id: ROOT_TASK_ID,
    proposal_revision_id: "90000000-0000-0000-0000-000000000001",
    proposal_revision: 2,
    proposal_content_hash: PROPOSAL_HASH,
    diff_hash: DIFF_HASH,
    base,
    freshness,
    subtask_changes: [{
      target_subtask_key: "api",
      path: "Root / API",
      strategy: "reuse",
      existing_task_id: EXISTING_SUBTASK_ID,
      field_changes: [],
      reason: "existing task",
    }],
    worker_item_changes: [{
      target_worker_item_key: "backend",
      path: "Root / API / Backend",
      strategy: "derive",
      context_disposition: "derive",
      from_subtask_key: null,
      to_subtask_key: "api",
      existing_worker_item_id: null,
      reasons: [],
      field_changes: [],
    }],
    ...overrides,
  } as unknown as RevisionDiff;
}

function rootTask(overrides: Record<string, unknown> = {}): TaskView {
  return {
    id: ROOT_TASK_ID,
    workspace_id: WORKSPACE_ID,
    title: "Root task",
    description: "Root description",
    acceptance_criteria: [],
    requested_by: "developer",
    priority: 1,
    status: "running",
    version: 11,
    created_at: "2026-10-07T00:00:00Z",
    updated_at: "2026-10-07T00:00:00Z",
    ...overrides,
  } as TaskView;
}

function reusedBinding(overrides: Record<string, unknown> = {}): AgentBindingView {
  return {
    workspace_id: WORKSPACE_ID,
    target_type: "subtask",
    target_id: EXISTING_SUBTASK_ID,
    version: 2,
    assignee: { kind: "profile", id: PROFILE_ID },
    profile_ref: { workspace_id: WORKSPACE_ID, profile_id: PROFILE_ID, revision: 1 },
    created_at: "2026-10-07T00:00:00Z",
    changed_at: "2026-10-07T00:00:00Z",
    runnable: false,
    not_runnable_reasons: ["profile_disabled"],
    ...overrides,
  } as AgentBindingView;
}

function input(overrides: Record<string, unknown> = {}) {
  return {
    workspaceId: WORKSPACE_ID,
    rootTaskId: ROOT_TASK_ID,
    requestId: REQUEST_ID,
    request: request(),
    diff: diff(),
    rootTask: rootTask(),
    runs: [run()],
    reusedBindings: new Map([[JSON.stringify(["subtask", EXISTING_SUBTASK_ID]), reusedBinding()]]),
    ...overrides,
  };
}

describe("native revision review facts", () => {
  it("freezes a revision review with the complete actual target set and saved reuse pin", () => {
    const result = reviewNativeRevision(input());

    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.binding).toMatchObject({
      requestVersion: 4,
      proposalRevision: 2,
      proposalContentHash: PROPOSAL_HASH,
      profileId: PROFILE_ID,
      profileVersion: 3,
      rootTaskId: ROOT_TASK_ID,
      diffHash: DIFF_HASH,
      baseSnapshotId: SNAPSHOT_ID,
      baseContentHash: HASH,
      baseGeneration: 5,
      baseRootTaskVersion: 11,
    });
    expect(result.targets).toEqual([
      { kind: "subtask", key: "api", label: "Root / API", subtaskKey: null, strategy: "reuse", existingId: EXISTING_SUBTASK_ID },
      { kind: "worker_item", key: "backend", label: "Root / API / Backend", subtaskKey: "api", strategy: "derive", existingId: null },
    ]);
    expect(Object.isFrozen(result.binding)).toBe(true);
    expect(Object.isFrozen(result.targets)).toBe(true);
    expect(Object.isFrozen(result.targets[0])).toBe(true);
  });

  it.each([
    ["foreign route workspace", { workspaceId: "foreign-workspace" }],
    ["foreign route root", { rootTaskId: "foreign-root" }],
    ["foreign route request", { requestId: "foreign-request" }],
    ["foreign diff request", { diff: diff({ planning_request_id: "foreign-request" }) }],
    ["foreign diff root", { diff: diff({ root_task_id: "foreign-root" }) }],
    ["foreign root task", { rootTask: rootTask({ id: "foreign-root" }) }],
    ["proposal revision mismatch", { diff: diff({ proposal_revision: 3 }) }],
    ["proposal hash mismatch", { diff: diff({ proposal_content_hash: "d".repeat(64) }) }],
    ["stale current base", { diff: diff({ freshness: { ...freshness, current_plan_generation: 6 } }) }],
    ["missing reused binding", { reusedBindings: new Map() }],
    ["wrong reused binding version target", {
      reusedBindings: new Map([[JSON.stringify(["subtask", EXISTING_SUBTASK_ID]), reusedBinding({ version: 0 })]]),
    }],
  ] as const)("fails closed for %s", (_name, overrides) => {
    expect(reviewNativeRevision(input(overrides)).ok).toBe(false);
  });

  it.each([
    ["foreign base workspace", { workspace_id: "foreign-workspace" }],
    ["foreign base root", { root_task_id: "foreign-root" }],
  ] as const)("fails closed for a mutually consistent %s", (_name, pin) => {
    const foreignBase = { ...base, ...pin };
    const result = reviewNativeRevision(input({
      request: request({
        revision_base: foreignBase,
        revision_freshness: { ...freshness, base: foreignBase },
      }),
      diff: diff({
        base: foreignBase,
        freshness: { ...freshness, base: foreignBase },
      }),
    }));

    expect(result.ok).toBe(false);
  });

  it("keeps immutable fingerprints independent of labels and observation timestamps", () => {
    const first = reviewNativeRevision(input());
    const second = reviewNativeRevision(input({
      diff: diff({
        subtask_changes: [{
          target_subtask_key: "api",
          path: "Renamed display only",
          strategy: "reuse",
          existing_task_id: EXISTING_SUBTASK_ID,
          field_changes: [],
          reason: "display changed",
        }],
        freshness: { ...freshness, checked_at: "2026-10-08T00:00:00Z" },
      }),
      reusedBindings: new Map([[JSON.stringify(["subtask", EXISTING_SUBTASK_ID]), reusedBinding({
        changed_at: "2026-10-08T00:00:00Z",
      })]]),
    }));

    expect(first.ok && second.ok).toBe(true);
    if (first.ok && second.ok) {
      expect(second.binding.targetsFingerprint).toBe(first.binding.targetsFingerprint);
      expect(second.binding.reusedBindingsFingerprint).toBe(first.binding.reusedBindingsFingerprint);
    }
  });

  it("serializes nested reuse pins as canonical sorted JSON", () => {
    const result = reviewNativeRevision(input());

    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.binding.reusedBindingsFingerprint).toBe(JSON.stringify([{
      assignee: { id: PROFILE_ID, kind: "profile" },
      kind: "subtask",
      profile_ref: { profile_id: PROFILE_ID, revision: 1, workspace_id: WORKSPACE_ID },
      targetId: EXISTING_SUBTASK_ID,
      version: 2,
    }]));
  });
});
