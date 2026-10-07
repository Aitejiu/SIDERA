import { describe, expect, it } from "vitest";

import { PROFILE, PROFILE_REF, profileAt, WORKSPACE_ID } from "@/features/agents/testFixtures";
import type { LocalSession } from "@/features/local-session/api";
import type { PlanningProposal } from "@/features/planning/types";
import { AssignmentUnavailableError } from "./types";
import type {
  AgentBindingView,
  AssigneeValue,
  PlanningAssigneeChoice,
  ProposalAssignmentTarget,
  RevisionDiff,
} from "./types";
import { assignmentTargetKey } from "./api";
import {
  extractInitialAssignmentTargets,
  extractRevisionAssignmentTargets,
  toPlanningAssigneeChoices,
} from "./proposalAssignments";

const PRINCIPAL_ID = "principal-current";
const REVIEW_TASK_ID = "30000000-0000-0000-0000-000000000001";
const INSPECT_ITEM_ID = "40000000-0000-0000-0000-000000000001";
const SESSION: LocalSession = {
  session_id: "50000000-0000-0000-0000-000000000001",
  workspace_id: WORKSPACE_ID,
  principal_id: PRINCIPAL_ID,
  expires_at: "2099-01-01T00:00:00Z",
};

function proposal(display: unknown): PlanningProposal {
  return { display } as PlanningProposal;
}

function initialDisplay() {
  return {
    subtasks: [
      { key: "review", title: "Review" },
      { key: "build", title: "Build" },
    ],
    worker_items: [
      { key: "inspect", subtask_key: "review", title: "Inspect" },
      { key: "implement", subtask_key: "build", title: "Implement" },
      { key: "check", subtask_key: "build", title: "Check" },
    ],
  };
}

function revisionDiff(
  subtaskChanges: unknown[] = [
    { target_subtask_key: "review", strategy: "reuse", existing_task_id: REVIEW_TASK_ID, path: "/subtasks/review" },
    { target_subtask_key: "build", strategy: "create", existing_task_id: null, path: "/subtasks/build" },
  ],
  workerItemChanges: unknown[] = [
    { target_worker_item_key: "inspect", strategy: "reuse", existing_worker_item_id: INSPECT_ITEM_ID, to_subtask_key: "review", path: "/subtasks/review/worker_items/inspect" },
    { target_worker_item_key: "implement", strategy: "create", existing_worker_item_id: null, to_subtask_key: "build", path: "/subtasks/build/worker_items/implement" },
    { target_worker_item_key: "check", strategy: "derive", existing_worker_item_id: null, to_subtask_key: "build", path: "/subtasks/build/worker_items/check" },
  ],
): RevisionDiff {
  return { subtask_changes: subtaskChanges, worker_item_changes: workerItemChanges } as RevisionDiff;
}

function profileChoice(revision = PROFILE_REF.revision): AssigneeValue {
  return {
    kind: "profile",
    id: PROFILE_REF.profile_id,
    profileRef: { ...PROFILE_REF, revision },
  };
}

function savedProfileBinding(
  targetType: "subtask" | "worker_item",
  targetId: string,
  revision = PROFILE_REF.revision,
): AgentBindingView {
  return {
    workspace_id: WORKSPACE_ID,
    target_type: targetType,
    target_id: targetId,
    assignee: { kind: "profile", id: PROFILE_REF.profile_id },
    profile_ref: { ...PROFILE_REF, revision },
    version: 2,
    created_at: "2026-10-06T12:00:00Z",
    changed_at: "2026-10-06T12:00:00Z",
    runnable: false,
    not_runnable_reasons: [],
  };
}

function choiceMap(targets: ProposalAssignmentTarget[], value: AssigneeValue) {
  return new Map(targets.map((target) => [
    assignmentTargetKey(target.kind, target.key),
    value,
  ]));
}

function choiceSummary(choices: PlanningAssigneeChoice[]) {
  return choices.map(({ target_type, target_key, assignee_kind, assignee_id, profile_revision, expected_binding_version }) => ({
    target_type,
    target_key,
    assignee_kind,
    assignee_id,
    profile_revision,
    expected_binding_version,
  }));
}

