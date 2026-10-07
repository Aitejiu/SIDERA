import { describe, expect, it } from "vitest";

import type { ProfileRef } from "@/features/agents/types";
import type { TaskView } from "@/features/tasks/types";
import type { WorkerWorkspace } from "@/features/worker-items/types";
import type { AgentBindingView, AgentExecutionView, NativeWorkerContextView } from "./types";
import {
  appointmentDraftKey,
  loadAppointmentDraft,
  reviewAppointmentFacts,
  saveAppointmentDraft,
  type AppointmentDraft,
  type AppointmentScope,
} from "./assigneeAppointmentState";

const WS = "20000000-0000-0000-0000-000000000001";
const SUBTASK = "30000000-0000-0000-0000-000000000002";
const WORKER = "40000000-0000-0000-0000-000000000001";
const ROOT = "30000000-0000-0000-0000-000000000001";
const PRINCIPAL = "principal-current";
const PROFILE_ID = "50000000-0000-0000-0000-000000000001";
const EXECUTION_ID = "60000000-0000-0000-0000-000000000001";
const SESSION_ID = "70000000-0000-0000-0000-000000000001";
const RUN_ID = "80000000-0000-0000-0000-000000000001";
const WORKER_SCOPE: AppointmentScope = {
  workspace_id: WS,
  principal_id: PRINCIPAL,
  target_kind: "worker_item",
  target_id: WORKER,
};
const scope: AppointmentScope = {
  workspace_id: WS,
  principal_id: PRINCIPAL,
  target_kind: "subtask",
  target_id: SUBTASK,
};
const task: TaskView = {
  id: SUBTASK,
  workspace_id: WS,
  title: "Implementation",
  description: "Implement the dependency",
  acceptance_criteria: ["Tests pass"],
  requested_by: PRINCIPAL,
  priority: 1,
  status: "running",
  version: 7,
  created_at: "2026-10-07T00:00:00Z",
  updated_at: "2026-10-07T00:00:00Z",
};
const binding: AgentBindingView = {
  workspace_id: WS,
  target_type: "subtask",
  target_id: SUBTASK,
  assignee: { kind: "human", id: PRINCIPAL },
  profile_ref: null,
  version: 2,
  runnable: false,
  not_runnable_reasons: [],
  created_at: "2026-10-07T00:00:00Z",
  changed_at: "2026-10-07T00:00:00Z",
};

const profileRef: ProfileRef = {
  workspace_id: WS,
  profile_id: PROFILE_ID,
  revision: 1,
};

function profileBinding(targetKind: "subtask" | "worker_item", targetId: string): AgentBindingView {
  return {
    workspace_id: WS,
    target_type: targetKind,
    target_id: targetId,
    assignee: { kind: "profile", id: PROFILE_ID },
    profile_ref: profileRef,
    version: 2,
    runnable: true,
    not_runnable_reasons: [],
    created_at: "2026-10-07T00:00:00Z",
    changed_at: "2026-10-07T00:00:00Z",
  };
}

