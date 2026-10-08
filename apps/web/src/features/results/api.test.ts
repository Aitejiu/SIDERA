import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";

import type { LocalSession } from "@/features/local-session/api";
import { ApiProblem } from "@/shared/api/client";
import { server } from "@/test/server";

import {
  buildNativeResultOperationCommand,
  buildNativeTaskAcceptanceCommand,
  executeTaskResultAction,
  getTaskResults,
  resultRefreshInterval,
  type ResultCommandIdentity,
  type ResultAction,
  type NativeResultOperationInput,
  taskResultsQueryKey,
} from "./api";
import {
  completeTaskInput,
  RESULT_CANDIDATE_ID,
  RESULT_ROOT_TASK_ID,
  RESULT_OPERATION_ID,
  RESULT_WORKSPACE_ID,
  resultAction,
  resultView,
} from "./testFixtures";

const workerItemId = "71000000-0000-0000-0000-000000000021";
const replacementId = "71000000-0000-0000-0000-000000000031";
const operationSession: LocalSession = {
  session_id: "current-human-session", workspace_id: RESULT_WORKSPACE_ID,
  principal_id: "human-current", expires_at: "2099-01-01T00:00:00Z",
};
const operationCases: NativeResultOperationInput[] = [
  { command_type: "RequestTaskRework", payload: {
    candidate_id: RESULT_CANDIDATE_ID, candidate_hash: "c".repeat(64), mode: "user",
    issue_keys: ["expired-credentials"], target_worker_item_ids: [workerItemId],
    impact_worker_item_ids: [workerItemId], reason: "Reviewed local repair", suggestion: null,
  } },
  { command_type: "AuthorizeAdditionalRework", payload: {
    additional_rounds: 1, additional_seconds: 300, reason: "Reviewed increment",
  } },
  { command_type: "RetryResultVerification", payload: {
    verification_operation_id: RESULT_OPERATION_ID, expected_operation_version: 3,
    expected_result_version: 7, candidate_id: RESULT_CANDIDATE_ID,
    candidate_hash: "c".repeat(64), reason: "Reviewed retry",
  } },
  { command_type: "ReintegrateTaskResult", payload: {
    source_candidate_id: RESULT_CANDIDATE_ID, source_candidate_hash: "c".repeat(64),
    replacement_result_ids: [replacementId], expected_result_version: 7,
    target_observation_id: null, reason: "Reviewed reintegration",
  } },
];

function operationAction(input: NativeResultOperationInput): ResultAction {
  return {
    ...resultAction("RequestTaskRework"), command_type: input.command_type,
    target_type: input.command_type === "RetryResultVerification" ? "result_operation" : "task",
    target_id: input.command_type === "RetryResultVerification" ? RESULT_OPERATION_ID : RESULT_ROOT_TASK_ID,
    expected_version: input.command_type === "RetryResultVerification" ? 3 : 7,
    allowed_worker_item_ids: input.command_type === "RequestTaskRework" ? [workerItemId] : [],
    replacement_result_ids: input.command_type === "ReintegrateTaskResult" ? [replacementId] : [],
  };
}

