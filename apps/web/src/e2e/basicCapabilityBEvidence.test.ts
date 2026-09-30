import { describe, expect, it } from "vitest";

import {
  candidateHistoryItems,
  classifyPlanTransitionStatus,
  findExactExecutionInput,
  findPostInputTurnCompletionSequence,
  isIntegrationDispatchStalled,
  isWorkerWorkspaceTerminalForIntervention,
  isWritableWorkerWorkspaceSnapshot,
  taskGraphSubtaskKeys,
  unaffectedSourceResultIds,
} from "./basicCapabilityBEvidence";

describe("B real acceptance evidence joins", () => {
  it("flags an integration dispatch stall only after all sources succeed without an integration execution", () => {
    expect(isIntegrationDispatchStalled({
      sourceStatuses: ["succeeded", "running"],
      integrationHasExecution: false,
      stalledForMs: 600_000,
    })).toBe(false);
    expect(isIntegrationDispatchStalled({
      sourceStatuses: [],
      integrationHasExecution: false,
      stalledForMs: 600_000,
    })).toBe(false);
    expect(isIntegrationDispatchStalled({
      sourceStatuses: ["succeeded", "succeeded"],
      integrationHasExecution: false,
      stalledForMs: 179_999,
    })).toBe(false);
    expect(isIntegrationDispatchStalled({
      sourceStatuses: ["succeeded", "succeeded"],
      integrationHasExecution: false,
      stalledForMs: 180_000,
    })).toBe(true);
    expect(isIntegrationDispatchStalled({
      sourceStatuses: ["succeeded", "succeeded"],
      integrationHasExecution: true,
      stalledForMs: 600_000,
    })).toBe(false);
  });

  it("reads candidate_history.items from the paginated result overview envelope", () => {
    const candidate = { candidate_id: "candidate-1" };
    expect(candidateHistoryItems({ items: [candidate], next_cursor: null })).toEqual([candidate]);
  });

  it("accepts only a coherent active-session intervention snapshot", () => {
    expect(isWritableWorkerWorkspaceSnapshot({
      can_intervene: true,
      read_only_reason: null,
      intervention_context: {
        execution_id: "execution-1",
        session_id: "session-1",
      },
      current_runtime: {
        execution_id: "execution-1",
        execution_status: "running",
        session_id: "session-1",
        session_status: "active",
        input_mode: "queued",
      },
    })).toBe(true);
  });

  it("distinguishes a pending WorkerItem from a Session that cannot be continued", () => {
    expect(isWorkerWorkspaceTerminalForIntervention({
      current_runtime: {
        execution_status: "queued",
        session_id: null,
        session_status: null,
      },
    })).toBe(false);
    expect(isWorkerWorkspaceTerminalForIntervention({
      current_runtime: {
        execution_status: "succeeded",
        session_id: "session-1",
        session_status: "closed",
      },
    })).toBe(true);
    expect(isWorkerWorkspaceTerminalForIntervention({
      current_runtime: {
        execution_status: "running",
        session_id: "session-1",
        session_status: "closed",
      },
    })).toBe(true);
  });

  it.each([
    {
      name: "a completed session",
      patch: {
        can_intervene: false,
        read_only_reason: "no_active_session",
        current_runtime: {
          execution_id: "execution-1",
          execution_status: "succeeded",
          session_id: "session-1",
          session_status: "closed",
          input_mode: "queued",
        },
      },
    },
    {
      name: "a session which becomes read-only after the initial poll",
      patch: { can_intervene: true, read_only_reason: "no_active_session" },
    },
    {
      name: "a runtime bound to a different session than its intervention context",
      patch: {
        current_runtime: {
          execution_id: "execution-1",
          execution_status: "running",
          session_id: "session-2",
          session_status: "active",
          input_mode: "queued",
        },
      },
    },
    {
      name: "a terminal execution with a retained active session row",
      patch: {
        current_runtime: {
          execution_id: "execution-1",
          execution_status: "succeeded",
          session_id: "session-1",
          session_status: "active",
          input_mode: "queued",
        },
      },
    },
  ])("rejects $name", ({ patch }) => {
    const snapshot = {
      can_intervene: true,
      read_only_reason: null,
      intervention_context: {
        execution_id: "execution-1",
        session_id: "session-1",
      },
      current_runtime: {
        execution_id: "execution-1",
        execution_status: "running",
        session_id: "session-1",
        session_status: "active",
        input_mode: "queued",
      },
      ...patch,
    };

    expect(isWritableWorkerWorkspaceSnapshot(snapshot)).toBe(false);
  });

  it("reads Subtask keys from graph nodes independently of WorkerItem keys", () => {
    expect(taskGraphSubtaskKeys([
      { kind: "root", task_id: "root-task" },
      { kind: "child", subtask_key: "implementation", work_item_key: null },
      { kind: "integration", subtask_key: "integration", work_item_key: null },
    ])).toEqual(["implementation", "integration"]);
  });

  it("rejects a Subtask graph node without a stable Subtask key", () => {
    expect(() => taskGraphSubtaskKeys([
      { kind: "child", work_item_key: "source-a" },
    ])).toThrow("task_graph_subtask_key_invalid");
  });

  it("joins an input by idempotency key and verifies its execution session", () => {
    const rows = [
      {
        id: "input-other",
        client_idempotency_key: "other-key",
        execution_id: "execution-1",
        session_id: "session-1",
        status: "delivered",
      },
      {
        id: "input-target",
        client_idempotency_key: "target-key",
        execution_id: "execution-1",
        session_id: "session-1",
        status: "queued",
      },
    ];

    expect(findExactExecutionInput(rows, {
      clientIdempotencyKey: "target-key",
      executionId: "execution-1",
      sessionId: "session-1",
    })).toEqual(rows[1]);
  });

  it("rejects an idempotency-key match bound to a different live session", () => {
    expect(() => findExactExecutionInput([{
      id: "input-target",
      client_idempotency_key: "target-key",
      execution_id: "execution-2",
      session_id: "session-2",
      status: "delivered",
    }], {
      clientIdempotencyKey: "target-key",
      executionId: "execution-1",
      sessionId: "session-1",
    })).toThrow("intervention_input_context_mismatch");
  });

  it("selects the first completed Worker turn after the input checkpoint", () => {
    expect(findPostInputTurnCompletionSequence([
      { sequence: 8, title: "回合已完成" },
      { sequence: 12, title: "执行中", artifact_refs: ["artifact-after"] },
      { sequence: 14, title: "回合已完成" },
      { sequence: 16, title: "回合已完成" },
    ], 9)).toBe(14);
  });

  it("does not accept an artifact or completed turn before the input checkpoint", () => {
    expect(findPostInputTurnCompletionSequence([
      { sequence: 4, title: "回合已完成", artifact_refs: ["artifact-before"] },
      { sequence: 5, title: "正在执行" },
    ], 5)).toBeNull();
  });

  it("keeps only non-target source result identities for locality comparison", () => {
    expect(unaffectedSourceResultIds([
      { snapshot_id: "snapshot-target", worker_item_id: "worker-target" },
      { snapshot_id: "snapshot-sibling-a", worker_item_id: "worker-sibling-a" },
      { snapshot_id: "snapshot-sibling-b", worker_item_id: "worker-sibling-b" },
    ], "worker-target")).toEqual([
      "snapshot-sibling-a",
      "snapshot-sibling-b",
    ]);
  });

  it("classifies plan transitions without converting unknown operations into failures", () => {
    expect(classifyPlanTransitionStatus({ status: "converged", workers: [] })).toBe("converged");
    expect(classifyPlanTransitionStatus({ status: "converging", workers: [] })).toBe("confirmed");
    expect(classifyPlanTransitionStatus({
      status: "degraded",
      workers: [
        { status: "failed", writer_state: "idle" },
        { status: "blocked_unknown", writer_state: "unknown" },
      ],
    })).toBe("unknown");
    expect(classifyPlanTransitionStatus({
      status: "degraded",
      workers: [{ status: "failed", writer_state: "idle" }],
    })).toBe("failed");
    expect(classifyPlanTransitionStatus({ status: "degraded", workers: [] })).toBe("unknown");
    expect(classifyPlanTransitionStatus({ status: "unexpected", workers: [] })).toBe("unknown");
  });
});