const workerWorkspace: WorkerWorkspace = {
  workspace_id: WS,
  worker_item: {
    id: WORKER,
    stable_key: "implement",
    title: "Implementation",
    responsibility: "Implement the dependency",
    root_task_id: ROOT,
    status: "active",
    context_version: 4,
    acknowledged_context_version: 4,
    version: 7,
    updated_at: "2026-10-07T00:00:00Z",
  },
  current_placement: {
    worker_item_key: "implement",
    subtask_key: "implementation",
    subtask_task_id: SUBTASK,
    role: "implementer",
    capability_requirements: [],
    plan_generation: 2,
    plan_snapshot_id: "90000000-0000-0000-0000-000000000001",
  },
  placement_history: [],
  current_runtime: null,
  execution_history: [],
  current_step: null,
  next_action: null,
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

function nativeContext(overrides: Partial<NativeWorkerContextView> = {}): NativeWorkerContextView {
  return {
    workspace_id: WS,
    worker_item_id: WORKER,
    execution_mode: "native_profile",
    worker_item_version: 7,
    binding: profileBinding("worker_item", WORKER),
    execution: null,
    execution_version: null,
    context: null,
    input_enabled: false,
    read_only_reason: "execution_not_started",
    run: null,
    as_of: "2026-10-07T00:00:00Z",
    ...overrides,
  };
}

function execution(overrides: Partial<AgentExecutionView> = {}): AgentExecutionView {
  return {
    workspace_id: WS,
    execution_id: EXECUTION_ID,
    worker_item_id: WORKER,
    subtask_id: SUBTASK,
    root_task_id: ROOT,
    profile_ref: profileRef,
    binding_version: 2,
    run_id: RUN_ID,
    event_cursor: 4,
    status: "running",
    link_status: "accepted",
    updated_at: "2026-10-07T00:00:00Z",
    ...overrides,
  };
}

function workerExecutionSummary(
  overrides: Partial<WorkerWorkspace["execution_history"][number]> = {},
): WorkerWorkspace["execution_history"][number] {
  return {
    execution_id: EXECUTION_ID,
    task_id: SUBTASK,
    status: "running",
    attempt: 1,
    worker_id: "80000000-0000-0000-0000-000000000002",
    session_id: SESSION_ID,
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
    assigned_worker_id: "80000000-0000-0000-0000-000000000002",
    pinned_worker_summary: null,
    assigned_worker_summary: null,
    binding_summary: null,
    ...overrides,
  };
}

function workerRuntime(adapterType: string): NonNullable<WorkerWorkspace["current_runtime"]> {
  return {
    adapter_type: adapterType,
    attempt: 1,
    execution_id: EXECUTION_ID,
    execution_status: "running",
    fencing_token: 2,
    input_mode: "queued",
    lease_id: "lease-1",
    session_id: SESSION_ID,
    session_status: "active",
    session_version: 3,
    task_id: SUBTASK,
    worker_display_name: "Agent",
    worker_id: "80000000-0000-0000-0000-000000000002",
  };
}

const activeWorkerWorkspace: WorkerWorkspace = {
  ...workerWorkspace,
  current_runtime: workerRuntime("agent-run"),
  execution_history: [workerExecutionSummary()],
};

function draft(overrides: Partial<AppointmentDraft> = {}): AppointmentDraft {
  return {
    schema_version: 1,
    scope,
    review: { aggregateVersion: 7, bindingFingerprint: "binding", executionFingerprint: "" },
    choice: { kind: "human", id: PRINCIPAL, profileRef: null },
    original: {
      command_id: "a0000000-0000-0000-0000-000000000001",
      workspace_id: WS,
      command_type: "SetSubtaskAssignee",
      actor: { type: "user", id: PRINCIPAL },
      target_type: "task",
      target_id: SUBTASK,
      expected_version: 7,
      idempotency_key: "b0000000-0000-0000-0000-000000000001",
      correlation_id: "c0000000-0000-0000-0000-000000000001",
      payload: { assignee_kind: "human", assignee_id: PRINCIPAL, profile_revision: null },
    },
    status: "pending",
    rejectionCode: null,
    ...overrides,
  };
}

describe("appointment review", () => {
  it("uses the local Task aggregate version, not its binding version", () => {
    const reviewed = reviewAppointmentFacts({ scope, task, binding });
    expect(reviewed.ok).toBe(true);
    if (!reviewed.ok) throw new Error("Expected valid scoped facts");
    expect(reviewed.action).toBe("appointment");
    expect(reviewed.review.aggregateVersion).toBe(7);
    expect(reviewed.review.aggregateVersion).not.toBe(binding.version);
  });

  it("rejects a Task returned for another scope", () => {
    const reviewed = reviewAppointmentFacts({
      scope,
      binding,
      task: { ...task, workspace_id: "foreign-workspace" },
    });
    expect(reviewed).toMatchObject({ ok: false, action: "unavailable" });
  });

  it("does not confuse a null binding with an unresolved Task aggregate", () => {
    const reviewed = reviewAppointmentFacts({ scope, task, binding: null });
    expect(reviewed).toMatchObject({ ok: true, action: "appointment" });
    if (reviewed.ok) expect(reviewed.review.aggregateVersion).toBe(7);
  });

  it.each(["succeeded", "failed", "canceled", "unexpected"])(
    "does not review a terminal or unknown Task status (%s)",
    (status) => {
      const reviewed = reviewAppointmentFacts({
        scope,
        task: { ...task, status },
        binding,
      });
      expect(reviewed).toMatchObject({ ok: false, action: "unavailable" });
    },
  );

  it("captures canonical owner changes as review drift", () => {
    const original = reviewAppointmentFacts({ scope, task, binding });
    const changed = reviewAppointmentFacts({
      scope,
      task,
      binding: { ...binding, assignee: { kind: "human", id: "principal-other" } },
    });
    expect(original.ok && changed.ok).toBe(true);
    if (original.ok && changed.ok) {
      expect(changed.review.aggregateVersion).toBe(original.review.aggregateVersion);
      expect(changed.review.bindingFingerprint).not.toBe(original.review.bindingFingerprint);
    }
  });
});

describe("worker appointment review", () => {
  it.each([
    ["active", true],
    ["paused", true],
    ["retired", false],
    ["unexpected", false],
  ] as const)("checks WorkerItem aggregate status %s before native appointment", (status, eligible) => {
    const reviewed = reviewAppointmentFacts({
      scope: WORKER_SCOPE,
      binding: profileBinding("worker_item", WORKER),
      workerWorkspace: {
        ...workerWorkspace,
        worker_item: {
          ...workerWorkspace.worker_item,
          status: status as WorkerWorkspace["worker_item"]["status"],
        },
      },
      nativeContext: nativeContext(),
    });
    if (eligible) expect(reviewed).toMatchObject({ ok: true, action: "appointment" });
    else expect(reviewed).toMatchObject({ ok: false, action: "unavailable" });
  });

  it("uses WorkerItem version 7 instead of binding version 2", () => {
    const reviewed = reviewAppointmentFacts({
      scope: WORKER_SCOPE,
      binding: profileBinding("worker_item", WORKER),
      workerWorkspace,
      nativeContext: nativeContext(),
    });
    expect(reviewed).toMatchObject({ ok: true, action: "appointment" });
    if (reviewed.ok) {
      expect(reviewed.review.aggregateVersion).toBe(7);
      expect(reviewed.review.aggregateVersion).not.toBe(2);
    }
  });

  it("refuses disagreeing WorkerItem aggregate versions", () => {
    const reviewed = reviewAppointmentFacts({
      scope: WORKER_SCOPE,
      binding: profileBinding("worker_item", WORKER),
      workerWorkspace,
      nativeContext: nativeContext({ worker_item_version: 8 }),
    });
    expect(reviewed).toMatchObject({ ok: false, action: "unavailable" });
  });

  it("requires native-context assignment facts to agree with the canonical GET", () => {
    const reviewed = reviewAppointmentFacts({
      scope: WORKER_SCOPE,
      binding: profileBinding("worker_item", WORKER),
      workerWorkspace,
      nativeContext: nativeContext({ binding: null }),
    });
    expect(reviewed).toMatchObject({ ok: false, action: "unavailable" });
  });

  it("preserves a positive legacy execution as legacy", () => {
    const legacyRuntime = {
      adapter_type: "opencode",
      attempt: 1,
      execution_id: EXECUTION_ID,
      execution_status: "running",
      fencing_token: 2,
      input_mode: "queued",
      lease_id: "lease-1",
      session_id: SESSION_ID,
      session_status: "active",
      session_version: 3,
      task_id: SUBTASK,
      worker_display_name: "OpenCode",
      worker_id: "80000000-0000-0000-0000-000000000002",
    } satisfies NonNullable<WorkerWorkspace["current_runtime"]>;
    const reviewed = reviewAppointmentFacts({
      scope: WORKER_SCOPE,
      binding: null,
      workerWorkspace: {
        ...workerWorkspace,
        current_runtime: legacyRuntime,
        execution_history: [workerExecutionSummary({
          version: 5,
          adapter_preferences: ["opencode"],
        })],
      },
      nativeContext: nativeContext({
        execution_mode: "legacy",
        binding: null,
        execution: execution({ profile_ref: null, binding_version: null, run_id: null, event_cursor: null }),
        execution_version: 5,
        read_only_reason: "execution_mode_legacy",
      }),
    });
    expect(reviewed).toMatchObject({ ok: false, action: "legacy" });
  });

  it("keeps active native execution changes on the handoff path", () => {
    const activeExecution = execution();
    const reviewed = reviewAppointmentFacts({
      scope: WORKER_SCOPE,
      binding: profileBinding("worker_item", WORKER),
      workerWorkspace: activeWorkerWorkspace,
      nativeContext: nativeContext({
        execution: activeExecution,
        execution_version: 5,
        context: {
          worker_item_id: WORKER,
          plan_generation: 2,
          context_version: 4,
          execution_id: EXECUTION_ID,
          session_id: SESSION_ID,
          session_version: 3,
          worker_id: "80000000-0000-0000-0000-000000000002",
          execution_mode: "native_profile",
          binding_version: 2,
          run_id: RUN_ID,
        },
        run: {
          actual_runtime_version: "1",
          applied_fields: [],
          config_hash: "a".repeat(64),
          input_open: true,
          observed_model: null,
          profile_ref: profileRef,
          reason_code: null,
          runtime_ref: { workspace_id: WS, runtime_id: "90000000-0000-0000-0000-000000000002" },
          selected_model: null,
          state: "running",
        },
      }),
    });
    if (!reviewed.ok) throw new Error(reviewed.reason);
    expect(reviewed.action).toBe("handoff_required");
  });

  it("does not authorize an active native Run with conflicting pins", () => {
    const reviewed = reviewAppointmentFacts({
      scope: WORKER_SCOPE,
      binding: profileBinding("worker_item", WORKER),
      workerWorkspace: activeWorkerWorkspace,
      nativeContext: nativeContext({
        execution: execution({ run_id: "80000000-0000-0000-0000-000000000099" }),
        execution_version: 5,
        context: {
          worker_item_id: WORKER,
          plan_generation: 2,
          context_version: 4,
          execution_id: EXECUTION_ID,
          session_id: SESSION_ID,
          session_version: 3,
          worker_id: "80000000-0000-0000-0000-000000000002",
          execution_mode: "native_profile",
          binding_version: 2,
          run_id: RUN_ID,
        },
        run: {
          actual_runtime_version: "1",
          applied_fields: [],
          config_hash: "a".repeat(64),
          input_open: true,
          observed_model: null,
          profile_ref: profileRef,
          reason_code: null,
          runtime_ref: { workspace_id: WS, runtime_id: "90000000-0000-0000-0000-000000000002" },
          selected_model: null,
          state: "running",
        },
      }),
    });
    expect(reviewed).toMatchObject({ ok: false, action: "unavailable" });
  });

  it("permits an exact known-unadmitted original for Control Plane arbitration", () => {
    const unadmittedExecution = execution({
      run_id: null,
      event_cursor: 0,
      status: "pending",
      link_status: "busy",
    });
    const reviewed = reviewAppointmentFacts({
      scope: WORKER_SCOPE,
      binding: profileBinding("worker_item", WORKER),
      workerWorkspace: {
        ...workerWorkspace,
        execution_history: [workerExecutionSummary({ status: "pending", version: 1 })],
      },
      nativeContext: nativeContext({
        execution: unadmittedExecution,
        execution_version: 1,
        context: null,
        run: null,
        read_only_reason: "run_not_admitted",
      }),
    });
    expect(reviewed).toMatchObject({ ok: true, action: "appointment" });
  });

  it("permits only a coherently terminal saved Run for Control Plane arbitration", () => {
    const finishedExecution = execution({
      status: "succeeded",
      link_status: "succeeded",
    });
    const reviewed = reviewAppointmentFacts({
      scope: WORKER_SCOPE,
      binding: profileBinding("worker_item", WORKER),
      workerWorkspace: {
        ...workerWorkspace,
        execution_history: [workerExecutionSummary({
          status: "succeeded",
          finished_at: "2026-10-07T01:00:00Z",
          version: 5,
        })],
      },
      nativeContext: nativeContext({
        execution: finishedExecution,
        execution_version: 5,
        context: {
          worker_item_id: WORKER,
          plan_generation: 2,
          context_version: 4,
          execution_id: EXECUTION_ID,
          session_id: SESSION_ID,
          session_version: 3,
          worker_id: "80000000-0000-0000-0000-000000000002",
          execution_mode: "native_profile",
          binding_version: 2,
          run_id: RUN_ID,
        },
        run: {
          actual_runtime_version: "1",
          applied_fields: [],
          config_hash: "a".repeat(64),
          input_open: false,
          observed_model: null,
          profile_ref: profileRef,
          reason_code: null,
          runtime_ref: { workspace_id: WS, runtime_id: "90000000-0000-0000-0000-000000000002" },
          selected_model: null,
          state: "completed",
        },
        read_only_reason: "execution_terminal",
      }),
    });
    expect(reviewed).toMatchObject({ ok: true, action: "appointment" });
  });

  it("does not classify contradictory agent-run evidence as legacy", () => {
    const reviewed = reviewAppointmentFacts({
      scope: WORKER_SCOPE,
      binding: null,
      workerWorkspace: { ...activeWorkerWorkspace, current_runtime: workerRuntime("agent-run") },
      nativeContext: nativeContext({ execution_mode: "legacy", binding: null }),
    });
    expect(reviewed).toMatchObject({ ok: false, action: "unavailable" });
  });
});

describe("appointment draft storage", () => {
  it("keys every scope field in a deterministic encoded local key", () => {
    expect(appointmentDraftKey({
      workspace_id: "workspace/one",
      principal_id: "principal one",
      target_kind: "worker_item",
      target_id: "item/one",
    })).toBe("sidera.native-assignment.appointment.v1/workspace%2Fone/principal%20one/worker_item/item%2Fone");
  });

  it("round-trips one exact original command without adding private fields", () => {
    expect(saveAppointmentDraft(scope, draft())).toEqual({ ok: true });
    expect(loadAppointmentDraft(scope)).toEqual({ ok: true, draft: draft() });
    const stored = localStorage.getItem(appointmentDraftKey(scope));
    expect(stored).not.toBeNull();
    expect(stored).not.toContain("cookie");
    expect(stored).not.toContain("token");
  });

  it.each([
    ["foreign principal", { scope: { ...scope, principal_id: "principal-other" } }],
    ["private extra property", { private_token: "must-not-hydrate" }],
    ["wrong target", { original: { ...draft().original, target_id: ROOT } }],
    ["wrong version", { original: { ...draft().original, expected_version: 2 } }],
    ["wrong payload", { original: { ...draft().original, payload: { assignee_kind: "human", assignee_id: PRINCIPAL, profile_revision: null, targets: [WORKER] } } }],
  ] as const)("refuses %s during hydration", (_label, change) => {
    localStorage.setItem(appointmentDraftKey(scope), JSON.stringify({ ...draft(), ...change }));
    expect(loadAppointmentDraft(scope)).toMatchObject({ ok: false });
  });

  it("refuses to save a draft for another scope", () => {
    expect(saveAppointmentDraft(WORKER_SCOPE, draft())).toMatchObject({ ok: false });
  });
});