describe("proposal assignment targets", () => {
  it("extracts actual initial keys and uses titles only as labels", () => {
    const targets = extractInitialAssignmentTargets(proposal(initialDisplay()));

    expect(targets).toEqual([
      { kind: "subtask", key: "review", label: "Review", subtaskKey: null, strategy: "create", existingId: null },
      { kind: "subtask", key: "build", label: "Build", subtaskKey: null, strategy: "create", existingId: null },
      { kind: "worker_item", key: "inspect", label: "Inspect", subtaskKey: "review", strategy: "create", existingId: null },
      { kind: "worker_item", key: "implement", label: "Implement", subtaskKey: "build", strategy: "create", existingId: null },
      { kind: "worker_item", key: "check", label: "Check", subtaskKey: "build", strategy: "create", existingId: null },
    ]);
  });

  it.each([
    ["missing subtask key", { subtasks: [{ title: "Review" }], worker_items: [] }],
    ["non-string subtask key", { subtasks: [{ key: 17, title: "Review" }], worker_items: [] }],
    ["blank subtask key", { subtasks: [{ key: "  ", title: "Review" }], worker_items: [] }],
    ["untrimmed subtask key", { subtasks: [{ key: " review", title: "Review" }], worker_items: [] }],
    ["duplicate subtask key", { subtasks: [{ key: "review" }, { key: "review" }], worker_items: [] }],
    ["duplicate worker key", { subtasks: [{ key: "review" }], worker_items: [{ key: "inspect", subtask_key: "review" }, { key: "inspect", subtask_key: "review" }] }],
    ["unknown worker parent", { subtasks: [{ key: "review" }], worker_items: [{ key: "inspect", subtask_key: "missing" }] }],
    ["wrong array shape", { subtasks: {}, worker_items: [] }],
    ["missing display arrays", { subtasks: [{ key: "review" }] }],
  ] as const)("refuses initial targets with %s instead of synthesizing identity", (_name, display) => {
    expect(() => extractInitialAssignmentTargets(proposal(display)))
      .toThrow(AssignmentUnavailableError);
  });

  it("keeps the subtask and WorkerItem key namespaces independent", () => {
    expect(extractInitialAssignmentTargets(proposal({
      subtasks: [{ key: "shared" }],
      worker_items: [{ key: "shared", subtask_key: "shared" }],
    }))).toHaveLength(2);
  });

  it("extracts all typed revision targets and retains their paths and strategies", () => {
    expect(extractRevisionAssignmentTargets(revisionDiff())).toEqual([
      { kind: "subtask", key: "review", label: "/subtasks/review", subtaskKey: null, strategy: "reuse", existingId: REVIEW_TASK_ID },
      { kind: "subtask", key: "build", label: "/subtasks/build", subtaskKey: null, strategy: "create", existingId: null },
      { kind: "worker_item", key: "inspect", label: "/subtasks/review/worker_items/inspect", subtaskKey: "review", strategy: "reuse", existingId: INSPECT_ITEM_ID },
      { kind: "worker_item", key: "implement", label: "/subtasks/build/worker_items/implement", subtaskKey: "build", strategy: "create", existingId: null },
      { kind: "worker_item", key: "check", label: "/subtasks/build/worker_items/check", subtaskKey: "build", strategy: "derive", existingId: null },
    ]);
  });

  it.each([
    ["unknown strategy", [{ target_subtask_key: "review", strategy: "maybe", existing_task_id: null, path: "/review" }], []],
    ["reuse without an existing id", [{ target_subtask_key: "review", strategy: "reuse", existing_task_id: null, path: "/review" }], []],
    ["create with an existing id", [{ target_subtask_key: "review", strategy: "create", existing_task_id: REVIEW_TASK_ID, path: "/review" }], []],
    ["duplicate key", [{ target_subtask_key: "review", strategy: "create", existing_task_id: null, path: "/one" }, { target_subtask_key: "review", strategy: "derive", existing_task_id: null, path: "/two" }], []],
    ["WorkerItem under an unknown target subtask", [{ target_subtask_key: "review", strategy: "create", existing_task_id: null, path: "/review" }], [{ target_worker_item_key: "inspect", strategy: "create", existing_worker_item_id: null, to_subtask_key: "missing", path: "/inspect" }]],
  ] as const)("refuses revision targets with %s", (_name, subtasks, workerItems) => {
    expect(() => extractRevisionAssignmentTargets(revisionDiff([...subtasks], [...workerItems])))
      .toThrow(AssignmentUnavailableError);
  });
});

