import { afterEach, expect, it, vi } from "vitest";

import type { ProfileRef } from "@/features/agents/types";
import type {
  AgentBindingView,
  AgentExecutionView,
  CommandRequest,
  NativeWorkerContextView,
} from "@/features/assignments/types";
import type { WorkerWorkspace } from "@/features/worker-items/types";
import {
  isNativeHandoffProposal,
  type CommandResponse,
  type NativeHandoffProposal,
  type NativeHandoffTransition,
} from "@/features/worker-items/types";
import {
  handoffDraftKey,
  loadHandoffDraft,
  mapHandoffActionResult,
  reviewHandoffSource,
  reviewPreparedHandoff,
  samePreparedHandoff,
  saveHandoffDraft,
  type HandoffDraft,
  type HandoffScope,
} from "./profileHandoff";

const workspaceId = "20000000-0000-0000-0000-000000000001";
const sourceProfileId = "50000000-0000-0000-0000-000000000001";
const targetProfileId = "50000000-0000-0000-0000-000000000002";
const workerItemId = "40000000-0000-0000-0000-000000000001";
const rootTaskId = "30000000-0000-0000-0000-000000000001";
const subtaskId = "30000000-0000-0000-0000-000000000002";
const executionId = "60000000-0000-0000-0000-000000000001";
const sessionId = "70000000-0000-0000-0000-000000000001";
const workerId = "80000000-0000-0000-0000-000000000001";
const runId = "90000000-0000-0000-0000-000000000001";
const snapshotId = "90000000-0000-0000-0000-000000000001";
const principalId = "principal-current";
const handoffScope: HandoffScope = {
  workspace_id: workspaceId,
  principal_id: principalId,
  worker_item_id: workerItemId,
};
const sourceProfileRef: ProfileRef = {
  workspace_id: workspaceId,
  profile_id: sourceProfileId,
  revision: 1,
};
const targetProfileRef: ProfileRef = {
  workspace_id: workspaceId,
  profile_id: targetProfileId,
  revision: 1,
};
const handoffTarget = {
  kind: "profile" as const,
  id: targetProfileId,
  profileRef: targetProfileRef,
};

const proposal: NativeHandoffProposal = {
  id: "a0000000-0000-0000-0000-000000000001",
  proposal_schema_version: 2,
  workspace_id: workspaceId,
  root_task_id: "30000000-0000-0000-0000-000000000001",
  subtask_id: "30000000-0000-0000-0000-000000000002",
  worker_item_id: "40000000-0000-0000-0000-000000000001",
  source_worker_item_version: 7,
  version: 1,
  proposal_hash: "a".repeat(64),
  status: "prepared",
  stale_reason: null,
  created_at: "2026-10-07T00:00:00Z",
  expires_at: "2026-10-07T00:15:00Z",
  confirmed_at: null,
  cancelled_at: null,
  source_execution_id: "60000000-0000-0000-0000-000000000001",
  source_session_id: "70000000-0000-0000-0000-000000000001",
  source_run_id: runId,
  source_binding_version: 2,
  source_profile_ref: { workspace_id: workspaceId, profile_id: sourceProfileId, revision: 1 },
  target_assignee: { kind: "profile", id: targetProfileId },
  target_profile_ref: targetProfileRef,
  target_worker_id: null,
  handoff_reason: "Change the implementation owner",
  handoff_instruction: "Continue the validation step",
  context: {
    source_package_id: "b0000000-0000-0000-0000-000000000001",
    source_package_hash: "b".repeat(64),
    source_context_version: 3,
    target_package_id: null,
    target_package_hash: null,
    target_context_version: 4,
  },
  plan_base: {
    snapshot_id: "90000000-0000-0000-0000-000000000001",
    generation: 2,
    head_version: 6,
  },
};

const sourceBinding: AgentBindingView = {
  workspace_id: workspaceId,
  target_type: "worker_item",
  target_id: workerItemId,
  assignee: { kind: "profile", id: sourceProfileId },
  profile_ref: sourceProfileRef,
  version: 2,
  runnable: true,
  not_runnable_reasons: [],
  created_at: "2026-10-07T00:00:00Z",
  changed_at: "2026-10-07T00:00:00Z",
};

const sourceExecution: AgentExecutionView = {
  workspace_id: workspaceId,
  execution_id: executionId,
  worker_item_id: workerItemId,
  root_task_id: rootTaskId,
  subtask_id: subtaskId,
  profile_ref: sourceProfileRef,
  binding_version: 2,
  run_id: runId,
  event_cursor: 5,
  status: "running",
  link_status: "accepted",
  updated_at: "2026-10-07T00:00:00Z",
};

