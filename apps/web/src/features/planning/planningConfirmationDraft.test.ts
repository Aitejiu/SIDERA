import { beforeEach, describe, expect, it, vi } from "vitest";

import { assignmentTargetKey } from "@/features/assignments/api";
import { toPlanningAssigneeChoices } from "@/features/assignments/proposalAssignments";
import type { AssigneeValue, CommandRequest, ProposalAssignmentTarget } from "@/features/assignments/types";
import type { PlanningConfirmationDraft, PlanningDraftScope } from "./planningConfirmationDraft";
import { loadPlanningDraft, planningDraftKey, savePlanningDraft } from "./planningConfirmationDraft";

const SCOPE: Extract<PlanningDraftScope, { kind: "create" }> = {
  kind: "create",
  workspace_id: "20000000-0000-0000-0000-000000000001",
  request_id: "30000000-0000-0000-0000-000000000001",
  principal_id: "principal-current",
};
const REVIEW = {
  requestVersion: 8,
  proposalRevision: 2,
  proposalContentHash: "a".repeat(64),
  profileId: "50000000-0000-0000-0000-000000000001",
  profileVersion: 3,
};
const profileChoice: Extract<AssigneeValue, { kind: "profile" }> = {
  kind: "profile",
  id: REVIEW.profileId,
  profileRef: { workspace_id: SCOPE.workspace_id, profile_id: REVIEW.profileId, revision: 1 },
};

const REVISION_SCOPE: Extract<PlanningDraftScope, { kind: "revision" }> = {
  kind: "revision",
  workspace_id: SCOPE.workspace_id,
  request_id: "40000000-0000-0000-0000-000000000001",
  principal_id: SCOPE.principal_id,
  root_task_id: "50000000-0000-0000-0000-000000000001",
};
const REVISION_REVIEW = {
  requestVersion: 4,
  proposalRevision: 2,
  proposalContentHash: "b".repeat(64),
  profileId: "60000000-0000-0000-0000-000000000001",
  profileVersion: 3,
  rootTaskId: REVISION_SCOPE.root_task_id,
  diffHash: "c".repeat(64),
  baseSnapshotId: "70000000-0000-0000-0000-000000000001",
  baseContentHash: "d".repeat(64),
  baseGeneration: 5,
  baseRootTaskVersion: 11,
  targetsFingerprint: JSON.stringify([{
    existing_id: "80000000-0000-0000-0000-000000000001",
    identity_strategy: "reuse",
    key: "api",
    kind: "subtask",
    parent: null,
  }]),
  reusedBindingsFingerprint: JSON.stringify([{
    assignee: { id: "60000000-0000-0000-0000-000000000001", kind: "profile" },
    kind: "subtask",
    profile_ref: {
      profile_id: "60000000-0000-0000-0000-000000000001",
      revision: 1,
      workspace_id: SCOPE.workspace_id,
    },
    targetId: "80000000-0000-0000-0000-000000000001",
    version: 2,
  }]),
};

function revisionCommand(overrides: Record<string, unknown> = {}): CommandRequest {
  return {
    command_id: "90000000-0000-0000-0000-000000000001",
    workspace_id: SCOPE.workspace_id,
    command_type: "ConfirmPlanRevision",
    actor: { type: "user", id: SCOPE.principal_id },
    target_type: "planning_request",
    target_id: REVISION_SCOPE.request_id,
    expected_version: 4,
    idempotency_key: "a0000000-0000-0000-0000-000000000001",
    correlation_id: "b0000000-0000-0000-0000-000000000001",
    payload: {
      expected_proposal_revision: 2,
      expected_proposal_content_hash: REVISION_REVIEW.proposalContentHash,
      expected_diff_hash: REVISION_REVIEW.diffHash,
      expected_base_snapshot_id: REVISION_REVIEW.baseSnapshotId,
      expected_base_content_hash: REVISION_REVIEW.baseContentHash,
      profile_id: REVISION_REVIEW.profileId,
      profile_version: REVISION_REVIEW.profileVersion,
      assignee_bindings: [],
    },
    ...overrides,
  } as CommandRequest;
}

function revisionDraft(overrides: Record<string, unknown> = {}) {
  return {
    ...REVISION_SCOPE,
    schema_version: 1,
    review: REVISION_REVIEW,
    choices: [],
    original: revisionCommand(),
    status: "unknown",
    ...overrides,
  };
}

