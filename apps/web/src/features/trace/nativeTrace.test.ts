import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import type { AgentExecutionView, NativeWorkerTraceView } from "@/features/assignments/types";
import { server } from "@/test/server";
import {
  NativeTraceUnavailableError,
  readNativeTracePage,
  readSavedTraceExecution,
  reviewTraceSelection,
  type NativeTracePin,
} from "./nativeTrace";
import type { Execution, ExecutionSession, NativeRunEvent } from "./types";

const WS = "20000000-0000-0000-0000-000000000001";
const ROOT = "30000000-0000-0000-0000-000000000001";
const TASK = "40000000-0000-0000-0000-000000000001";
const WI = "50000000-0000-0000-0000-000000000001";
const EX = "60000000-0000-0000-0000-000000000001";
const SESSION = "70000000-0000-0000-0000-000000000001";
const RUN = "80000000-0000-0000-0000-000000000001";
const WORKER = "90000000-0000-0000-0000-000000000001";
const PROFILE = "10000000-0000-0000-0000-000000000001";
const PRINCIPAL = "current-human";
const NOW = "2026-10-07T00:00:00Z";
const profileRef = { workspace_id: WS, profile_id: PROFILE, revision: 1 };
const pin: NativeTracePin = {
  workspaceId: WS, workerItemId: WI, executionId: EX, principalId: PRINCIPAL,
  runId: RUN, profileRef,
};
const worker = {
  id: WORKER, adapter_type: "agent-run", display_name: "Saved worker",
  status: "online", desired_state: "online", capacity: 1, current_load: 1,
};
const execution: Execution = {
  id: EX, workspace_id: WS, task_id: TASK, worker_item_id: WI,
  lineage_id: EX, attempt: 1, max_attempts: 3, purpose: "Selected old execution",
  status: "completed", capability_requirements: [], pinned_worker_id: WORKER,
  assigned_worker_id: WORKER, assigned_worker_summary: worker, pinned_worker_summary: worker,
  binding_summary: null, active_lease_id: null, last_fencing_token: 1,
  not_before: NOW, failure_code: null, started_at: NOW, finished_at: NOW,
  version: 7, stop_requested: false, source_execution_id: null, source_session_id: null,
  created_at: NOW, updated_at: NOW, adapter_forbidden: [], adapter_preferences: [],
  blocked_since: null, requires_operator: false, scheduling_status: "completed",
  waiting_details_summary: null, waiting_reason: null,
};
const saved: AgentExecutionView = {
  workspace_id: WS, execution_id: EX, root_task_id: ROOT, subtask_id: TASK,
  worker_item_id: WI, status: "completed", updated_at: NOW,
  profile_ref: profileRef, binding_version: 2, run_id: RUN,
  link_status: "completed", event_cursor: 12,
};
const session: ExecutionSession = {
  id: SESSION, workspace_id: WS, execution_id: EX, worker_id: WORKER,
  adapter_session_id: `agent-run:${RUN}`, status: "closed", input_mode: "queued",
  last_sequence: 12, last_activity_at: NOW, version: 5,
  adapter_module_id: null, adapter_config_version: null, capability_snapshot: null,
  created_at: NOW, updated_at: NOW, workspace_config_version: null,
  workspace_module_id: null, workspace_ref: null,
};
function event(sequence: number): NativeRunEvent {
  return { run_id: RUN, sequence, kind: "assistant.message", occurred_at: NOW,
    operation_id: null, response_id: null, data: { text: "Checking the dependency" } };
}
function page(): NativeWorkerTraceView {
  return { workspace_id: WS, worker_item_id: WI, execution_id: EX, run_id: RUN,
    profile_ref: profileRef, events: [event(3), event(8)], next_sequence: 8, has_more: true };
}
function review(overrides: Partial<Parameters<typeof reviewTraceSelection>[0]> = {}) {
  return reviewTraceSelection({ workspaceId: WS,
    address: { rootTaskId: ROOT, executionId: EX, sessionId: null },
    principalId: PRINCIPAL, execution, agentExecution: saved, session: null, ...overrides });
}