const sourceWorkspace: WorkerWorkspace = {
  workspace_id: workspaceId,
  worker_item: {
    id: workerItemId,
    stable_key: "native-worker",
    title: "Native worker",
    responsibility: "Implement the callback contract.",
    root_task_id: rootTaskId,
    status: "active",
    context_version: 4,
    acknowledged_context_version: 4,
    version: 7,
    updated_at: "2026-10-07T00:00:00Z",
  },
  current_placement: {
    worker_item_key: "native-worker",
    subtask_key: "callback-idempotency",
    subtask_task_id: subtaskId,
    role: "implementer",
    capability_requirements: [],
    plan_generation: 2,
    plan_snapshot_id: snapshotId,
  },
  placement_history: [],
  current_runtime: {
    adapter_type: "agent-run",
    attempt: 1,
    execution_id: executionId,
    execution_status: "running",
    fencing_token: 2,
    input_mode: "queued",
    lease_id: "lease-1",
    session_id: sessionId,
    session_status: "active",
    session_version: 3,
    task_id: subtaskId,
    worker_display_name: "Source Profile R1",
    worker_id: workerId,
  },
  execution_history: [{
    execution_id: executionId,
    task_id: subtaskId,
    status: "running",
    attempt: 1,
    worker_id: workerId,
    session_id: sessionId,
    failure_code: null,
    started_at: "2026-10-07T00:00:00Z",
    finished_at: null,
    created_at: "2026-10-07T00:00:00Z",
    updated_at: "2026-10-07T00:00:00Z",
    version: 5,
    capability_requirements: [],
    adapter_preferences: ["agent-run"],
    adapter_forbidden: [],
    scheduling_status: "assigned",
    waiting_reason: null,
    waiting_details_summary: null,
    requires_operator: false,
    blocked_since: null,
    pinned_worker_id: null,
    assigned_worker_id: workerId,
    pinned_worker_summary: null,
    assigned_worker_summary: null,
    binding_summary: null,
  }],
  current_step: "Validate proposal",
  next_action: "Continue validation",
  latest_checkpoint: null,
  usage: { input_tokens: 0, output_tokens: 0, cached_tokens: 0, cost_microunits: 0 },
  artifacts: [],
  evidence: [],
  can_intervene: false,
  read_only_reason: null,
  intervention_context: null,
  trace_descriptor: null,
  last_change_cursor: 0,
  as_of: "2026-10-07T00:00:00Z",
};

const sourceContext: NativeWorkerContextView = {
  workspace_id: workspaceId,
  worker_item_id: workerItemId,
  execution_mode: "native_profile",
  worker_item_version: 7,
  binding: sourceBinding,
  execution: sourceExecution,
  execution_version: 5,
  context: {
    worker_item_id: workerItemId,
    execution_mode: "native_profile",
    execution_id: executionId,
    session_id: sessionId,
    session_version: 3,
    worker_id: workerId,
    binding_version: 2,
    run_id: runId,
    plan_generation: 2,
    context_version: 4,
  },
  input_enabled: false,
  read_only_reason: "input_not_available",
  run: {
    state: "running",
    input_open: false,
    profile_ref: sourceProfileRef,
    runtime_ref: { workspace_id: workspaceId, runtime_id: "b0000000-0000-0000-0000-000000000003" },
    actual_runtime_version: "1",
    applied_fields: [],
    config_hash: "c".repeat(64),
    observed_model: null,
    reason_code: null,
    selected_model: null,
  },
  as_of: "2026-10-07T00:00:00Z",
};

const prepareOriginal: CommandRequest = {
  command_id: "d0000000-0000-0000-0000-000000000001",
  workspace_id: workspaceId,
  command_type: "PrepareProfileHandoff",
  actor: { type: "user", id: principalId },
  target_type: "worker_item",
  target_id: workerItemId,
  expected_version: 7,
  idempotency_key: "d0000000-0000-0000-0000-000000000002",
  correlation_id: "d0000000-0000-0000-0000-000000000003",
  payload: {
    proposal_schema_version: 2,
    context: {
      worker_item_id: workerItemId,
      execution_mode: "native_profile",
      execution_id: executionId,
      session_id: sessionId,
      session_version: 3,
      worker_id: workerId,
      binding_version: 2,
      run_id: runId,
      plan_generation: 2,
      context_version: 4,
    },
    target_assignee: { kind: "profile", id: targetProfileId },
    target_profile_ref: targetProfileRef,
    handoff_reason: "Change the implementation owner",
    handoff_instruction: "Continue the validation step",
    input_plan: {},
    proposal_ttl_seconds: 900,
  },
};

const confirmOriginal: CommandRequest = {
  command_id: "e0000000-0000-0000-0000-000000000001",
  workspace_id: workspaceId,
  command_type: "ConfirmProfileHandoff",
  actor: { type: "user", id: principalId },
  target_type: "handoff_proposal",
  target_id: proposal.id,
  expected_version: proposal.version,
  idempotency_key: "e0000000-0000-0000-0000-000000000002",
  correlation_id: "e0000000-0000-0000-0000-000000000003",
  payload: {
    proposal_id: proposal.id,
    expected_proposal_version: proposal.version,
    expected_proposal_hash: proposal.proposal_hash,
    expected_worker_item_version: proposal.source_worker_item_version,
  },
};