describe("native Worker result-operation builder", () => {
  it.each(operationCases)("freezes the current human's $command_type payload without POST", (input) => {
    let posts = 0;
    server.use(http.post("/api/v1/commands", () => { posts += 1; return HttpResponse.error(); }));
    const reviewed = operationAction(input);
    const command = buildNativeResultOperationCommand(
      RESULT_WORKSPACE_ID, RESULT_ROOT_TASK_ID, workerItemId, reviewed, input, operationSession,
    );
    expect(command).toEqual({
      command_id: expect.any(String), workspace_id: RESULT_WORKSPACE_ID,
      command_type: input.command_type, actor: { type: "user", id: "human-current" },
      target_type: input.command_type === "RetryResultVerification" ? "result_operation" : "task",
      target_id: input.command_type === "RetryResultVerification" ? RESULT_OPERATION_ID : RESULT_ROOT_TASK_ID,
      expected_version: input.command_type === "RetryResultVerification" ? 3 : 7,
      idempotency_key: expect.any(String), correlation_id: expect.any(String), payload: input.payload,
    });
    expect(command.expected_version).not.toBe(11);
    expect(Object.isFrozen(command.payload)).toBe(true);
    expect(command.payload).not.toBe(input.payload);
    expect(new Set([command.command_id, command.idempotency_key, command.correlation_id]).size).toBe(3);
    expect(posts).toBe(0);
  });

  it.each<[string, Partial<ResultAction>]>([
    ["command", { command_type: "CompleteTask" }],
    ["target type", { target_type: "worker_item" }],
    ["root target", { target_id: RESULT_OPERATION_ID }],
    ["candidate", { candidate_id: RESULT_OPERATION_ID }],
    ["hash", { candidate_hash: "b".repeat(64) }],
    ["fingerprint", { expected_fingerprint: "b".repeat(64) }],
    ["result version", { expected_result_version: 8 }],
    ["root head version", { expected_version: 11 }],
    ["unsafe version", { expected_version: Number.MAX_SAFE_INTEGER + 1 }],
    ["disallowed WorkerItem", { allowed_worker_item_ids: [RESULT_OPERATION_ID] }],
  ])("rejects changed %s before identity allocation", (_label, override) => {
    const identity = vi.spyOn(crypto, "randomUUID");
    try {
      const input = operationCases[0]!;
      expect(() => buildNativeResultOperationCommand(RESULT_WORKSPACE_ID, RESULT_ROOT_TASK_ID, workerItemId,
        { ...operationAction(input), ...override }, input, operationSession)).toThrow();
      expect(identity).not.toHaveBeenCalled();
    } finally { identity.mockRestore(); }
  });

  it.each([
    { verification_operation_id: RESULT_CANDIDATE_ID },
    { expected_operation_version: 4 },
    { expected_result_version: 8 },
  ])("rejects a changed independent retry fence before identity", (override) => {
    const input = operationCases[2]! as Extract<NativeResultOperationInput, { command_type: "RetryResultVerification" }>;
    const identity = vi.spyOn(crypto, "randomUUID");
    try {
      expect(() => buildNativeResultOperationCommand(RESULT_WORKSPACE_ID, RESULT_ROOT_TASK_ID, workerItemId,
        operationAction(input), { ...input, payload: { ...input.payload, ...override } }, operationSession)).toThrow();
      expect(identity).not.toHaveBeenCalled();
    } finally { identity.mockRestore(); }
  });

  it.each([
    { additional_rounds: -1, additional_seconds: 300, reason: "reviewed" },
    { additional_rounds: 1.5, additional_seconds: 300, reason: "reviewed" },
    { additional_rounds: 0, additional_seconds: 0, reason: "reviewed" },
    { additional_rounds: 1, additional_seconds: 300, reason: " " },
    { additional_rounds: 1, additional_seconds: 300, reason: "reviewed", private_token: "private" },
  ])("rejects an invalid budget before identity", (payload) => {
    const input = operationCases[1]!;
    const identity = vi.spyOn(crypto, "randomUUID");
    try {
      expect(() => buildNativeResultOperationCommand(RESULT_WORKSPACE_ID, RESULT_ROOT_TASK_ID, workerItemId,
        operationAction(input), { command_type: "AuthorizeAdditionalRework", payload }, operationSession)).toThrow();
      expect(identity).not.toHaveBeenCalled();
    } finally { identity.mockRestore(); }
  });

  it("keeps the empty allowed-list behavior but rejects broader rework and changed replacements", () => {
    const rework = operationCases[0]! as Extract<NativeResultOperationInput, { command_type: "RequestTaskRework" }>;
    expect(buildNativeResultOperationCommand(RESULT_WORKSPACE_ID, RESULT_ROOT_TASK_ID, workerItemId,
      { ...operationAction(rework), allowed_worker_item_ids: [] }, rework, operationSession).payload.mode).toBe("user");
    const reintegrate = operationCases[3]! as Extract<NativeResultOperationInput, { command_type: "ReintegrateTaskResult" }>;
    const identity = vi.spyOn(crypto, "randomUUID");
    try {
      for (const payload of [
        { ...rework.payload, mode: "automatic" as const },
        { ...rework.payload, impact_worker_item_ids: [workerItemId, RESULT_OPERATION_ID] },
        { ...rework.payload, target_worker_item_ids: [RESULT_OPERATION_ID] },
      ]) {
        expect(() => buildNativeResultOperationCommand(RESULT_WORKSPACE_ID, RESULT_ROOT_TASK_ID, workerItemId,
          operationAction(rework), { ...rework, payload }, operationSession)).toThrow();
      }
      expect(() => buildNativeResultOperationCommand(RESULT_WORKSPACE_ID, RESULT_ROOT_TASK_ID, workerItemId,
        operationAction(reintegrate), { ...reintegrate, payload: { ...reintegrate.payload, replacement_result_ids: [RESULT_OPERATION_ID] } }, operationSession)).toThrow();
      expect(identity).not.toHaveBeenCalled();
    } finally { identity.mockRestore(); }
  });

  it.each([
    { ...operationSession, principal_id: "" },
    { ...operationSession, workspace_id: RESULT_OPERATION_ID },
    { ...operationSession, expires_at: "2000-01-01T00:00:00Z" },
  ])("rejects unavailable human authority before identity", (session) => {
    const identity = vi.spyOn(crypto, "randomUUID");
    try {
      const input = operationCases[0]!;
      expect(() => buildNativeResultOperationCommand(RESULT_WORKSPACE_ID, RESULT_ROOT_TASK_ID, workerItemId,
        operationAction(input), input, session)).toThrow();
      expect(identity).not.toHaveBeenCalled();
    } finally { identity.mockRestore(); }
  });
});