describe("selected execution mode", () => {
  it("keeps saved R1 and RunA with no active Session or current owner input", () => {
    expect(review()).toEqual({ mode: "native", pin });
    expect(review({ address: { rootTaskId: ROOT, executionId: EX, sessionId: "unknown" } }))
      .toEqual({ mode: "native", pin });
  });

  it("accepts the original native Session but rejects a conflicting legacy Session", () => {
    expect(review({ address: { rootTaskId: ROOT, executionId: EX, sessionId: SESSION }, session }))
      .toEqual({ mode: "native", pin });
    expect(() => review({ address: { rootTaskId: ROOT, executionId: EX, sessionId: SESSION },
      session: { ...session, adapter_session_id: "legacy-session" } })).toThrow(NativeTraceUnavailableError);
  });

  it("accepts a saved native Run and exact Session when worker summaries are null", () => {
    expect(review({ address: { rootTaskId: ROOT, executionId: EX, sessionId: SESSION }, session,
      execution: { ...execution, assigned_worker_summary: null, pinned_worker_summary: null } }))
      .toEqual({ mode: "native", pin });
  });

  it.each([
    { workspace_id: "foreign" }, { execution_id: "foreign" }, { subtask_id: ROOT },
    { root_task_id: null }, { worker_item_id: null }, { run_id: null },
    { binding_version: 0 }, { profile_ref: { ...profileRef, workspace_id: "foreign" } },
  ])("refuses incomplete or foreign saved native pins %j", (change) => {
    expect(() => review({ agentExecution: { ...saved, ...change } })).toThrow(NativeTraceUnavailableError);
  });

  it("refuses a foreign selected Execution and missing saved evidence", () => {
    expect(() => review({ execution: { ...execution, id: "foreign" } })).toThrow(NativeTraceUnavailableError);
    expect(() => review({ agentExecution: undefined })).toThrow(NativeTraceUnavailableError);
  });

  it("requires positive saved legacy worker and Session evidence, including null Wi/root history", () => {
    const legacyWorker = { ...worker, adapter_type: "opencode" };
    const legacyExecution = { ...execution, worker_item_id: null,
      assigned_worker_summary: legacyWorker, pinned_worker_summary: legacyWorker };
    const legacySaved = { ...saved, root_task_id: null, worker_item_id: null,
      profile_ref: null, binding_version: null, run_id: null, link_status: null, event_cursor: null };
    const legacySession = { ...session, adapter_session_id: "old-opaque-session" };
    const options = { execution: legacyExecution, agentExecution: legacySaved, session: legacySession,
      address: { rootTaskId: ROOT, executionId: EX, sessionId: SESSION } };
    expect(review(options)).toEqual({ mode: "legacy", session: legacySession });
    expect(() => review({ ...options, session: null })).toThrow(NativeTraceUnavailableError);
    expect(() => review({ ...options, session: { ...legacySession, worker_id: PROFILE } }))
      .toThrow(NativeTraceUnavailableError);
    expect(() => review({ ...options, session: { ...legacySession, adapter_session_id: `agent-run:${RUN}` } }))
      .toThrow(NativeTraceUnavailableError);
    expect(() => review({ ...options, execution })).toThrow(NativeTraceUnavailableError);
  });
});

describe("saved execution read", () => {
  it("uses the scoped saved view endpoint and passes the AbortSignal", async () => {
    const controller = new AbortController();
    server.use(http.get(`/api/v1/workspaces/${WS}/agent-executions/${EX}`, ({ request }) => {
      expect(request.method).toBe("GET");
      expect(new URL(request.url).search).toBe("");
      return HttpResponse.json(saved);
    }));
    expect(await readSavedTraceExecution(WS, EX, controller.signal)).toEqual(saved);
  });

  it.each([403, 404, 503])("does not turn failed saved read %i into legacy", async (status) => {
    server.use(http.get(`/api/v1/workspaces/${WS}/agent-executions/${EX}`, () =>
      HttpResponse.json({ error: { code: "permission_denied", message: "private-secret", details: {} } }, { status })));
    await expect(readSavedTraceExecution(WS, EX)).rejects.toMatchObject({
      kind: status === 403 ? "denied" : "unavailable", message: "Trace data is unavailable.",
    });
  });

  it("rejects a saved response for another execution", async () => {
    server.use(http.get(`/api/v1/workspaces/${WS}/agent-executions/${EX}`, () =>
      HttpResponse.json({ ...saved, execution_id: ROOT })));
    await expect(readSavedTraceExecution(WS, EX)).rejects.toBeInstanceOf(NativeTraceUnavailableError);
  });
});

