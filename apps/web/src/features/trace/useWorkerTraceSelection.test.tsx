import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import type { PropsWithChildren } from "react";
import { beforeEach, expect, it } from "vitest";

import type { AgentExecutionView } from "@/features/assignments/types";
import { server } from "@/test/server";
import type { TraceSelectionAddress } from "./nativeTrace";
import type { Execution, ExecutionSession } from "./types";
import { useWorkerTraceSelection } from "./useWorkerTraceSelection";

const WS = "20000000-0000-0000-0000-000000000001";
const ROOT = "30000000-0000-0000-0000-000000000001";
const TASK = "40000000-0000-0000-0000-000000000001";
const WI = "50000000-0000-0000-0000-000000000001";
const EX = "60000000-0000-0000-0000-000000000001";
const EX_B = "60000000-0000-0000-0000-000000000002";
const SESSION = "70000000-0000-0000-0000-000000000001";
const RUN = "80000000-0000-0000-0000-000000000001";
const WORKER = "90000000-0000-0000-0000-000000000001";
const PROFILE = "10000000-0000-0000-0000-000000000001";
const NOW = "2026-10-07T00:00:00Z";
const worker = { id: WORKER, adapter_type: "agent-run", display_name: "Saved worker",
  status: "online", desired_state: "online", capacity: 1, current_load: 1 };
const execution: Execution = {
  id: EX, workspace_id: WS, task_id: TASK, worker_item_id: WI, lineage_id: EX,
  attempt: 1, max_attempts: 3, purpose: "Selected execution", status: "running",
  capability_requirements: [], pinned_worker_id: WORKER, assigned_worker_id: WORKER,
  pinned_worker_summary: worker, assigned_worker_summary: worker, binding_summary: null,
  active_lease_id: null, last_fencing_token: 1, not_before: NOW, failure_code: null,
  started_at: NOW, finished_at: null, version: 7, stop_requested: false,
  source_execution_id: null, source_session_id: null, created_at: NOW, updated_at: NOW,
  adapter_forbidden: [], adapter_preferences: [], blocked_since: null,
  requires_operator: false, scheduling_status: "running", waiting_details_summary: null, waiting_reason: null,
};
const saved: AgentExecutionView = {
  workspace_id: WS, execution_id: EX, root_task_id: ROOT, subtask_id: TASK, worker_item_id: WI,
  status: "running", updated_at: NOW, profile_ref: { workspace_id: WS, profile_id: PROFILE, revision: 1 },
  binding_version: 2, run_id: RUN, link_status: "running", event_cursor: 8,
};
const session: ExecutionSession = {
  id: SESSION, workspace_id: WS, execution_id: EX, worker_id: WORKER,
  adapter_session_id: `agent-run:${RUN}`, status: "active", input_mode: "queued",
  last_sequence: 8, last_activity_at: NOW, version: 5, adapter_module_id: null,
  adapter_config_version: null, capability_snapshot: null, created_at: NOW, updated_at: NOW,
  workspace_config_version: null, workspace_module_id: null, workspace_ref: null,
};
const address: TraceSelectionAddress = { rootTaskId: ROOT, executionId: EX, sessionId: null };
let localSession = { workspace_id: WS, principal_id: "human-A", expires_at: "2099-01-01T00:00:00Z" };
let executionReads = 0;
let savedReads = 0;
let sessionReads = 0;

function setup(initialAddress = address) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  const wrapper = ({ children }: PropsWithChildren) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  const view = renderHook(({ selected }) => useWorkerTraceSelection({ workspaceId: WS, address: selected }),
    { wrapper, initialProps: { selected: initialAddress } });
  return { ...view, client };
}

beforeEach(() => {
  localSession = { workspace_id: WS, principal_id: "human-A", expires_at: "2099-01-01T00:00:00Z" };
  executionReads = 0; savedReads = 0; sessionReads = 0;
  server.use(
    http.get("/product/v1/session", () => HttpResponse.json(localSession)),
    http.get("/api/v1/executions/:ex", ({ params }) => {
      executionReads += 1; return HttpResponse.json({ ...execution, id: params.ex });
    }),
    http.get(`/api/v1/workspaces/${WS}/agent-executions/:ex`, ({ params }) => {
      savedReads += 1; return HttpResponse.json({ ...saved, execution_id: params.ex });
    }),
    http.get("/api/v1/gateway/sessions", () => {
      sessionReads += 1; return HttpResponse.json({ items: [session], next_cursor: null });
    }),
  );
});

it("selects original native pins with unknown Session and no legacy read", async () => {
  const { result } = setup({ ...address, sessionId: "unknown" });
  await waitFor(() => expect(result.current.selection?.mode).toBe("native"));
  expect(result.current.selection).toMatchObject({ pin: { runId: RUN, principalId: "human-A", profileRef: { revision: 1 } } });
  expect(sessionReads).toBe(0);
});