const cancelOriginal: CommandRequest = {
  command_id: "f0000000-0000-0000-0000-000000000001",
  workspace_id: workspaceId,
  command_type: "CancelWorkerHandoff",
  actor: { type: "user", id: principalId },
  target_type: "handoff_proposal",
  target_id: proposal.id,
  expected_version: proposal.version,
  idempotency_key: "f0000000-0000-0000-0000-000000000002",
  correlation_id: "f0000000-0000-0000-0000-000000000003",
  payload: { reason: "Cancel before source activation" },
};

const nativeTransition: NativeHandoffTransition = {
  id: "b0000000-0000-0000-0000-000000000001",
  handoff_schema_version: 2,
  workspace_id: workspaceId,
  worker_item_id: workerItemId,
  proposal_id: proposal.id,
  version: 1,
  status: "pending",
  phase: "stop_source",
  writer_state: "source_stop_requested",
  confirmed_at: "2026-10-07T00:01:00Z",
  started_at: null,
  completed_at: null,
  failure_code: null,
  blocked_operation_id: null,
  logical_attempt: 1,
  next_attempt_at: null,
  source_execution_id: executionId,
  source_session_id: sessionId,
  source_run_id: runId,
  source_binding_version: 2,
  source_profile_ref: sourceProfileRef,
  source_input_cutoff_sequence: 5,
  target_assignee: proposal.target_assignee,
  target_profile_ref: proposal.target_profile_ref,
  target_worker_id: null,
  target_execution_id: null,
  target_session_id: null,
  target_run_id: null,
  target_binding_version: null,
  target_context_version: 4,
  context: proposal.context,
  plan_base: proposal.plan_base,
  available_recovery_strategies: [],
};

function currentSourceReview() {
  const result = reviewHandoffSource({
    workspace: sourceWorkspace,
    nativeContext: sourceContext,
    principalId,
  });
  if (!result.ok) throw new Error("Expected the native source fixture to be reviewable");
  return result.review;
}

function emptyHandoffDraft(): HandoffDraft {
  const idle = { original: null, status: "idle" as const, rejectionCode: null };
  return {
    schema_version: 1,
    scope: handoffScope,
    source_review: currentSourceReview(),
    target: {
      kind: "profile",
      id: targetProfileId,
      profileRef: { workspace_id: workspaceId, profile_id: targetProfileId, revision: 1 },
    },
    reason: "Change the implementation owner",
    instruction: "Continue the validation step",
    prepared: null,
    transition_id: null,
    actions: { prepare: idle, confirm: idle, cancel: idle },
  };
}

function currentPreparedAnchor() {
  const result = reviewPreparedHandoff({
    review: currentSourceReview(),
    proposal,
    workspace: sourceWorkspace,
    nativeContext: sourceContext,
    target: handoffTarget,
    now: Date.parse("2026-10-07T00:01:00Z"),
  });
  if (!result.ok) throw new Error("Expected the native proposal fixture to be reviewable");
  return result.anchor;
}

function confirmedDraftWithV1Anchor(
  anchor = currentPreparedAnchor(),
  transitionId: string | null = nativeTransition.id,
): HandoffDraft {
  const draft = emptyHandoffDraft();
  return {
    ...draft,
    prepared: anchor,
    transition_id: transitionId,
    actions: {
      prepare: { original: prepareOriginal, status: "applied", rejectionCode: null },
      confirm: { original: confirmOriginal, status: "applied", rejectionCode: null },
      cancel: { original: null, status: "idle", rejectionCode: null },
    },
  };
}

it("narrows a native proposal without legacy Worker participants", () => {
  expect(isNativeHandoffProposal(proposal)).toBe(true);
  expect("source" in proposal).toBe(false);
  expect("target" in proposal).toBe(false);
  expect(proposal.context.source_context_version).not.toBe(proposal.context.target_context_version);
});

it("reviews the exact active native source pins independently of input permission", () => {
  const result = reviewHandoffSource({
    workspace: sourceWorkspace,
    nativeContext: sourceContext,
    principalId,
  });

  expect(result.ok).toBe(true);
  if (!result.ok) return;
  expect(result.review).toMatchObject({
    workspace_id: workspaceId,
    principal_id: principalId,
    worker_item_id: workerItemId,
    root_task_id: rootTaskId,
    subtask_id: subtaskId,
    worker_item_version: 7,
    source_profile_ref: sourceProfileRef,
    execution_mode: "native_profile",
    execution_id: executionId,
    session_id: sessionId,
    session_version: 3,
    worker_id: workerId,
    binding_version: 2,
    run_id: runId,
    plan_generation: 2,
    context_version: 4,
    plan_snapshot_id: snapshotId,
  });
});