describe("native task acceptance builder", () => {
  const session: LocalSession = {
    session_id: "human-session-current",
    workspace_id: RESULT_WORKSPACE_ID,
    principal_id: "human-current",
    expires_at: "2099-01-01T00:00:00Z",
  };

  it("builds the public frozen human command without submitting", () => {
    let posts = 0;
    server.use(http.post("/api/v1/commands", () => {
      posts += 1;
      return HttpResponse.error();
    }));
    const input = completeTaskInput();
    const command = buildNativeTaskAcceptanceCommand(RESULT_WORKSPACE_ID, resultAction(), input.payload, session);
    expect(command).toEqual({
      command_id: expect.any(String),
      workspace_id: RESULT_WORKSPACE_ID,
      command_type: "CompleteTask",
      actor: { type: "user", id: "human-current" },
      target_type: "task",
      target_id: RESULT_ROOT_TASK_ID,
      expected_version: 11,
      idempotency_key: expect.any(String),
      correlation_id: expect.any(String),
      payload: {
        candidate_id: RESULT_CANDIDATE_ID,
        candidate_hash: "c".repeat(64),
        acceptance_basis_hash: "f".repeat(64),
        expected_result_version: 7,
        criteria: [{ criterion_key: "expired-credentials", decision: "accept_passed", reason: null }],
      },
    });
    expect(new Set([command.command_id, command.idempotency_key, command.correlation_id]).size).toBe(3);
    input.payload.criteria[0]!.reason = "later form change";
    expect((command.payload.criteria as { reason: string | null }[])[0]?.reason).toBeNull();
    expect(Object.isFrozen(command)).toBe(true);
    expect(posts).toBe(0);
  });

  it.each<[string, Partial<ResultAction>]>([
    ["command", { command_type: "RequestTaskRework" }],
    ["target type", { target_type: "result_operation" }],
    ["missing target", { target_id: "" }],
    ["non-resource target", { target_id: "root" }],
    ["missing candidate", { candidate_id: null }],
    ["candidate", { candidate_id: "71000000-0000-0000-0000-000000000099" }],
    ["hash", { candidate_hash: "b".repeat(64) }],
    ["result version", { expected_result_version: 8 }],
    ["task version", { expected_version: 0 }],
  ])("rejects mismatched %s before identity or POST", (_label, override) => {
    const identity = vi.spyOn(crypto, "randomUUID");
    try {
      expect(() => buildNativeTaskAcceptanceCommand(
        RESULT_WORKSPACE_ID, { ...resultAction(), ...override }, completeTaskInput().payload, session,
      )).toThrow();
      expect(identity).not.toHaveBeenCalled();
    } finally {
      identity.mockRestore();
    }
  });

  it.each([
    { ...session, expires_at: "2000-01-01T00:00:00Z" },
    { ...session, workspace_id: "71000000-0000-0000-0000-000000000099" },
    { ...session, principal_id: "" },
    { ...session, session_id: "" },
  ])("rejects expired or foreign authority before identity", (invalid) => {
    const identity = vi.spyOn(crypto, "randomUUID");
    try {
      expect(() => buildNativeTaskAcceptanceCommand(
        RESULT_WORKSPACE_ID, resultAction(), completeTaskInput().payload, invalid,
      )).toThrow();
      expect(identity).not.toHaveBeenCalled();
    } finally {
      identity.mockRestore();
    }
  });
});