it.each([{ expires_at: "2000-01-01T00:00:00Z" }, { workspace_id: ROOT }])(
  "denies invalid local identity %j before any Execution reads", async (change) => {
  localSession = { ...localSession, ...change };
  const { result } = setup();
  await waitFor(() => expect(result.current.error?.kind).toBe("denied"));
  expect(executionReads).toBe(0); expect(savedReads).toBe(0);
  expect(result.current.authorizationRequired).toBe(true);
});

it("clears selection on expiry after a successful local-session reread", async () => {
  const { result } = setup();
  await waitFor(() => expect(result.current.selection?.mode).toBe("native"));
  localSession = { ...localSession, expires_at: "2000-01-01T00:00:00Z" };
  await act(async () => { await result.current.retry(); });
  await waitFor(() => expect(result.current.selection).toBeNull());
  expect(result.current.error?.kind).toBe("denied");
});

it.each([403, 404, 503])("keeps failed saved read %i unavailable without mode fallback", async (status) => {
  server.use(http.get(`/api/v1/workspaces/${WS}/agent-executions/:ex`, () =>
    HttpResponse.json({ error: { code: "permission_denied", message: "private-secret", details: {} } }, { status })));
  const { result } = setup();
  await waitFor(() => expect(result.current.error?.kind).toBe(status === 403 ? "denied" : "unavailable"));
  expect(result.current.selection).toBeNull();
  expect(result.current.error?.message).not.toContain("private-secret");
});

it("rejects a foreign selected execution", async () => {
  server.use(http.get("/api/v1/executions/:ex", () => HttpResponse.json({ ...execution, workspace_id: ROOT })));
  const { result } = setup();
  await waitFor(() => expect(result.current.error?.kind).toBe("unavailable"));
  expect(result.current.selection).toBeNull();
});

it("does not switch a captured original Run on contradictory reread", async () => {
  const { result } = setup();
  await waitFor(() => expect(result.current.selection?.mode).toBe("native"));
  server.use(http.get(`/api/v1/workspaces/${WS}/agent-executions/:ex`, () =>
    HttpResponse.json({ ...saved, run_id: PROFILE, profile_ref: { ...saved.profile_ref!, revision: 2 } })));
  await act(async () => { await result.current.retry(); });
  await waitFor(() => expect(result.current.error?.kind).toBe("unavailable"));
  expect(result.current.selection).toBeNull();
});

it("does not let late A reads replace selected B or its principal", async () => {
  let releaseA: () => void = () => {};
  const pending = new Promise<void>((resolve) => { releaseA = resolve; });
  let startedA = false;
  server.use(http.get(`/api/v1/workspaces/${WS}/agent-executions/:ex`, async ({ params }) => {
    if (params.ex === EX) { startedA = true; await pending; }
    return HttpResponse.json({ ...saved, execution_id: params.ex, run_id: params.ex === EX ? RUN : PROFILE });
  }));
  const { result, rerender } = setup();
  await waitFor(() => expect(startedA).toBe(true));
  rerender({ selected: { ...address, executionId: EX_B } });
  await waitFor(() => expect(result.current.selection).toMatchObject({ pin: { executionId: EX_B, runId: PROFILE } }));
  await act(async () => { releaseA(); await pending; });
  expect(result.current.selection).toMatchObject({ pin: { executionId: EX_B, runId: PROFILE } });
  localSession = { ...localSession, principal_id: "human-B" };
  await act(async () => { await result.current.retry(); });
  await waitFor(() => expect(result.current.selection).toMatchObject({ pin: { principalId: "human-B", executionId: EX_B } }));
});

it("selects a complete genuine legacy route including null Wi/root history", async () => {
  const legacyWorker = { ...worker, adapter_type: "opencode" };
  server.use(
    http.get("/api/v1/executions/:ex", () => HttpResponse.json({ ...execution, worker_item_id: null,
      assigned_worker_summary: legacyWorker, pinned_worker_summary: legacyWorker })),
    http.get(`/api/v1/workspaces/${WS}/agent-executions/:ex`, () => HttpResponse.json({ ...saved,
      worker_item_id: null, root_task_id: null, profile_ref: null, binding_version: null,
      run_id: null, link_status: null, event_cursor: null })),
    http.get("/api/v1/gateway/sessions", () => HttpResponse.json({ items: [{ ...session, adapter_session_id: "legacy-session" }], next_cursor: null })),
  );
  const { result } = setup({ ...address, sessionId: SESSION });
  await waitFor(() => expect(result.current.selection?.mode).toBe("legacy"));
});

it("rejects native pins with a positively legacy Session", async () => {
  server.use(http.get("/api/v1/gateway/sessions", () => HttpResponse.json({
    items: [{ ...session, adapter_session_id: "legacy-session" }], next_cursor: null,
  })));
  const { result } = setup({ ...address, sessionId: SESSION });
  await waitFor(() => expect(result.current.error?.kind).toBe("unavailable"));
  expect(result.current.selection).toBeNull();
});