it("requires the active CP Session status without changing Execution or Run status rules", () => {
  const activeResult = reviewHandoffSource({
    workspace: sourceWorkspace,
    nativeContext: sourceContext,
    principalId,
  });
  expect(activeResult.ok).toBe(true);

  for (const sessionStatus of ["running", "idle", "closed", "lost", null] as const) {
    const result = reviewHandoffSource({
      workspace: {
        ...sourceWorkspace,
        current_runtime: { ...sourceWorkspace.current_runtime!, session_status: sessionStatus },
      },
      nativeContext: sourceContext,
      principalId,
    });
    expect(result.ok, String(sessionStatus)).toBe(false);
  }
});

it("rejects an unassigned, terminal, paused, or mismatched native source", () => {
  const cases: Array<[string, WorkerWorkspace, NativeWorkerContextView]> = [
    ["unassigned", sourceWorkspace, { ...sourceContext, execution_mode: "unassigned", binding: null }],
    ["terminal Run", sourceWorkspace, { ...sourceContext, run: { ...sourceContext.run!, state: "completed" } }],
    ["paused Session", { ...sourceWorkspace, current_runtime: { ...sourceWorkspace.current_runtime!, session_status: "paused" } }, sourceContext],
    ["foreign Run", sourceWorkspace, { ...sourceContext, context: { ...sourceContext.context!, run_id: "90000000-0000-0000-0000-000000000099" } }],
  ];

  for (const [label, workspace, nativeContext] of cases) {
    const result = reviewHandoffSource({ workspace, nativeContext, principalId });
    expect(result.ok, label).toBe(false);
  }
});

it("accepts a prepared proposal whose package context version differs from the WorkerItem address", () => {
  const source = reviewHandoffSource({
    workspace: sourceWorkspace,
    nativeContext: sourceContext,
    principalId,
  });
  expect(source.ok).toBe(true);
  if (!source.ok) return;

  const result = reviewPreparedHandoff({
    review: source.review,
    proposal,
    workspace: sourceWorkspace,
    nativeContext: sourceContext,
    target: {
      kind: "profile",
      id: targetProfileId,
      profileRef: {
        workspace_id: workspaceId,
        profile_id: targetProfileId,
        revision: 1,
      },
    },
    now: Date.parse("2026-10-07T00:01:00Z"),
  });

  expect(result.ok).toBe(true);
  if (!result.ok) return;
  expect(result.anchor.proposal.context).toMatchObject({
    source_context_version: 3,
    target_context_version: 4,
    target_package_id: null,
    target_package_hash: null,
  });
  expect(result.anchor.proposal.source_profile_ref.revision).toBe(1);
});

it("allows explicitly selecting another revision of the saved source Profile", () => {
  const source = reviewHandoffSource({
    workspace: sourceWorkspace,
    nativeContext: sourceContext,
    principalId,
  });
  expect(source.ok).toBe(true);
  if (!source.ok) return;
  const target = {
    kind: "profile" as const,
    id: sourceProfileId,
    profileRef: { ...sourceProfileRef, revision: 2 },
  };
  const result = reviewPreparedHandoff({
    review: source.review,
    proposal: {
      ...proposal,
      target_assignee: { kind: "profile", id: sourceProfileId },
      target_profile_ref: target.profileRef,
    },
    workspace: sourceWorkspace,
    nativeContext: sourceContext,
    target,
    now: Date.parse("2026-10-07T00:01:00Z"),
  });

  expect(result.ok).toBe(true);
  if (result.ok) {
    expect(result.anchor.source_review.source_profile_ref.revision).toBe(1);
    expect(result.anchor.target).toEqual(target);
  }
});

it("detects canonical drift in frozen package and proposal pins", () => {
  expect(samePreparedHandoff(proposal, proposal)).toBe(true);
  expect(samePreparedHandoff(proposal, {
    ...proposal,
    context: { ...proposal.context, source_package_id: "b0000000-0000-0000-0000-000000000099" },
  })).toBe(false);
  expect(samePreparedHandoff(proposal, {
    ...proposal,
    context: { ...proposal.context, source_package_hash: "c".repeat(64) },
  })).toBe(false);
  expect(samePreparedHandoff(proposal, {
    ...proposal,
    context: { ...proposal.context, source_context_version: 4 },
  })).toBe(false);
  expect(samePreparedHandoff(proposal, { ...proposal, proposal_hash: "d".repeat(64) })).toBe(false);
});

