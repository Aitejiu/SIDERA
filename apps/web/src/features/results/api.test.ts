import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { ApiProblem } from "@/shared/api/client";
import { server } from "@/test/server";

import {
  executeTaskResultAction,
  getTaskResults,
  type ResultCommandIdentity,
  taskResultsQueryKey,
} from "./api";
import {
  completeTaskInput,
  RESULT_CANDIDATE_ID,
  RESULT_ROOT_TASK_ID,
  RESULT_WORKSPACE_ID,
  resultAction,
  resultView,
} from "./testFixtures";

describe("task result API", () => {
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
