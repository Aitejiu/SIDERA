import { beforeEach, describe, expect, it } from "vitest";
import { http, HttpResponse } from "msw";

import { AssignmentUnavailableError } from "@/features/assignments/types";
import { server } from "@/test/server";
import type { ExecutionInput, ExecutionInputPage } from "./types";
import { readNativeExecutionInputPage } from "./nativeInputApi";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const EXECUTION_ID = "60000000-0000-0000-0000-000000000001";
const SESSION_ID = "70000000-0000-0000-0000-000000000001";
const INPUT_ID = "50000000-0000-0000-0000-000000000001";
const CLIENT_KEY = "12000000-0000-0000-0000-000000000001";

function input(status: string = "queued"): ExecutionInput {
  return {
    id: INPUT_ID,
    workspace_id: WORKSPACE_ID,
    execution_id: EXECUTION_ID,
    session_id: SESSION_ID,
    client_idempotency_key: CLIENT_KEY,
    status,
    operation_id: null,
    failure_code: null,
    version: 1,
    text_length: 12,
    attachment_count: 0,
    created_at: "2026-10-07T00:00:00Z",
    updated_at: "2026-10-07T00:00:00Z",
  };
}

beforeEach(() => {
  server.resetHandlers();
});

describe("readNativeExecutionInputPage", () => {
  it("requests exact scope and preserves the actual cursor for page two", async () => {
    const queries: Array<Record<string, string | null>> = [];
    server.use(http.get("/api/v1/gateway/inputs", ({ request }) => {
      const params = new URL(request.url).searchParams;
      queries.push({
        workspace_id: params.get("workspace_id"),
        execution_id: params.get("execution_id"),
        session_id: params.get("session_id"),
        limit: params.get("limit"),
        cursor: params.get("cursor"),
        names: [...params.keys()].sort().join(","),
      });
      if (params.get("cursor") === "2") {
        return HttpResponse.json({ items: [input("delivered")], next_cursor: null } satisfies ExecutionInputPage);
      }
      return HttpResponse.json({ items: [], next_cursor: "2" } satisfies ExecutionInputPage);
    }));

    const first = await readNativeExecutionInputPage(WORKSPACE_ID, EXECUTION_ID, SESSION_ID, { limit: 2 });
    expect(first.items).toEqual([]);
    expect(first.next_cursor).toBe("2");
    const nextCursor = first.next_cursor;
    if (nextCursor === null) throw new Error("Expected a next cursor");

    const second = await readNativeExecutionInputPage(WORKSPACE_ID, EXECUTION_ID, SESSION_ID, {
      cursor: nextCursor,
      limit: 2,
    });
    expect(second.items.map((item) => item.id)).toEqual([INPUT_ID]);
    expect(queries).toEqual([
      {
        workspace_id: WORKSPACE_ID,
        execution_id: EXECUTION_ID,
        session_id: SESSION_ID,
        limit: "2",
        cursor: null,
        names: "execution_id,limit,session_id,workspace_id",
      },
      {
        workspace_id: WORKSPACE_ID,
        execution_id: EXECUTION_ID,
        session_id: SESSION_ID,
        limit: "2",
        cursor: "2",
        names: "cursor,execution_id,limit,session_id,workspace_id",
      },
    ]);
  });

  it("defaults to a page size of 100", async () => {
    let requestedLimit: string | null = null;
    server.use(http.get("/api/v1/gateway/inputs", ({ request }) => {
      requestedLimit = new URL(request.url).searchParams.get("limit");
      return HttpResponse.json({ items: [], next_cursor: null } satisfies ExecutionInputPage);
    }));

    await readNativeExecutionInputPage(WORKSPACE_ID, EXECUTION_ID, SESSION_ID);

    expect(requestedLimit).toBe("100");
  });

  it.each([
    "accepted",
    "queued",
    "dispatching",
    "delivered",
    "blocked",
    "failed",
    "cancelled",
    "superseded",
  ])("accepts the public delivery status %s", async (status) => {
    server.use(http.get("/api/v1/gateway/inputs", () => HttpResponse.json({
      items: [input(status)],
      next_cursor: null,
    } satisfies ExecutionInputPage)));

    const page = await readNativeExecutionInputPage(WORKSPACE_ID, EXECUTION_ID, SESSION_ID);

    expect(page.items[0]?.status).toBe(status);
  });

  it("rejects a foreign scoped item instead of returning it", async () => {
    server.use(http.get("/api/v1/gateway/inputs", () => HttpResponse.json({
      items: [{ ...input(), workspace_id: "20000000-0000-0000-0000-000000000099" }],
      next_cursor: null,
    })));

    await expect(readNativeExecutionInputPage(WORKSPACE_ID, EXECUTION_ID, SESSION_ID))
      .rejects.toBeInstanceOf(AssignmentUnavailableError);
  });

  it("rejects unknown delivery status and malformed page shape", async () => {
    server.use(http.get("/api/v1/gateway/inputs", () => HttpResponse.json({
      items: [input("unknown")],
      next_cursor: null,
    })));
    await expect(readNativeExecutionInputPage(WORKSPACE_ID, EXECUTION_ID, SESSION_ID))
      .rejects.toBeInstanceOf(AssignmentUnavailableError);

    server.use(http.get("/api/v1/gateway/inputs", () => HttpResponse.json({ items: [] })));
    await expect(readNativeExecutionInputPage(WORKSPACE_ID, EXECUTION_ID, SESSION_ID))
      .rejects.toBeInstanceOf(AssignmentUnavailableError);
  });

  it("rejects out-of-range limits before making a request", async () => {
    let calls = 0;
    server.use(http.get("/api/v1/gateway/inputs", () => {
      calls += 1;
      return HttpResponse.json({ items: [], next_cursor: null });
    }));

    await expect(readNativeExecutionInputPage(WORKSPACE_ID, EXECUTION_ID, SESSION_ID, { limit: 0 }))
      .rejects.toBeInstanceOf(AssignmentUnavailableError);
    await expect(readNativeExecutionInputPage(WORKSPACE_ID, EXECUTION_ID, SESSION_ID, { limit: 101 }))
      .rejects.toBeInstanceOf(AssignmentUnavailableError);
    expect(calls).toBe(0);
  });

  it.each([
    ["zero version", { version: 0 }],
    ["negative version", { version: -1 }],
    ["fractional version", { version: 1.5 }],
    ["unsafe version", { version: Number.MAX_SAFE_INTEGER + 1 }],
    ["negative length", { text_length: -1 }],
    ["fractional length", { text_length: 1.5 }],
    ["negative attachments", { attachment_count: -1 }],
    ["fractional attachments", { attachment_count: 1.5 }],
    ["non-UUID input", { id: "not-a-uuid" }],
    ["non-UUID operation", { operation_id: "not-a-uuid" }],
  ])("rejects malformed public metadata: %s", async (_name, changes) => {
    server.use(http.get("/api/v1/gateway/inputs", () => HttpResponse.json({
      items: [{ ...input(), ...changes }],
      next_cursor: null,
    })));

    await expect(readNativeExecutionInputPage(WORKSPACE_ID, EXECUTION_ID, SESSION_ID))
      .rejects.toBeInstanceOf(AssignmentUnavailableError);
  });

  it("accepts a UUID operation and an opaque nonempty client key", async () => {
    server.use(http.get("/api/v1/gateway/inputs", () => HttpResponse.json({
      items: [{
        ...input(),
        operation_id: "13000000-0000-0000-0000-000000000001",
        client_idempotency_key: "opaque-input-key",
      }],
      next_cursor: null,
    })));

    const page = await readNativeExecutionInputPage(WORKSPACE_ID, EXECUTION_ID, SESSION_ID);

    expect(page.items[0]?.operation_id).toBe("13000000-0000-0000-0000-000000000001");
    expect(page.items[0]?.client_idempotency_key).toBe("opaque-input-key");
  });
});