it("rejects expired, foreign, drifted, or changed-context prepared proposals", () => {
  const source = reviewHandoffSource({
    workspace: sourceWorkspace,
    nativeContext: sourceContext,
    principalId,
  });
  expect(source.ok).toBe(true);
  if (!source.ok) return;
  const target = {
    kind: "profile" as const,
    id: targetProfileId,
    profileRef: { workspace_id: workspaceId, profile_id: targetProfileId, revision: 1 },
  };
  const changedWorkspace: WorkerWorkspace = {
    ...sourceWorkspace,
    worker_item: { ...sourceWorkspace.worker_item, context_version: 5, acknowledged_context_version: 5 },
  };
  const changedContext: NativeWorkerContextView = {
    ...sourceContext,
    context: { ...sourceContext.context!, context_version: 5 },
  };
  const cases: Array<[string, NativeHandoffProposal, WorkerWorkspace, NativeWorkerContextView, number]> = [
    ["expired", proposal, sourceWorkspace, sourceContext, Date.parse("2026-10-07T00:16:00Z")],
    ["foreign workspace", { ...proposal, workspace_id: "20000000-0000-0000-0000-000000000099" }, sourceWorkspace, sourceContext, Date.parse("2026-10-07T00:01:00Z")],
    ["wrong source aggregate", { ...proposal, source_worker_item_version: 8 }, sourceWorkspace, sourceContext, Date.parse("2026-10-07T00:01:00Z")],
    ["target address context drift", { ...proposal, context: { ...proposal.context, target_context_version: 5 } }, sourceWorkspace, sourceContext, Date.parse("2026-10-07T00:01:00Z")],
    ["plan generation drift", { ...proposal, plan_base: { ...proposal.plan_base, generation: 3 } }, sourceWorkspace, sourceContext, Date.parse("2026-10-07T00:01:00Z")],
    ["current source context drift", proposal, changedWorkspace, changedContext, Date.parse("2026-10-07T00:01:00Z")],
  ];

  for (const [label, candidate, workspace, nativeContext, now] of cases) {
    const result = reviewPreparedHandoff({
      review: source.review,
      proposal: candidate,
      workspace,
      nativeContext,
      target,
      now,
    });
    expect(result.ok, label).toBe(false);
  }
});

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

it("stores drafts under the exact workspace, principal, and WorkerItem scope", () => {
  const scope = { ...handoffScope, principal_id: "principal/current" };
  expect(handoffDraftKey(scope)).toBe(
    "sidera.native-assignment.handoff.v1/20000000-0000-0000-0000-000000000001/principal%2Fcurrent/40000000-0000-0000-0000-000000000001",
  );

  const baseDraft = emptyHandoffDraft();
  const draft = {
    ...baseDraft,
    scope,
    source_review: baseDraft.source_review === null
      ? null
      : { ...baseDraft.source_review, principal_id: scope.principal_id },
  };
  expect(saveHandoffDraft(scope, draft)).toEqual({ ok: true });
  expect(loadHandoffDraft(scope)).toEqual({ ok: true, draft });
});

it("rejects foreign, malformed, and private-extra stored drafts", () => {
  const draft = emptyHandoffDraft();
  localStorage.setItem(handoffDraftKey(handoffScope), JSON.stringify({
    ...draft,
    scope: { ...draft.scope, principal_id: "principal-other" },
  }));
  expect(loadHandoffDraft(handoffScope).ok).toBe(false);

  const privateExtra = { ...draft, private_token: "must-not-hydrate" };
  localStorage.setItem(handoffDraftKey(handoffScope), JSON.stringify(privateExtra));
  expect(loadHandoffDraft(handoffScope).ok).toBe(false);
  expect(saveHandoffDraft(handoffScope, privateExtra).ok).toBe(false);

  localStorage.setItem(handoffDraftKey(handoffScope), "{");
  expect(loadHandoffDraft(handoffScope).ok).toBe(false);
});

it("contains storage access failures and reports drafts as unavailable", () => {
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
    throw new Error("storage denied");
  });
  expect(loadHandoffDraft(handoffScope).ok).toBe(false);

  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new Error("storage denied");
  });
  expect(saveHandoffDraft(handoffScope, emptyHandoffDraft()).ok).toBe(false);
});

it("contains failures while acquiring the global localStorage object for load and save", () => {
  const originalDescriptor = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    get() {
      throw new Error("StorageBlocked");
    },
  });

  let loadResult: ReturnType<typeof loadHandoffDraft> | Error;
  let saveResult: ReturnType<typeof saveHandoffDraft> | Error;
  try {
    try {
      loadResult = loadHandoffDraft(handoffScope);
    } catch (error) {
      loadResult = error as Error;
    }
    try {
      saveResult = saveHandoffDraft(handoffScope, emptyHandoffDraft());
    } catch (error) {
      saveResult = error as Error;
    }
  } finally {
    if (originalDescriptor) {
      Object.defineProperty(globalThis, "localStorage", originalDescriptor);
    } else {
      Reflect.deleteProperty(globalThis, "localStorage");
    }
  }

  expect(loadResult).toMatchObject({ ok: false });
  expect(saveResult).toMatchObject({ ok: false });
});