function command(overrides: Partial<CommandRequest> = {}): CommandRequest {
  return {
    command_id: "60000000-0000-0000-0000-000000000001",
    workspace_id: SCOPE.workspace_id,
    command_type: "ConfirmPlanningProposal",
    actor: { type: "user", id: SCOPE.principal_id },
    target_type: "planning_request",
    target_id: SCOPE.request_id,
    expected_version: 8,
    idempotency_key: "70000000-0000-0000-0000-000000000001",
    correlation_id: "80000000-0000-0000-0000-000000000001",
    payload: {
      proposal_revision: 2,
      proposal_content_hash: REVIEW.proposalContentHash,
      profile_id: REVIEW.profileId,
      profile_version: 3,
      assignee_bindings: [{
        target_type: "subtask",
        target_key: "prepare",
        assignee_kind: "profile",
        assignee_id: REVIEW.profileId,
        profile_revision: 1,
        expected_binding_version: null,
      }],
    },
    ...overrides,
  } as CommandRequest;
}

function draft(
  overrides: Partial<Extract<PlanningConfirmationDraft, { kind: "create" }>> = {},
): Extract<PlanningConfirmationDraft, { kind: "create" }> {
  return {
    ...SCOPE,
    schema_version: 1,
    review: REVIEW,
    choices: [[JSON.stringify(["subtask", "prepare"]), {
      ...profileChoice,
      profileRef: { ...profileChoice.profileRef },
    }]],
    original: null,
    status: "idle",
    ...overrides,
  };
}

