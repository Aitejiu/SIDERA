import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it } from "vitest";

import { server } from "@/test/server";

import { createThread, getTurn, startTurn } from "./api";

const THREAD_ID = "thread-api";

afterEach(() => {
  document.cookie = "sidera_csrf=; Max-Age=0; Path=/";
});

describe("Main Agent chat API", () => {
  it("sends the exact profile and shared CSRF on creation but not a view read", async () => {
    document.cookie = "sidera_csrf=chat-owned; Path=/";
    const profileRef = { workspace_id: "20000000-0000-0000-0000-000000000001", profile_id: "40000000-0000-0000-0000-000000000001", revision: 3 };
    const reads: Array<{ method: string; csrf: string | null; credentials: string }> = [];
    let payload: unknown;
    server.use(
      http.post("/main-agent/v1/threads", async ({ request }) => {
        payload = await request.json();
        reads.push({ method: request.method, csrf: request.headers.get("X-Sidera-CSRF"), credentials: request.credentials });
        return HttpResponse.json({ id: THREAD_ID, profileRef });
      }),
      http.get(`/main-agent/v1/threads/${THREAD_ID}/turns/original`, ({ request }) => {
        reads.push({ method: request.method, csrf: request.headers.get("X-Sidera-CSRF"), credentials: request.credentials });
        return HttpResponse.json({ id: "original" });
      }),
    );
    await createThread({ title: "Pinned", contextRefs: [], profileRef });
    await getTurn(THREAD_ID, "original");
    expect(payload).toEqual({ title: "Pinned", contextRefs: [], profileRef });
    expect(reads).toEqual([
      { method: "POST", csrf: "chat-owned", credentials: "same-origin" },
      { method: "GET", csrf: null, credentials: "same-origin" },
    ]);
  });

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
