import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it } from "vitest";

import { server } from "@/test/server";
import type { AgentBindingView, NativeWorkerContextView } from "./types";
import {
  assignmentQueryKey,
  assignmentTargetKey,
  nativeWorkerContextQueryKey,
  readAssignment,
  readNativeWorkerContext,
} from "./api";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const OTHER_WORKSPACE_ID = "20000000-0000-0000-0000-000000000002";
const SUBTASK_ID = "30000000-0000-0000-0000-000000000001";
const WORKER_ITEM_ID = "40000000-0000-0000-0000-000000000001";
const BASE = `/api/v1/workspaces/${WORKSPACE_ID}`;

const binding: AgentBindingView = {
  workspace_id: WORKSPACE_ID,
  target_type: "subtask",
  target_id: SUBTASK_ID,
  assignee: { kind: "human", id: "principal-1" },
  profile_ref: null,
  version: 2,
  created_at: "2026-10-06T12:00:00Z",
  changed_at: "2026-10-06T12:00:00Z",
  runnable: false,
  not_runnable_reasons: [],
};

const context: NativeWorkerContextView = {
  workspace_id: WORKSPACE_ID,
  worker_item_id: WORKER_ITEM_ID,
} as NativeWorkerContextView;

afterEach(() => server.resetHandlers());

describe("assignment scoped reads", () => {
  it("keys cached reads by workspace and exact target identity", () => {
    expect(assignmentTargetKey("subtask", "review")).toBe('["subtask","review"]');
    expect(assignmentQueryKey(WORKSPACE_ID, "subtask", SUBTASK_ID)).toEqual([
      "agent-assignment",
      WORKSPACE_ID,
      "subtask",
      SUBTASK_ID,
    ]);
    expect(assignmentQueryKey(WORKSPACE_ID, "subtask", SUBTASK_ID)).not.toEqual(
      assignmentQueryKey(OTHER_WORKSPACE_ID, "subtask", SUBTASK_ID),
    );
    expect(nativeWorkerContextQueryKey(WORKSPACE_ID, WORKER_ITEM_ID)).toEqual([
      "native-worker-context",
      WORKSPACE_ID,
      WORKER_ITEM_ID,
    ]);
    expect(nativeWorkerContextQueryKey(WORKSPACE_ID, WORKER_ITEM_ID)).not.toEqual(
      nativeWorkerContextQueryKey(WORKSPACE_ID, `${WORKER_ITEM_ID}-other`),
    );
  });

  it("GETs the encoded assignment target and preserves a legitimate null binding", async () => {
    let seenPath = "";
    server.use(http.get(/\/api\/v1\/workspaces\/.*\/assignments\/subtask\/.*/, ({ request }) => {
      seenPath = new URL(request.url).pathname;
      expect(request.method).toBe("GET");
      return HttpResponse.json(null);
    }));

    await expect(readAssignment("workspace/one", "subtask", "target/one")).resolves.toBeNull();
    expect(seenPath).toBe(
      "/api/v1/workspaces/workspace%2Fone/assignments/subtask/target%2Fone",
    );
  });

  it("forwards AbortSignal to the native context GET and validates its scope", async () => {
    const controller = new AbortController();
    let receivedSignalAborted: boolean | null = null;
    server.use(http.get(`${BASE}/worker-items/${WORKER_ITEM_ID}/native-context`, ({ request }) => {
      receivedSignalAborted = request.signal.aborted;
      return HttpResponse.json(context);
    }));

    await expect(readNativeWorkerContext(WORKSPACE_ID, WORKER_ITEM_ID, controller.signal))
      .resolves.toEqual(context);
    expect(receivedSignalAborted).toBe(false);
  });

  it.each([
    ["workspace", { workspace_id: OTHER_WORKSPACE_ID }],
    ["kind", { target_type: "worker_item" }],
    ["target", { target_id: WORKER_ITEM_ID }],
  ] as const)("rejects an assignment returned for another %s", async (_name, mismatch) => {
    server.use(http.get(`${BASE}/assignments/subtask/${SUBTASK_ID}`, () =>
      HttpResponse.json({ ...binding, ...mismatch }),
    ));

    await expect(readAssignment(WORKSPACE_ID, "subtask", SUBTASK_ID))
      .rejects.toMatchObject({ code: "assignment_unavailable" });
    await expect(readAssignment(WORKSPACE_ID, "subtask", SUBTASK_ID))
      .rejects.not.toThrow("PRIVATE_DRIVER_DETAIL");
  });

  it.each([
    ["workspace", { workspace_id: OTHER_WORKSPACE_ID }],
    ["WorkerItem", { worker_item_id: `${WORKER_ITEM_ID}-other` }],
  ] as const)("rejects a native context returned for another %s", async (_name, mismatch) => {
    server.use(http.get(`${BASE}/worker-items/${WORKER_ITEM_ID}/native-context`, () =>
      HttpResponse.json({ ...context, ...mismatch }),
    ));

    await expect(readNativeWorkerContext(WORKSPACE_ID, WORKER_ITEM_ID))
      .rejects.toMatchObject({ code: "assignment_unavailable" });
  });

  it.each([
    ["denied", 403],
    ["unavailable", 503],
  ] as const)("returns a safe error for %s read responses", async (_kind, status) => {
    server.use(http.get(`${BASE}/assignments/subtask/${SUBTASK_ID}`, () =>
      HttpResponse.json({ detail: { message: "PRIVATE_DRIVER_DETAIL" } }, { status }),
    ));

    await expect(readAssignment(WORKSPACE_ID, "subtask", SUBTASK_ID))
      .rejects.toMatchObject({ code: "assignment_unavailable" });
    await expect(readAssignment(WORKSPACE_ID, "subtask", SUBTASK_ID))
      .rejects.not.toThrow("PRIVATE_DRIVER_DETAIL");
  });
});
