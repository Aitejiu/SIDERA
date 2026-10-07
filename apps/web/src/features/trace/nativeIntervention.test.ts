import { beforeEach, expect, it, vi } from "vitest";

import type {
  AgentBindingView,
  AgentExecutionView,
  CommandRequest,
  CommandResponse,
  CommandStatusResponse,
  NativeWorkerContextView,
  ProfileRef,
} from "@/features/assignments/types";
import type { WorkerWorkspace } from "@/features/worker-items/types";
import type { Execution, ExecutionSession } from "./types";
import {
  loadNativeInterventionDraft,
  nativeContextFingerprint,
  nativeInterventionDraftKey,
  nativeOriginalStatusMatches,
  reviewNativeInputResult,
  reviewNativeInterventionFacts,
  reviewNativeStopResult,
  saveNativeInterventionDraft,
  type NativeInterventionDraft,
  type NativeInterventionReview,
  type NativeInterventionScope,
  type NativeInputStatus,
} from "./nativeIntervention";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const WORKER_ITEM_ID = "40000000-0000-0000-0000-000000000001";
const ROOT_TASK_ID = "30000000-0000-0000-0000-000000000001";
const SUBTASK_ID = "30000000-0000-0000-0000-000000000002";
const EXECUTION_ID = "60000000-0000-0000-0000-000000000001";
const SESSION_ID = "70000000-0000-0000-0000-000000000001";
const WORKER_ID = "90000000-0000-0000-0000-000000000001";
const RUN_ID = "80000000-0000-0000-0000-000000000001";
const INPUT_ID = "50000000-0000-0000-0000-000000000001";
const PRINCIPAL_ID = "10000000-0000-0000-0000-000000000001";
const LEASE_ID = "a0000000-0000-0000-0000-000000000001";
const PROFILE_REF: ProfileRef = {
  workspace_id: WORKSPACE_ID,
  profile_id: "b0000000-0000-0000-0000-000000000001",
  revision: 1,
};
const SCOPE: NativeInterventionScope = {
  workspace_id: WORKSPACE_ID,
  principal_id: PRINCIPAL_ID,
  worker_item_id: WORKER_ITEM_ID,
};
const context: NonNullable<NativeWorkerContextView["context"]> = {
  worker_item_id: WORKER_ITEM_ID,
  plan_generation: 2,
  context_version: 4,
  execution_id: EXECUTION_ID,
  session_id: SESSION_ID,
  session_version: 5,
  worker_id: WORKER_ID,
  execution_mode: "native_profile",
  binding_version: 2,
  run_id: RUN_ID,
};

const binding: AgentBindingView = {
  workspace_id: WORKSPACE_ID,
  target_type: "worker_item",
  target_id: WORKER_ITEM_ID,
  version: 2,
  assignee: { kind: "profile", id: PROFILE_REF.profile_id },
  profile_ref: PROFILE_REF,
  runnable: true,
  not_runnable_reasons: [],
  created_at: "2026-10-07T00:00:00Z",
  changed_at: "2026-10-07T00:00:00Z",
};

const savedExecution: AgentExecutionView = {
  workspace_id: WORKSPACE_ID,
  worker_item_id: WORKER_ITEM_ID,
  execution_id: EXECUTION_ID,
  subtask_id: SUBTASK_ID,
  root_task_id: ROOT_TASK_ID,
  status: "running",
  link_status: "running",
  run_id: RUN_ID,
  profile_ref: PROFILE_REF,
  binding_version: 2,
  event_cursor: 3,
  updated_at: "2026-10-07T00:00:00Z",
};

const nativeContext: NativeWorkerContextView = {
  workspace_id: WORKSPACE_ID,
  worker_item_id: WORKER_ITEM_ID,
  execution_mode: "native_profile",
  worker_item_version: 7,
  binding,
  execution: savedExecution,
  execution_version: 7,
  context,
  input_enabled: true,
  read_only_reason: null,
  run: {
    state: "running",
    input_open: true,
    profile_ref: PROFILE_REF,
    runtime_ref: { workspace_id: WORKSPACE_ID, runtime_id: "c0000000-0000-0000-0000-000000000001" },
    actual_runtime_version: "1",
    applied_fields: [],
    config_hash: "config-hash",
    observed_model: null,
    reason_code: null,
    selected_model: null,
  },
  as_of: "2026-10-07T00:00:00Z",
};