describe("scoped native planning draft storage", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    window.localStorage.clear();
  });

  it.each([SCOPE, REVISION_SCOPE])("retains failed planning feedback without a proposal in $kind scope", (scope) => {
    const original = command({
      command_type: "ReviseFailedPlanningRequest",
      target_id: scope.request_id,
      payload: {
        failed_planning_run_id: "c0000000-0000-0000-0000-000000000001",
        expected_failed_planning_run_version: 3,
        expected_work_context_version: 4,
        work_context_revision_id: "d0000000-0000-0000-0000-000000000001",
        work_context_content_hash: "a".repeat(64),
        feedback: "Retain the original requirements",
      },
    });
    for (const status of ["pending", "unknown", "applied", "rejected"] as const) {
      const input: PlanningConfirmationDraft = { ...scope, schema_version: 1, review: null, choices: [], original, status };
      expect(savePlanningDraft(scope, input)).toEqual({ ok: true });
      expect(loadPlanningDraft(scope)).toEqual({ ok: true, draft: input });
    }
  });

  it.each([SCOPE, REVISION_SCOPE])("strictly validates every failed planning feedback field in $kind scope", (scope) => {
    const payload = {
      failed_planning_run_id: "c0000000-0000-0000-0000-000000000001",
      expected_failed_planning_run_version: 3, expected_work_context_version: 4,
      work_context_revision_id: "d0000000-0000-0000-0000-000000000001",
      work_context_content_hash: "a".repeat(64), feedback: "Keep history",
    };
    const original = command({ command_type: "ReviseFailedPlanningRequest", target_id: scope.request_id, payload });
    const pending: PlanningConfirmationDraft = { ...scope, schema_version: 1, review: null, choices: [], original, status: "unknown" };
    const damaged: Record<string, unknown>[] = [{ ...payload, closed: true }];
    for (const field of Object.keys(payload)) {
      const missing: Record<string, unknown> = { ...payload };
      delete missing[field];
      damaged.push(missing);
    }
    for (const field of ["expected_failed_planning_run_version", "expected_work_context_version"]) {
      for (const value of [0, -1, 1.5, "1", true, null]) damaged.push({ ...payload, [field]: value });
    }
    for (const field of ["failed_planning_run_id", "work_context_revision_id"]) {
      for (const value of ["bad", "", 1, null]) damaged.push({ ...payload, [field]: value });
    }
    for (const value of ["A".repeat(64), "a".repeat(63), "g".repeat(64), null]) damaged.push({ ...payload, work_context_content_hash: value });
    for (const value of ["", " \n ", 123, null]) damaged.push({ ...payload, feedback: value });
    for (const value of damaged) {
      expect(savePlanningDraft(scope, { ...pending, original: { ...original, payload: value } }).ok).toBe(false);
    }
    for (const changes of [{ workspace_id: "foreign" }, { target_id: "foreign" }, { target_type: "task" },
      { actor: { type: "user", id: "foreign" } }, { actor: { type: "system", id: scope.principal_id } },
      { expected_version: 0 }, { correlation_id: "bad" }, { closed: true }]) {
      window.localStorage.setItem(planningDraftKey(scope), JSON.stringify({ ...pending, original: { ...original, ...changes } }));
      expect(loadPlanningDraft(scope).ok).toBe(false);
    }
    for (const field of Object.keys(original)) {
      const incomplete = { ...original } as Record<string, unknown>;
      delete incomplete[field];
      window.localStorage.setItem(planningDraftKey(scope), JSON.stringify({ ...pending, original: incomplete }));
      expect(loadPlanningDraft(scope).ok).toBe(false);
    }
  });

  it("round-trips a detached safe draft and keeps an unknown original after reload", () => {
    const original = command();
    const input = draft({ original, status: "unknown" });
    const saved = savePlanningDraft(SCOPE, input);
    const mutatedChoice = input.choices[0]?.[1];
    if (mutatedChoice?.kind === "profile") mutatedChoice.profileRef.revision = 9;

    expect(saved).toEqual({ ok: true });
    const loaded = loadPlanningDraft(SCOPE);
    expect(loaded).toEqual({ ok: true, draft: { ...draft(), original, status: "unknown" } });
    if (loaded.ok && loaded.draft) {
      expect(loaded.draft.choices[0]![1]).toEqual(profileChoice);
      expect(loaded.draft.original).toEqual(original);
    }
  });

  it.each([
    ["foreign workspace", { ...SCOPE, workspace_id: "foreign-workspace" }],
    ["foreign request", { ...SCOPE, request_id: "foreign-request" }],
    ["foreign principal", { ...SCOPE, principal_id: "foreign-principal" }],
  ] as const)("does not hydrate a %s draft", (_name, foreignScope) => {
    savePlanningDraft(SCOPE, draft());
    expect(loadPlanningDraft(foreignScope)).toEqual({ ok: true, draft: null });
  });

  it("rejects duplicate or noncanonical assignment keys during hydration", () => {
    const bad = draft({ choices: [
      [JSON.stringify(["subtask", "prepare"]), profileChoice],
      [JSON.stringify(["subtask", "prepare"]), profileChoice],
    ] });
    window.localStorage.setItem(planningDraftKey(SCOPE), JSON.stringify(bad));
    expect(loadPlanningDraft(SCOPE)).toEqual({ ok: false, reason: "本地确认草稿不可用" });

    window.localStorage.setItem(planningDraftKey(SCOPE), JSON.stringify(draft({
      choices: [["[\"subtask\", \"prepare\"]", profileChoice]],
    })));
    expect(loadPlanningDraft(SCOPE)).toEqual({ ok: false, reason: "本地确认草稿不可用" });
  });

  it.each([
    ["private top-level authority", { session_token: "secret" }],
    ["wrong actor", { actor: { type: "user", id: "another-person" } }],
    ["wrong target", { target_id: "another-request" }],
    ["unknown confirmation payload field", { payload: { ...command().payload, admin: true } }],
    ["wrong reviewed hash", { payload: { ...command().payload, proposal_content_hash: "b".repeat(64) } }],
  ] as const)("refuses %s instead of hydrating it as write authority", (_name, changed) => {
    const bad = draft({ original: command(changed as Partial<CommandRequest>), status: "unknown" });
    window.localStorage.setItem(planningDraftKey(SCOPE), JSON.stringify(bad));
    expect(loadPlanningDraft(SCOPE)).toEqual({ ok: false, reason: "本地确认草稿不可用" });
  });

  it("accepts pre-proposal cancellation and clarification without a review anchor", () => {
    const cancellation = command({
      command_type: "CancelPlanningRequest",
      expected_version: 4,
      payload: {},
    });
    const clarification = command({
      command_type: "AnswerPlanningClarification",
      expected_version: 4,
      payload: { answers: [{ clarification_id: "90000000-0000-0000-0000-000000000001", answer: "保留" }] },
    });
    expect(savePlanningDraft(SCOPE, draft({ review: null, choices: [], original: cancellation, status: "pending" })))
      .toEqual({ ok: true });
    expect(loadPlanningDraft(SCOPE)).toMatchObject({ ok: true, draft: { review: null, original: cancellation } });
    expect(savePlanningDraft(SCOPE, draft({ review: null, choices: [], original: clarification, status: "unknown" })))
      .toEqual({ ok: true });
  });

  it("retains a rejected original when explicit review advances the local anchor", () => {
    const nextReview = { ...REVIEW, requestVersion: 9, proposalRevision: 3, proposalContentHash: "b".repeat(64) };
    const saved = savePlanningDraft(SCOPE, draft({
      review: nextReview,
      original: command(),
      status: "rejected",
    }));

    expect(saved).toEqual({ ok: true });
    expect(loadPlanningDraft(SCOPE)).toMatchObject({
      ok: true,
      draft: { review: nextReview, original: command(), status: "rejected" },
    });
  });

  it("turns storage read and write failures into safe results", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("private storage detail"); });
    expect(loadPlanningDraft(SCOPE)).toEqual({ ok: false, reason: "本地确认草稿不可用" });
    vi.restoreAllMocks();
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("private storage detail"); });
    expect(savePlanningDraft(SCOPE, draft())).toEqual({ ok: false, reason: "无法保存本地确认草稿" });
  });

  it("keeps the existing create key stable and adds a root-specific revision key", () => {
    expect(planningDraftKey(SCOPE)).toBe(
      "sidera.native-planning.confirmation.v1:20000000-0000-0000-0000-000000000001:30000000-0000-0000-0000-000000000001:principal-current",
    );
    expect(planningDraftKey(REVISION_SCOPE as never)).toBe(
      "sidera.native-planning.confirmation.v1:20000000-0000-0000-0000-000000000001:40000000-0000-0000-0000-000000000001:principal-current:revision:50000000-0000-0000-0000-000000000001",
    );
  });

  it("round-trips an unknown revision original unchanged when an all-REUSE plan has no new bindings", () => {
    const savedDraft = revisionDraft();
    const raw = JSON.stringify(savedDraft);

    expect(savePlanningDraft(REVISION_SCOPE as never, savedDraft as never)).toEqual({ ok: true });
    expect(window.localStorage.getItem(planningDraftKey(REVISION_SCOPE as never))).toBe(raw);
    const loaded = loadPlanningDraft(REVISION_SCOPE as never);

    expect(loaded).toEqual({ ok: true, draft: savedDraft });
    if (loaded.ok && loaded.draft) {
      expect(loaded.draft.original).toEqual(revisionCommand());
      expect(Object.isFrozen(loaded.draft)).toBe(true);
      expect(Object.isFrozen(loaded.draft.original)).toBe(true);
      expect((loaded.draft.original?.payload as Record<string, unknown>).assignee_bindings).toEqual([]);
    }
  });

  it.each([["A", "a"], ["a-b", "a_b"]] as const)(
    "preserves a mapped revision original with keys %s/%s",
    (left, right) => {
      const targets: ProposalAssignmentTarget[] = [left, right].map((key) => ({
        kind: "subtask",
        key,
        label: key,
        subtaskKey: null,
        strategy: "create",
        existingId: null,
      }));
      const human: AssigneeValue = {
        kind: "human",
        id: REVISION_SCOPE.principal_id,
        profileRef: null,
      };
      const choices = new Map(targets.map((target) => [
        assignmentTargetKey(target.kind, target.key), human,
      ]));
      const assigneeBindings = toPlanningAssigneeChoices({
        targets,
        choices,
        session: {
          session_id: "d0000000-0000-0000-0000-000000000001",
          workspace_id: REVISION_SCOPE.workspace_id,
          principal_id: REVISION_SCOPE.principal_id,
          expires_at: "2099-01-01T00:00:00Z",
        },
        permittedProfiles: [],
        reusedBindings: new Map(),
      });
      expect(assigneeBindings.map((binding) => binding.target_key)).toEqual([left, right]);
      const targetPins = targets.map((target) => ({
        existing_id: null,
        identity_strategy: target.strategy,
        key: target.key,
        kind: target.kind,
        parent: null,
      }));
      targetPins.sort((a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b)));
      const review = {
        ...REVISION_REVIEW,
        targetsFingerprint: JSON.stringify(targetPins),
        reusedBindingsFingerprint: "[]",
      };
      const original = revisionCommand({
        payload: {
          ...revisionCommand().payload,
          assignee_bindings: assigneeBindings,
        },
      });
      const pending: PlanningConfirmationDraft = {
        ...REVISION_SCOPE,
        schema_version: 1,
        review,
        choices: [...choices],
        original,
        status: "pending",
      };

      expect(savePlanningDraft(REVISION_SCOPE, pending)).toEqual({ ok: true });
      const restored = loadPlanningDraft(REVISION_SCOPE);
      expect(restored.ok).toBe(true);
      if (!restored.ok || restored.draft === null) throw new Error("Missing draft");
      expect(JSON.stringify(restored.draft.original)).toBe(JSON.stringify(original));
    },
  );

  it("rejects a revision draft with a foreign root or private extra field", () => {
    window.localStorage.setItem(
      planningDraftKey(REVISION_SCOPE as never),
      JSON.stringify(revisionDraft({ root_task_id: "another-root" })),
    );
    expect(loadPlanningDraft(REVISION_SCOPE as never)).toEqual({
      ok: false,
      reason: "本地确认草稿不可用",
    });

    window.localStorage.setItem(
      planningDraftKey(REVISION_SCOPE as never),
      JSON.stringify({ ...revisionDraft(), private_token: "secret" }),
    );
    expect(loadPlanningDraft(REVISION_SCOPE as never)).toEqual({
      ok: false,
      reason: "本地确认草稿不可用",
    });
  });

  it("rejects a pending revision original whose payload no longer matches its saved review", () => {
    window.localStorage.setItem(
      planningDraftKey(REVISION_SCOPE as never),
      JSON.stringify(revisionDraft({
        original: revisionCommand({
          payload: {
            ...revisionCommand().payload,
            expected_diff_hash: "e".repeat(64),
          },
        }),
        status: "pending",
      })),
    );

    expect(loadPlanningDraft(REVISION_SCOPE as never)).toEqual({
      ok: false,
      reason: "本地确认草稿不可用",
    });
  });
});