it("maps Prepare to the new proposal aggregate without requiring live proposal status", () => {
  const expiredProposal = {
    ...proposal,
    status: "expired",
    stale_reason: "source_context_changed",
    expires_at: "2026-10-06T23:59:00Z",
  } satisfies NativeHandoffProposal;
  const response: CommandResponse = {
    command_id: prepareOriginal.command_id,
    aggregate_id: expiredProposal.id,
    aggregate_version: expiredProposal.version,
    event_ids: [],
    data: { proposal: expiredProposal },
  };
  const result = mapHandoffActionResult({
    action: "prepare",
    original: prepareOriginal,
    response,
    scope: handoffScope,
    source_review: currentSourceReview(),
    target: handoffTarget,
    prepared: null,
  });

  expect(result.ok).toBe(true);
  if (result.ok) {
    expect(response.aggregate_id).not.toBe(workerItemId);
    expect(result.proposal.id).toBe(response.aggregate_id);
    expect(result.proposal.status).toBe("expired");
  }
});

it("keeps applied Prepare mappings unavailable when the original result is absent or mismatched", () => {
  const missing = mapHandoffActionResult({
    action: "prepare",
    original: prepareOriginal,
    response: {
      command_id: prepareOriginal.command_id,
      aggregate_id: proposal.id,
      aggregate_version: proposal.version,
      event_ids: [],
      data: null,
    },
    scope: handoffScope,
    source_review: currentSourceReview(),
    target: handoffTarget,
    prepared: null,
  });
  const wrongAggregate = mapHandoffActionResult({
    action: "prepare",
    original: prepareOriginal,
    response: {
      command_id: prepareOriginal.command_id,
      aggregate_id: workerItemId,
      aggregate_version: proposal.version,
      event_ids: [],
      data: { proposal },
    },
    scope: handoffScope,
    source_review: currentSourceReview(),
    target: handoffTarget,
    prepared: null,
  });

  expect(missing.ok).toBe(false);
  expect(wrongAggregate.ok).toBe(false);
});

it("rejects Prepare mappings whose returned reason or instruction differs from the frozen original", () => {
  const unchanged = mapHandoffActionResult({
    action: "prepare",
    original: prepareOriginal,
    response: {
      command_id: prepareOriginal.command_id,
      aggregate_id: proposal.id,
      aggregate_version: proposal.version,
      event_ids: [],
      data: { proposal },
    },
    scope: handoffScope,
    source_review: currentSourceReview(),
    target: handoffTarget,
    prepared: null,
  });
  expect(unchanged.ok).toBe(true);

  const mismatchedProposals = [
    { ...proposal, handoff_reason: "Different reason" },
    { ...proposal, handoff_instruction: "Different instruction" },
  ];
  for (const mismatchedProposal of mismatchedProposals) {
    const result = mapHandoffActionResult({
      action: "prepare",
      original: prepareOriginal,
      response: {
        command_id: prepareOriginal.command_id,
        aggregate_id: mismatchedProposal.id,
        aggregate_version: mismatchedProposal.version,
        event_ids: [],
        data: { proposal: mismatchedProposal },
      },
      scope: handoffScope,
      source_review: currentSourceReview(),
      target: handoffTarget,
      prepared: null,
    });
    expect(result).toEqual({ ok: false, reason: "mapping_unavailable" });
  }
});

it("rejects stored prepared proposals whose text differs from the draft and original Prepare", () => {
  const anchor = currentPreparedAnchor();
  const baseDraft = emptyHandoffDraft();
  const preparedDraft: HandoffDraft = {
    ...baseDraft,
    prepared: anchor,
    actions: {
      ...baseDraft.actions,
      prepare: { original: prepareOriginal, status: "applied", rejectionCode: null },
    },
  };
  const alteredProposals = [
    { ...anchor.proposal, handoff_reason: "Different reason" },
    { ...anchor.proposal, handoff_instruction: "Different instruction" },
  ];

  for (const alteredProposal of alteredProposals) {
    const invalidDraft = {
      ...preparedDraft,
      prepared: { ...anchor, proposal: alteredProposal },
    } as HandoffDraft;
    expect(saveHandoffDraft(handoffScope, invalidDraft).ok).toBe(false);
    localStorage.setItem(handoffDraftKey(handoffScope), JSON.stringify(invalidDraft));
    expect(loadHandoffDraft(handoffScope).ok).toBe(false);
  }
});