describe("native public page", () => {
  it("reads forward gaps and the actual server cursor, with GETs only", async () => {
    const reads: string[] = [];
    server.use(http.all("*", ({ request }) => {
      reads.push(`${request.method} ${new URL(request.url).pathname} ${new URL(request.url).search}`);
      const after = new URL(request.url).searchParams.get("after_sequence");
      return HttpResponse.json(after === "8" ? { ...page(), events: [event(12)], next_sequence: 12, has_more: false } : page());
    }));
    const first = await readNativeTracePage(pin);
    const second = await readNativeTracePage(pin, { afterSequence: first.next_sequence });
    expect(first.events.map((item) => item.sequence)).toEqual([3, 8]);
    expect(second.events.map((item) => item.sequence)).toEqual([12]);
    expect(reads).toEqual([
      `GET /api/v1/workspaces/${WS}/worker-items/${WI}/executions/${EX}/native-trace ?after_sequence=0&limit=100`,
      `GET /api/v1/workspaces/${WS}/worker-items/${WI}/executions/${EX}/native-trace ?after_sequence=8&limit=100`,
    ]);
  });

  it("retains the cursor on an empty terminal page", async () => {
    server.use(http.get("*/native-trace", () => HttpResponse.json({ ...page(), events: [], next_sequence: 8, has_more: false })));
    expect(await readNativeTracePage(pin, { afterSequence: 8 })).toMatchObject({ next_sequence: 8, events: [] });
  });

  it.each([
    { workspace_id: ROOT }, { worker_item_id: ROOT }, { execution_id: ROOT }, { run_id: ROOT },
    { profile_ref: { ...profileRef, revision: 2 } },
    { events: [event(3), event(3)] }, { events: [event(8), event(3)] },
    { events: [{ ...event(3), run_id: ROOT }] }, { events: [{ ...event(3), sequence: 0 }] },
    { events: [{ ...event(3), operation_id: undefined }] },
    { events: [{ ...event(3), data: [] }] }, { next_sequence: 9 },
    { events: [], next_sequence: 0, has_more: true },
  ])("rejects foreign, malformed or nonadvancing page %j", async (change) => {
    server.use(http.get("*/native-trace", () => HttpResponse.json({ ...page(), ...change })));
    await expect(readNativeTracePage(pin)).rejects.toBeInstanceOf(NativeTraceUnavailableError);
  });

  it.each([{ afterSequence: -1 }, { limit: 0 }, { limit: 501 }])("rejects invalid cursor/limit before reads %j", async (options) => {
    let requests = 0;
    server.use(http.all("*", () => { requests += 1; return HttpResponse.json(page()); }));
    await expect(readNativeTracePage(pin, options)).rejects.toBeInstanceOf(NativeTraceUnavailableError);
    expect(requests).toBe(0);
  });

  it.each([
    [503, "native_trace_unsupported", "unsupported"], [503, "service_unavailable", "unavailable"],
    [503, "other_code", "unavailable"], [403, "permission_denied", "denied"],
    [404, "native_trace_unsupported", "unavailable"],
  ])("classifies only exact safe HTTP error %i/%s", async (status, code, kind) => {
    server.use(http.get("*/native-trace", () => HttpResponse.json({
      error: { code, message: "private-native-handle-secret", details: {} },
    }, { status })));
    await expect(readNativeTracePage(pin)).rejects.toMatchObject({ kind, message: "Trace data is unavailable." });
  });
});