const workspace = {
  workspace_id: WORKSPACE_ID,
  worker_item: {
    id: WORKER_ITEM_ID,
    root_task_id: ROOT_TASK_ID,
    status: "active",
    version: 7,
    context_version: 4,
    acknowledged_context_version: 4,
  },
  current_placement: {
    plan_generation: 2,
    subtask_task_id: SUBTASK_ID,
  },
  current_runtime: {
    adapter_type: "agent-run",
    execution_id: EXECUTION_ID,
    execution_status: "running",
    session_id: SESSION_ID,
    session_status: "active",
    session_version: 5,
    worker_id: WORKER_ID,
    task_id: SUBTASK_ID,
    lease_id: LEASE_ID,
  },
  active_handoff: null,
} as unknown as WorkerWorkspace;

const execution = {
  id: EXECUTION_ID,
  workspace_id: WORKSPACE_ID,
  worker_item_id: WORKER_ITEM_ID,
  task_id: SUBTASK_ID,
  version: 7,
  status: "running",
  stop_requested: false,
  active_lease_id: LEASE_ID,
} as unknown as Execution;

const session = {
  id: SESSION_ID,
  workspace_id: WORKSPACE_ID,
  execution_id: EXECUTION_ID,
  worker_id: WORKER_ID,
  version: 5,
  status: "active",
} as unknown as ExecutionSession;

const review: NativeInterventionReview = {
  context,
  execution_version: 7,
  profile_ref: PROFILE_REF,
  binding_version: 2,
};

function inputCommand(): CommandRequest {
  return {
    command_id: "e0000000-0000-0000-0000-000000000001",
    workspace_id: WORKSPACE_ID,
    command_type: "SendExecutionInput",
    actor: { type: "user", id: PRINCIPAL_ID },
    target_type: "execution_session",
    target_id: SESSION_ID,
    expected_version: 5,
    idempotency_key: "f0000000-0000-0000-0000-000000000001",
    correlation_id: "11000000-0000-0000-0000-000000000001",
    payload: {
      context,
      client_idempotency_key: "12000000-0000-0000-0000-000000000001",
      text: "  retain normalized body  ",
      attachment_refs: [],
    },
  };
}

function stopCommand(): CommandRequest {
  return {
    command_id: "e0000000-0000-0000-0000-000000000002",
    workspace_id: WORKSPACE_ID,
    command_type: "StopExecution",
    actor: { type: "user", id: PRINCIPAL_ID },
    target_type: "execution",
    target_id: EXECUTION_ID,
    expected_version: 7,
    idempotency_key: "f0000000-0000-0000-0000-000000000002",
    correlation_id: "11000000-0000-0000-0000-000000000002",
    payload: {},
  };
}

function makeDraft(): NativeInterventionDraft {
  return {
    schema_version: 1,
    scope: SCOPE,
    text: "  retain normalized body  ",
    client_idempotency_key: "12000000-0000-0000-0000-000000000001",
    review,
    input: {
      original: inputCommand(),
      review,
      status: "unknown",
      aggregate_id: null,
      aggregate_version: null,
      rejection_code: null,
    },
    stop: {
      original: null,
      review: null,
      status: "idle",
      aggregate_id: null,
      aggregate_version: null,
      rejection_code: null,
    },
  };
}

function makeFacts(overrides: {
  workspace?: WorkerWorkspace;
  nativeContext?: NativeWorkerContextView;
  execution?: Execution;
  session?: ExecutionSession;
} = {}) {
  return {
    scope: SCOPE,
    workspace: overrides.workspace ?? workspace,
    nativeContext: overrides.nativeContext ?? nativeContext,
    execution: overrides.execution ?? execution,
    session: overrides.session ?? session,
  };
}

beforeEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

it("includes all native pins in its context fingerprint", () => {
  expect(nativeContextFingerprint(context)).not.toBe(
    nativeContextFingerprint({ ...context, binding_version: 3 }),
  );
  expect(nativeContextFingerprint(context)).not.toBe(
    nativeContextFingerprint({
      ...context,
      run_id: "80000000-0000-0000-0000-000000000002",
    }),
  );
});

it("reviews a pinned R1 native source without substituting a later profile head", () => {
  const result = reviewNativeInterventionFacts(makeFacts());

  expect(result).toMatchObject({
    ok: true,
    review: { context, execution_version: 7, profile_ref: PROFILE_REF, binding_version: 2 },
    canInput: true,
    canStop: true,
  });
});

it("keeps Stop eligible when the input Run is explicitly unsupported", () => {
  const result = reviewNativeInterventionFacts(makeFacts({
    nativeContext: {
      ...nativeContext,
      input_enabled: false,
      read_only_reason: "unsupported_input",
      run: { ...nativeContext.run!, input_open: false, state: "idle" },
    },
  }));

  expect(result).toMatchObject({ ok: true, canInput: false, canStop: true });
});

it("requires the active lease on the exact original Execution for Stop", () => {
  const noLeaseWorkspace = {
    ...workspace,
    current_runtime: { ...workspace.current_runtime!, lease_id: null },
  } as WorkerWorkspace;
  const noLeaseExecution = { ...execution, active_lease_id: null } as Execution;
  const result = reviewNativeInterventionFacts(makeFacts({
    workspace: noLeaseWorkspace,
    execution: noLeaseExecution,
  }));

  expect(result).toMatchObject({ ok: true, canInput: true, canStop: false });
});

it("rejects an Execution belonging to another Subtask", () => {
  const result = reviewNativeInterventionFacts(makeFacts({
    execution: {
      ...execution,
      task_id: "30000000-0000-0000-0000-000000000099",
    } as Execution,
  }));

  expect(result).toMatchObject({ ok: false, reason: "native_source_mismatch" });
});

it("fails closed when the current human Session no longer matches the saved pins", () => {
  const result = reviewNativeInterventionFacts(makeFacts({
    session: { ...session, id: "70000000-0000-0000-0000-000000000002" } as ExecutionSession,
  }));

  expect(result).toMatchObject({ ok: false });
});

it("correlates input results to a new input aggregate, not the Session version", () => {
  const original = inputCommand();
  const response: CommandResponse = {
    command_id: original.command_id,
    aggregate_id: INPUT_ID,
    aggregate_version: 1,
    event_ids: [],
    data: { status: "delivered", text: "private response fields are ignored" },
  };

  expect(reviewNativeInputResult(original, response)).toEqual({
    status: "applied",
    aggregate_id: INPUT_ID,
    aggregate_version: 1,
  });
  expect(reviewNativeInputResult(original, { ...response, command_id: SESSION_ID })).toEqual({
    status: "unknown",
    aggregate_id: null,
    aggregate_version: null,
  });
});

it("keeps a correlated input result recoverable when its mapping is malformed", () => {
  const original = inputCommand();
  const response: CommandResponse = {
    command_id: original.command_id,
    aggregate_id: SESSION_ID,
    aggregate_version: 0,
    event_ids: [],
  };

  expect(reviewNativeInputResult(original, response)).toEqual({
    status: "applied",
    aggregate_id: null,
    aggregate_version: null,
  });
});

it("treats Stop as requested only for the original Execution at version plus one", () => {
  const original = stopCommand();
  const result: CommandResponse = {
    command_id: original.command_id,
    aggregate_id: EXECUTION_ID,
    aggregate_version: 8,
    event_ids: [],
  };

  expect(reviewNativeStopResult(original, review, result)).toEqual({
    status: "applied",
    aggregate_id: EXECUTION_ID,
    aggregate_version: 8,
  });
  expect(reviewNativeStopResult(original, review, { ...result, aggregate_version: 7 })).toEqual({
    status: "applied",
    aggregate_id: null,
    aggregate_version: null,
  });
});