it("rejects private extra keys throughout the stored public proposal shape", () => {
  const anchor = currentPreparedAnchor();
  const draft = emptyHandoffDraft();
  const validPreparedDraft: HandoffDraft = {
    ...draft,
    prepared: anchor,
    actions: {
      ...draft.actions,
      prepare: { original: prepareOriginal, status: "applied", rejectionCode: null },
    },
  };
  const proposalWithExtras = [
    { ...anchor.proposal, _private_native: "must-not-persist" },
    { ...anchor.proposal, source_profile_ref: { ...anchor.proposal.source_profile_ref, _private_native: "must-not-persist" } },
    { ...anchor.proposal, target_profile_ref: { ...anchor.proposal.target_profile_ref!, _private_native: "must-not-persist" } },
    { ...anchor.proposal, target_assignee: { ...anchor.proposal.target_assignee, _private_native: "must-not-persist" } },
    { ...anchor.proposal, context: { ...anchor.proposal.context, _private_native: "must-not-persist" } },
    { ...anchor.proposal, plan_base: { ...anchor.proposal.plan_base, _private_native: "must-not-persist" } },
  ] as unknown as NativeHandoffProposal[];

  for (const storedProposal of proposalWithExtras) {
    const invalidDraft = {
      ...validPreparedDraft,
      prepared: { ...anchor, proposal: storedProposal },
    };
    expect(saveHandoffDraft(handoffScope, invalidDraft).ok).toBe(false);
    localStorage.setItem(handoffDraftKey(handoffScope), JSON.stringify(invalidDraft));
    expect(loadHandoffDraft(handoffScope).ok).toBe(false);
  }
});

it("persists a correlated expired Prepare proposal without making it confirmable", () => {
  const expiredProposal = {
    ...proposal,
    status: "expired",
    stale_reason: "proposal_expired",
  } satisfies NativeHandoffProposal;
  const mapping = mapHandoffActionResult({
    action: "prepare",
    original: prepareOriginal,
    response: {
      command_id: prepareOriginal.command_id,
      aggregate_id: expiredProposal.id,
      aggregate_version: expiredProposal.version,
      event_ids: [],
      data: { proposal: expiredProposal },
    } satisfies CommandResponse,
    scope: handoffScope,
    source_review: currentSourceReview(),
    target: handoffTarget,
    prepared: null,
  });
  expect(mapping.ok).toBe(true);
  if (!mapping.ok) return;

  const draft = emptyHandoffDraft();
  const resolvedDraft: HandoffDraft = {
    ...draft,
    prepared: {
      proposal: mapping.proposal,
      source_review: currentSourceReview(),
      target: handoffTarget,
    },
    actions: {
      ...draft.actions,
      prepare: { original: prepareOriginal, status: "applied", rejectionCode: null },
    },
  };
  expect(saveHandoffDraft(handoffScope, resolvedDraft)).toEqual({ ok: true });
  expect(loadHandoffDraft(handoffScope)).toEqual({ ok: true, draft: resolvedDraft });
});

it("maps Confirm to a new transition aggregate and validates both returned DTOs", () => {
  const confirmedProposal = {
    ...proposal,
    status: "confirmed",
    version: 2,
    confirmed_at: "2026-10-07T00:02:00Z",
  } satisfies NativeHandoffProposal;
  const transition = {
    ...nativeTransition,
    confirmed_at: confirmedProposal.confirmed_at,
  } satisfies NativeHandoffTransition;
  const response: CommandResponse = {
    command_id: confirmOriginal.command_id,
    aggregate_id: transition.id,
    aggregate_version: transition.version,
    event_ids: [],
    data: { proposal: confirmedProposal, transition },
  };
  const result = mapHandoffActionResult({
    action: "confirm",
    original: confirmOriginal,
    response,
    scope: handoffScope,
    source_review: currentSourceReview(),
    target: handoffTarget,
    prepared: currentPreparedAnchor(),
  });

  expect(result.ok).toBe(true);
  if (result.ok) {
    expect(transition.id).not.toBe(proposal.id);
    expect(response.aggregate_id).toBe(transition.id);
    expect(result.proposal.version).toBe(proposal.version + 1);
    expect(result.transition?.proposal_id).toBe(proposal.id);
  }
});

it("persists Confirm against the frozen proposal V1 while keeping proposal V2 separate", () => {
  const anchor = currentPreparedAnchor();
  const confirmedProposal = {
    ...proposal,
    status: "confirmed",
    version: 2,
    confirmed_at: "2026-10-07T00:02:00Z",
  } satisfies NativeHandoffProposal;
  const transition = {
    ...nativeTransition,
    confirmed_at: confirmedProposal.confirmed_at,
  } satisfies NativeHandoffTransition;
  const result = mapHandoffActionResult({
    action: "confirm",
    original: confirmOriginal,
    response: {
      command_id: confirmOriginal.command_id,
      aggregate_id: transition.id,
      aggregate_version: transition.version,
      event_ids: [],
      data: { proposal: confirmedProposal, transition },
    } satisfies CommandResponse,
    scope: handoffScope,
    source_review: currentSourceReview(),
    target: handoffTarget,
    prepared: anchor,
  });

  expect(result.ok).toBe(true);
  if (!result.ok || result.transition === null) return;
  expect(result.proposal).toMatchObject({ id: proposal.id, version: 2, status: "confirmed" });
  expect(result.transition).toMatchObject({ id: transition.id, version: 1, proposal_id: proposal.id });
  expect(anchor.proposal).toEqual(proposal);

  const draft = confirmedDraftWithV1Anchor(anchor, result.transition.id);
  expect(saveHandoffDraft(handoffScope, draft)).toEqual({ ok: true });
  expect(loadHandoffDraft(handoffScope)).toEqual({ ok: true, draft });
  expect(draft.prepared?.proposal).toMatchObject({ id: proposal.id, version: 1, status: "prepared" });
});

