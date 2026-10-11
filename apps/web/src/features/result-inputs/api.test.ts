import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";

import { workerResultInputsFixture } from "@/test/resultInputFixtures";
import { server } from "@/test/server";

import {
  executeResultInputAction,
  resultInputActionPayload,
  retryContextAssemblyAction,
} from "./api";

describe("result input actions", () => {
  beforeEach(() => {
    server.resetHandlers();
  });

  it("exports the existing payload projection with its fixed approval Selection", () => {
    const action = workerResultInputsFixture().available_actions[0];
    if (!action) throw new Error("Missing approval action");
    expect(resultInputActionPayload(action, "Reviewed")).toEqual({
      transport: "web", reason: "Reviewed",
      expected_input_selection_id: "61000000-0000-0000-0000-000000000001",
      expected_input_selection_hash: "a".repeat(64),
    });
  });

  it("sends the exact context assembly identity from the server view", async () => {
    const view = workerResultInputsFixture();
    view.delivery.assembly_request_id = "61000000-0000-0000-0000-000000000025";
    view.delivery.assembly_status = "blocked";
    view.delivery.assembly_version = 4;
    view.delivery.assembly_request_fingerprint = "d".repeat(64);
    const action = retryContextAssemblyAction(view);
    if (action === null) throw new Error("expected a retry action");
    let requestBody: Record<string, unknown> | null = null;
    server.use(
      http.post("/api/v1/commands", async ({ request }) => {
        requestBody = await request.json() as Record<string, unknown>;
        return HttpResponse.json({
          aggregate_id: view.worker_item_id,
          aggregate_version: 5,
          command_id: "61000000-0000-0000-0000-000000000026",
          event_ids: [],
        });
      }),
    );

    await executeResultInputAction(view.workspace_id, action);

    expect(requestBody).toMatchObject({
      workspace_id: view.workspace_id,
      command_type: "RetryContextAssembly",
      target_type: "context_assembly",
      target_id: "61000000-0000-0000-0000-000000000025",
      expected_version: 4,
      payload: { request_fingerprint: "d".repeat(64) },
    });
  });

  it("refuses to send an action without an expected version", async () => {
    const sourceAction = workerResultInputsFixture().available_actions[0];
    if (sourceAction === undefined) throw new Error("expected a fixture action");
    const action = {
      ...sourceAction,
      expected_version: null,
    };

    await expect(
      executeResultInputAction("61000000-0000-0000-0000-000000000002", action),
    ).rejects.toThrow("缺少目标版本");
  });
});