describe("planning assignee mapping", () => {
  it("maps one exact Profile pin independently to all five initial targets", () => {
    const targets = extractInitialAssignmentTargets(proposal(initialDisplay()));
    const choices = toPlanningAssigneeChoices({
      targets,
      choices: choiceMap(targets, profileChoice()),
      session: SESSION,
      permittedProfiles: [PROFILE],
      reusedBindings: new Map(),
    });

    expect(choiceSummary(choices)).toEqual([
      { target_type: "subtask", target_key: "build", assignee_kind: "profile", assignee_id: PROFILE_REF.profile_id, profile_revision: 1, expected_binding_version: null },
      { target_type: "subtask", target_key: "review", assignee_kind: "profile", assignee_id: PROFILE_REF.profile_id, profile_revision: 1, expected_binding_version: null },
      { target_type: "worker_item", target_key: "check", assignee_kind: "profile", assignee_id: PROFILE_REF.profile_id, profile_revision: 1, expected_binding_version: null },
      { target_type: "worker_item", target_key: "implement", assignee_kind: "profile", assignee_id: PROFILE_REF.profile_id, profile_revision: 1, expected_binding_version: null },
      { target_type: "worker_item", target_key: "inspect", assignee_kind: "profile", assignee_id: PROFILE_REF.profile_id, profile_revision: 1, expected_binding_version: null },
    ]);
    for (const choice of choices) {
      expect(Object.keys(choice).sort()).toEqual([
        "assignee_id",
        "assignee_kind",
        "expected_binding_version",
        "profile_revision",
        "target_key",
        "target_type",
      ]);
    }
  });

  it("changes only the selected parent owner and leaves every WorkerItem independent", () => {
    const targets = extractInitialAssignmentTargets(proposal(initialDisplay()));
    const choices = choiceMap(targets, profileChoice());
    const before = toPlanningAssigneeChoices({
      targets,
      choices,
      session: SESSION,
      permittedProfiles: [PROFILE],
      reusedBindings: new Map(),
    });
    choices.set(assignmentTargetKey("subtask", "build"), {
      kind: "human",
      id: PRINCIPAL_ID,
      profileRef: null,
    });
    const after = toPlanningAssigneeChoices({
      targets,
      choices,
      session: SESSION,
      permittedProfiles: [PROFILE],
      reusedBindings: new Map(),
    });

    expect(after.find((item) => item.target_type === "subtask" && item.target_key === "build"))
      .toMatchObject({ assignee_kind: "human", assignee_id: PRINCIPAL_ID, profile_revision: null });
    expect(after.filter((item) => item.target_type === "worker_item"))
      .toEqual(before.filter((item) => item.target_type === "worker_item"));
  });

  it("requires every new target choice and rejects unknown draft keys", () => {
    const targets = extractInitialAssignmentTargets(proposal(initialDisplay()));
    const parentOnly = new Map([[assignmentTargetKey("subtask", "review"), profileChoice()]]);
    expect(() => toPlanningAssigneeChoices({
      targets,
      choices: parentOnly,
      session: SESSION,
      permittedProfiles: [PROFILE],
      reusedBindings: new Map(),
    })).toThrow(AssignmentUnavailableError);

    const allChoices = choiceMap(targets, profileChoice());
    allChoices.set(assignmentTargetKey("subtask", "retired"), profileChoice());
    expect(() => toPlanningAssigneeChoices({
      targets,
      choices: allChoices,
      session: SESSION,
      permittedProfiles: [PROFILE],
      reusedBindings: new Map(),
    })).toThrow(AssignmentUnavailableError);
  });

  it("requires current human identity and an exact enabled permitted Profile ref", () => {
    const target = extractInitialAssignmentTargets(proposal({
      subtasks: [{ key: "review" }],
      worker_items: [],
    }));
    const wrongHuman = new Map([[assignmentTargetKey("subtask", "review"), {
      kind: "human",
      id: "another-principal",
      profileRef: null,
    } satisfies AssigneeValue]]);
    expect(() => toPlanningAssigneeChoices({
      targets: target,
      choices: wrongHuman,
      session: SESSION,
      permittedProfiles: [PROFILE],
      reusedBindings: new Map(),
    })).toThrow(AssignmentUnavailableError);

    for (const value of [
      { kind: "profile", id: PROFILE_REF.profile_id, profileRef: { ...PROFILE_REF, workspace_id: "foreign" } },
      { kind: "profile", id: PROFILE_REF.profile_id, profileRef: { ...PROFILE_REF, revision: 2 } },
    ] satisfies AssigneeValue[]) {
      expect(() => toPlanningAssigneeChoices({
        targets: target,
        choices: new Map([[assignmentTargetKey("subtask", "review"), value]]),
        session: SESSION,
        permittedProfiles: [PROFILE],
        reusedBindings: new Map(),
      })).toThrow(AssignmentUnavailableError);
    }
    expect(() => toPlanningAssigneeChoices({
      targets: target,
      choices: new Map([[assignmentTargetKey("subtask", "review"), profileChoice()]]),
      session: SESSION,
      permittedProfiles: [{ ...PROFILE, status: { ...PROFILE.status, enabled: false } }],
      reusedBindings: new Map(),
    })).toThrow(AssignmentUnavailableError);
  });

  it("omits unchanged REUSE bindings and uses binding version for an explicit identical reuse", () => {
    const targets = extractRevisionAssignmentTargets(revisionDiff());
    const reusedBindings = new Map([
      [assignmentTargetKey("subtask", REVIEW_TASK_ID), savedProfileBinding("subtask", REVIEW_TASK_ID, 1)],
      [assignmentTargetKey("worker_item", INSPECT_ITEM_ID), savedProfileBinding("worker_item", INSPECT_ITEM_ID, 1)],
    ]);
    const newChoices = new Map([
      [assignmentTargetKey("subtask", "build"), profileChoice(2)],
      [assignmentTargetKey("worker_item", "implement"), profileChoice(2)],
      [assignmentTargetKey("worker_item", "check"), profileChoice(2)],
    ]);
    const omittedReuse = toPlanningAssigneeChoices({
      targets,
      choices: newChoices,
      session: SESSION,
      permittedProfiles: [profileAt(2)],
      reusedBindings,
    });
    expect(choiceSummary(omittedReuse)).toEqual([
      { target_type: "subtask", target_key: "build", assignee_kind: "profile", assignee_id: PROFILE_REF.profile_id, profile_revision: 2, expected_binding_version: null },
      { target_type: "worker_item", target_key: "check", assignee_kind: "profile", assignee_id: PROFILE_REF.profile_id, profile_revision: 2, expected_binding_version: null },
      { target_type: "worker_item", target_key: "implement", assignee_kind: "profile", assignee_id: PROFILE_REF.profile_id, profile_revision: 2, expected_binding_version: null },
    ]);

    newChoices.set(assignmentTargetKey("subtask", "review"), profileChoice(1));
    newChoices.set(assignmentTargetKey("worker_item", "inspect"), profileChoice(1));
    const explicitReuse = toPlanningAssigneeChoices({
      targets,
      choices: newChoices,
      session: SESSION,
      permittedProfiles: [profileAt(2)],
      reusedBindings,
    });
    expect(choiceSummary(explicitReuse).filter((item) => item.expected_binding_version !== null)).toEqual([
      { target_type: "subtask", target_key: "review", assignee_kind: "profile", assignee_id: PROFILE_REF.profile_id, profile_revision: 1, expected_binding_version: 2 },
      { target_type: "worker_item", target_key: "inspect", assignee_kind: "profile", assignee_id: PROFILE_REF.profile_id, profile_revision: 1, expected_binding_version: 2 },
    ]);
  });

  it("refuses changed or missing saved reuse facts without upgrading historical pins", () => {
    const targets = extractRevisionAssignmentTargets(revisionDiff());
    const newChoices = new Map([
      [assignmentTargetKey("subtask", "build"), profileChoice(2)],
      [assignmentTargetKey("worker_item", "implement"), profileChoice(2)],
      [assignmentTargetKey("worker_item", "check"), profileChoice(2)],
    ]);
    const reusedBindings = new Map([
      [assignmentTargetKey("subtask", REVIEW_TASK_ID), savedProfileBinding("subtask", REVIEW_TASK_ID, 1)],
      [assignmentTargetKey("worker_item", INSPECT_ITEM_ID), savedProfileBinding("worker_item", INSPECT_ITEM_ID, 1)],
    ]);
    const changedOwner = new Map(newChoices);
    changedOwner.set(assignmentTargetKey("subtask", "review"), {
      kind: "human",
      id: PRINCIPAL_ID,
      profileRef: null,
    });

    expect(() => toPlanningAssigneeChoices({
      targets,
      choices: changedOwner,
      session: SESSION,
      permittedProfiles: [profileAt(2)],
      reusedBindings,
    })).toThrow(AssignmentUnavailableError);
    expect(() => toPlanningAssigneeChoices({
      targets,
      choices: newChoices,
      session: SESSION,
      permittedProfiles: [profileAt(2)],
      reusedBindings: new Map(),
    })).toThrow(AssignmentUnavailableError);
  });
});