it("rejects a mutated Confirm anchor and unresolved or competing post-Confirm actions", () => {
  const draft = confirmedDraftWithV1Anchor();
  const confirmedAnchor = {
    ...currentPreparedAnchor(),
    proposal: {
      ...proposal,
      status: "confirmed",
      version: 2,
      confirmed_at: "2026-10-07T00:02:00Z",
    } satisfies NativeHandoffProposal,
  };

  expect(saveHandoffDraft(handoffScope, {
    ...draft,
    prepared: confirmedAnchor,
    transition_id: null,
    actions: {
      ...draft.actions,
      confirm: { original: null, status: "idle", rejectionCode: null },
    },
  }).ok).toBe(false);

  for (const confirm of [
    { ...draft.actions.confirm, status: "unknown" as const },
    { ...draft.actions.confirm, status: "rejected" as const, rejectionCode: "version_conflict" },
  ]) {
    expect(saveHandoffDraft(handoffScope, {
      ...draft,
      actions: { ...draft.actions, confirm },
    }).ok).toBe(false);
  }

  expect(saveHandoffDraft(handoffScope, {
    ...draft,
    actions: {
      ...draft.actions,
      cancel: { original: cancelOriginal, status: "pending", rejectionCode: null },
    },
  }).ok).toBe(false);
});

it("rejects Confirm drafts with foreign scope or altered actor, hash, version, or target pins", () => {
  const draft = confirmedDraftWithV1Anchor();
  const original = draft.actions.confirm.original;
  if (original === null) throw new Error("Expected the exact original Confirm command");
  if (original.expected_version === null) throw new Error("Expected a pinned Confirm version");
  const withConfirm = (confirmOriginalValue: CommandRequest): HandoffDraft => ({
    ...draft,
    actions: {
      ...draft.actions,
      confirm: { ...draft.actions.confirm, original: confirmOriginalValue },
    },
  });
  const invalidDrafts = [
    { ...draft, scope: { ...draft.scope, principal_id: "principal-other" } },
    withConfirm({ ...original, actor: { ...original.actor, id: "principal-other" } }),
    withConfirm({
      ...original,
      payload: { ...original.payload, expected_proposal_hash: "b".repeat(64) },
    }),
    withConfirm({
      ...original,
      expected_version: original.expected_version + 1,
      payload: { ...original.payload, expected_proposal_version: original.expected_version + 1 },
    }),
    withConfirm({ ...original, target_id: workerItemId }),
  ];

  for (const invalid of invalidDrafts) {
    expect(saveHandoffDraft(handoffScope, invalid).ok).toBe(false);
  }
});

it("maps preactivation Cancel to the same proposal with one version advance", () => {
  const cancelledProposal = {
    ...proposal,
    status: "cancelled",
    version: 2,
    cancelled_at: "2026-10-07T00:02:00Z",
  } satisfies NativeHandoffProposal;
  const response: CommandResponse = {
    command_id: cancelOriginal.command_id,
    aggregate_id: proposal.id,
    aggregate_version: cancelledProposal.version,
    event_ids: [],
    data: { proposal: cancelledProposal },
  };
  const anchor = currentPreparedAnchor();
  const result = mapHandoffActionResult({
    action: "cancel",
    original: cancelOriginal,
    response,
    scope: handoffScope,
    source_review: currentSourceReview(),
    target: handoffTarget,
    prepared: anchor,
  });

  expect(result.ok).toBe(true);
  if (result.ok) {
    expect(response.aggregate_id).toBe(proposal.id);
    expect(result.proposal.version).toBe(proposal.version + 1);
    expect(result.proposal.status).toBe("cancelled");
    expect(anchor.proposal).toEqual(proposal);

    const draft = emptyHandoffDraft();
    const cancelledDraft: HandoffDraft = {
      ...draft,
      prepared: anchor,
      actions: {
        ...draft.actions,
        prepare: { original: prepareOriginal, status: "applied", rejectionCode: null },
        cancel: { original: cancelOriginal, status: "applied", rejectionCode: null },
      },
    };
    expect(saveHandoffDraft(handoffScope, cancelledDraft)).toEqual({ ok: true });
    expect(loadHandoffDraft(handoffScope)).toEqual({ ok: true, draft: cancelledDraft });
  }
});