it("accepts only actual original journal records with explicit matching status", () => {
  const original = inputCommand();
  const status = (value: string): CommandStatusResponse => ({
    command_id: original.command_id,
    command_type: original.command_type,
    target_type: original.target_type,
    target_id: original.target_id,
    expected_version: original.expected_version,
    status: value as CommandStatusResponse["status"],
    result: null,
  });

  expect(nativeOriginalStatusMatches(original, status("pending"))).toBe(true);
  expect(nativeOriginalStatusMatches(original, status("applied"))).toBe(true);
  expect(nativeOriginalStatusMatches(original, status("rejected"))).toBe(true);
  expect(nativeOriginalStatusMatches(original, status("unknown"))).toBe(false);
  expect(nativeOriginalStatusMatches(original, { ...status("applied"), expected_version: 4 })).toBe(false);
});

it("uses an exact workspace, principal, WorkerItem storage key", () => {
  expect(nativeInterventionDraftKey(SCOPE)).toBe(
    `sidera.native-intervention.v1:${WORKSPACE_ID}:${PRINCIPAL_ID}:${WORKER_ITEM_ID}`,
  );
});

it("round trips independent original input and draft reviews", () => {
  const saved = makeDraft();
  const headReview: NativeInterventionReview = {
    ...review,
    context: { ...context, run_id: "80000000-0000-0000-0000-000000000002" },
  };
  const next = { ...saved, review: headReview };

  expect(saveNativeInterventionDraft(SCOPE, next)).toEqual({ ok: true });
  expect(loadNativeInterventionDraft(SCOPE)).toEqual({ ok: true, draft: next });
});

it("rejects a private nested original payload before persisting it", () => {
  const key = nativeInterventionDraftKey(SCOPE);
  const draft = makeDraft();
  const privateOriginal = {
    ...draft.input.original!,
    payload: { ...draft.input.original!.payload, native_handle: "private" },
  };
  const unsafe = { ...draft, input: { ...draft.input, original: privateOriginal } };

  expect(saveNativeInterventionDraft(SCOPE, unsafe)).toMatchObject({ ok: false });
  expect(localStorage.getItem(key)).toBeNull();
});

it("rejects private fields in a hydrated nested review context", () => {
  const key = nativeInterventionDraftKey(SCOPE);
  const draft = makeDraft();
  const unsafe = {
    ...draft,
    review: {
      ...review,
      context: { ...context, private_handle: "private" },
    },
  };
  localStorage.setItem(key, JSON.stringify(unsafe));

  expect(loadNativeInterventionDraft(SCOPE)).toMatchObject({ ok: false });
});

it("rejects nested ProfileRef extras and foreign principal drafts", () => {
  const key = nativeInterventionDraftKey(SCOPE);
  const draft = makeDraft();
  const unsafeProfile = {
    ...draft,
    review: {
      ...review,
      profile_ref: { ...PROFILE_REF, token: "private" },
    },
  };
  localStorage.setItem(key, JSON.stringify(unsafeProfile));
  expect(loadNativeInterventionDraft(SCOPE)).toMatchObject({ ok: false });

  localStorage.setItem(key, JSON.stringify({ ...draft, scope: { ...SCOPE, principal_id: "other" } }));
  expect(loadNativeInterventionDraft(SCOPE)).toMatchObject({ ok: false });
});

it("turns blocked localStorage reads and writes into safe failures", () => {
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
    throw new Error("private storage failure");
  });
  expect(loadNativeInterventionDraft(SCOPE)).toMatchObject({ ok: false });

  vi.restoreAllMocks();
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new Error("private storage failure");
  });
  expect(saveNativeInterventionDraft(SCOPE, makeDraft())).toMatchObject({ ok: false });
});

it("keeps the public native input delivery status vocabulary finite", () => {
  const statuses: NativeInputStatus[] = [
    "accepted", "queued", "dispatching", "delivered", "blocked", "failed", "cancelled", "superseded",
  ];

  expect(statuses).toHaveLength(8);
});
