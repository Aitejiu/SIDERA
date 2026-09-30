import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { server } from "@/test/server";

import { startTurn } from "./api";

const THREAD_ID = "thread-api";

describe("Main Agent chat API", () => {
  it("sends the caller-provided operation id with the turn input", async () => {
    let body: Record<string, unknown> | undefined;
    const response = {
      id: "turn-api",
      threadId: THREAD_ID,
      status: "completed",
      createdAt: "2026-09-18T10:00:00Z",
      startedAt: "2026-09-18T10:00:01Z",
      completedAt: "2026-09-18T10:00:02Z",
      finalResponse: "ok",
      items: [],
      usage: null,
      error: null,
      streamAvailable: false,
      recoveryMode: null,
    };
    server.use(
      http.post(`/main-agent/v1/threads/${THREAD_ID}/turns`, async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(response, { status: 202 });
      }),
    );

    await expect(startTurn(THREAD_ID, "保留这条需求", "operation-api-1")).resolves.toEqual(response);
    expect(body).toEqual({ input: "保留这条需求", operationId: "operation-api-1" });
  });
});