describe("task result API", () => {
  it("refreshes an in-progress or not-yet-materialized result, then stops", () => {
    expect(resultRefreshInterval(resultView({ result_stage: "verifying", current_candidate: null }))).toBe(1_000);
    expect(resultRefreshInterval(resultView({ result_stage: "reworking" }))).toBe(1_000);
    expect(resultRefreshInterval(resultView({ result_stage: "awaiting_acceptance" }))).toBe(false);
    expect(resultRefreshInterval(resultView({ result_stage: "completed", current_candidate: null }))).toBe(false);
    expect(resultRefreshInterval(undefined)).toBe(false);
  });

  it("scopes result caches to their task", () => {
    expect(taskResultsQueryKey("w1", "t1")).toEqual(["task-results", "w1", "t1"]);
    expect(taskResultsQueryKey("w1", "t2")).not.toEqual(taskResultsQueryKey("w1", "t1"));
  });

  it("reads the bounded result overview with its cursor", async () => {
    const requestUrls: URL[] = [];
    server.use(
      http.get("/api/v1/workspaces/:workspaceId/tasks/:rootTaskId/results", ({ request }) => {
        requestUrls.push(new URL(request.url));
        return HttpResponse.json(resultView());
      }),
    );

    const view = await getTaskResults(RESULT_WORKSPACE_ID, RESULT_ROOT_TASK_ID, {
      cursor: "19",
      limit: 20,
    });

    expect(view.root_task_id).toBe(RESULT_ROOT_TASK_ID);
    expect(requestUrls[0]?.search).toBe("?cursor=19&limit=20");
  });

  it("submits exactly the server action fence and typed command payload", async () => {
    const action = resultAction();
    const input = completeTaskInput();
    const identity: ResultCommandIdentity = {
      commandId: "71000000-0000-0000-0000-000000000012",
      idempotencyKey: "71000000-0000-0000-0000-000000000013",
    };
    const requestBodies: Record<string, unknown>[] = [];
    server.use(
      http.post("/api/v1/commands", async ({ request }) => {
        requestBodies.push(await request.json() as Record<string, unknown>);
        return HttpResponse.json({
          aggregate_id: RESULT_ROOT_TASK_ID,
          aggregate_version: 12,
          command_id: identity.commandId,
          event_ids: [],
        });
      }),
    );

    await executeTaskResultAction(RESULT_WORKSPACE_ID, action, input, identity);

    const requestBody = requestBodies[0];
    expect(requestBody).toMatchObject({
      command_id: identity.commandId,
      workspace_id: RESULT_WORKSPACE_ID,
      command_type: "CompleteTask",
      target_type: "task",
      target_id: RESULT_ROOT_TASK_ID,
      expected_version: 11,
      idempotency_key: identity.idempotencyKey,
      payload: input.payload,
    });
    expect((requestBody?.payload as Record<string, unknown>).candidate_id).toBe(RESULT_CANDIDATE_ID);
    expect((requestBody?.payload as Record<string, unknown>).acceptance_basis_hash).toBe("f".repeat(64));
  });

  it("does not guess a command when the input and server action disagree", async () => {
    await expect(
      executeTaskResultAction(
        RESULT_WORKSPACE_ID,
        resultAction("RequestTaskRework"),
        completeTaskInput(),
      ),
    ).rejects.toThrow("结果动作与命令输入不一致");
  });

  it("preserves the server error code for a stale 409", async () => {
    server.use(
      http.post("/api/v1/commands", () => HttpResponse.json(
        { error: { code: "acceptance_stale", message: "成果已经变化" } },
        { status: 409, headers: { "X-Correlation-Id": "result-correlation-1" } },
      )),
    );

    await expect(
      executeTaskResultAction(
        RESULT_WORKSPACE_ID,
        resultAction(),
        completeTaskInput(),
      ),
    ).rejects.toBeInstanceOf(ApiProblem);

    try {
      await executeTaskResultAction(
        RESULT_WORKSPACE_ID,
        resultAction(),
        completeTaskInput(),
      );
    } catch (error) {
      expect(error).toMatchObject({
        status: 409,
        code: "acceptance_stale",
        correlationId: "result-correlation-1",
      });
    }
  });
});
