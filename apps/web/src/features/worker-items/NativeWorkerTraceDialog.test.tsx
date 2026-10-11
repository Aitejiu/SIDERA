import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { useState } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, it } from "vitest";

import type { AgentExecutionView } from "@/features/assignments/types";
import type { LocalSession } from "@/features/local-session/api";
import { server } from "@/test/server";
import type { Execution, ExecutionSession } from "../trace/types";
import { WorkerTraceDialog } from "./WorkerTraceDialog";

const WS = "20000000-0000-0000-0000-000000000001";
const ROOT = "30000000-0000-0000-0000-000000000001";
const TASK = "40000000-0000-0000-0000-000000000001";
const WI = "50000000-0000-0000-0000-000000000001";
const OLD = "60000000-0000-0000-0000-000000000001";
const CURRENT = "60000000-0000-0000-0000-000000000002";
const SESSION = "70000000-0000-0000-0000-000000000001";
const RUN = "80000000-0000-0000-0000-000000000001";
const WORKER = "90000000-0000-0000-0000-000000000001";
const PROFILE = "10000000-0000-0000-0000-000000000001";
const NOW = "2026-10-07T00:00:00Z";
const worker = { id: WORKER, adapter_type: "agent-run", display_name: "Worker",
  status: "online", desired_state: "online", capacity: 1, current_load: 1 };
function execution(id: string): Execution {
  return { id, workspace_id: WS, task_id: TASK, worker_item_id: WI, lineage_id: id,
    attempt: 1, max_attempts: 3, purpose: "Trace", status: "completed", capability_requirements: [],
    pinned_worker_id: null, assigned_worker_id: WORKER, pinned_worker_summary: null,
    assigned_worker_summary: worker, binding_summary: null, active_lease_id: null,
    last_fencing_token: 1, not_before: NOW, failure_code: null, started_at: NOW, finished_at: NOW,
    version: 7, stop_requested: false, source_execution_id: null, source_session_id: null, created_at: NOW,
    updated_at: NOW, adapter_forbidden: [], adapter_preferences: [], blocked_since: null,
    requires_operator: false, scheduling_status: "completed", waiting_details_summary: null, waiting_reason: null };
}
function saved(id: string): AgentExecutionView {
  return { workspace_id: WS, execution_id: id, root_task_id: ROOT, subtask_id: TASK, worker_item_id: WI,
    status: "completed", updated_at: NOW, profile_ref: { workspace_id: WS, profile_id: PROFILE, revision: id === OLD ? 1 : 2 },
    binding_version: 2, run_id: id === OLD ? RUN : PROFILE, link_status: "accepted", event_cursor: 8 };
}
function page(id: string, after = "0") {
  const original = saved(id); const sequences = after === "8" ? [12] : [3, 8];
  return { workspace_id: WS, worker_item_id: WI, execution_id: id, run_id: original.run_id,
    profile_ref: original.profile_ref, next_sequence: sequences.at(-1), has_more: after === "0",
    events: sequences.map((sequence) => ({ run_id: original.run_id, sequence, kind: id === OLD ? "old.original" : "current.original",
      occurred_at: NOW, operation_id: null, response_id: null,
      data: { text: `Original ${id}`, principal: localSession.principal_id } })) };
}
let legacyReads = 0;
let writes = 0;
let nativeGets: Array<{ id: string; after: string }> = [];
let sessionReads = 0;
let localSession: LocalSession;
function Harness({ id = OLD, descriptor = false, sessionId = null }: { id?: string; descriptor?: boolean; sessionId?: string | null }) {
  const [open, setOpen] = useState(false);
  return <><button onClick={() => setOpen(true)}>打开 Trace</button>{open ?
    descriptor ? <WorkerTraceDialog workspaceId={WS} descriptor={{ root_task_id: ROOT, execution_id: id,
      session_id: sessionId ?? "unknown", route: "ignored-descriptor-route", raw_ref: null }} onClose={() => setOpen(false)} />
      : <WorkerTraceDialog workspaceId={WS} selection={{ rootTaskId: ROOT, executionId: id, sessionId }} onClose={() => setOpen(false)} />
    : null}</>;
}
function setup(id = OLD, descriptor = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  const body = (selected: string, sessionId: string | null = null) => <QueryClientProvider client={client}><MemoryRouter><Harness id={selected} descriptor={descriptor} sessionId={sessionId} /></MemoryRouter></QueryClientProvider>;
  const view = render(body(id)); return { ...view, client, change: (next: string, sessionId: string | null = null) => view.rerender(body(next, sessionId)) };
}
beforeEach(() => {
  legacyReads = 0; writes = 0; nativeGets = []; sessionReads = 0;
  localSession = { session_id: "human-session", workspace_id: WS,
    principal_id: "human", expires_at: "2099-01-01T00:00:00Z" };
  const nativeSession: ExecutionSession = { id: SESSION, workspace_id: WS, execution_id: CURRENT, worker_id: WORKER,
    adapter_session_id: `agent-run:${saved(CURRENT).run_id}`, status: "closed", input_mode: "queued", last_sequence: 8,
    last_activity_at: NOW, version: 5, adapter_module_id: null, adapter_config_version: null, capability_snapshot: null,
    created_at: NOW, updated_at: NOW, workspace_config_version: null, workspace_module_id: null, workspace_ref: null };
  server.use(
    http.get("/product/v1/session", () => HttpResponse.json(localSession)),
    http.get("/api/v1/executions/:ex", ({ params }) => HttpResponse.json(execution(String(params.ex)))),
    http.get(`/api/v1/workspaces/${WS}/agent-executions/:ex`, ({ params }) => HttpResponse.json(saved(String(params.ex)))),
    http.get("/api/v1/gateway/sessions", () => {
      sessionReads += 1; return HttpResponse.json({ items: [nativeSession], next_cursor: null });
    }),
    http.get("*/native-trace", ({ request }) => {
      const parts = new URL(request.url).pathname.split("/"); const id = parts.at(-2)!;
      const after = new URL(request.url).searchParams.get("after_sequence")!;
      nativeGets.push({ id, after }); return HttpResponse.json(page(id, after));
    }),
    http.get("/api/v1/tasks/:root/executions/:ex/sessions/:session/trace", () => { legacyReads += 1; return HttpResponse.json({}); }),
    http.post("*", () => { writes += 1; return HttpResponse.json({}); }),
  );
});

it.each([false, true])("opens native original Trace with nullable/unknown Session descriptor=%s", async (descriptor) => {
  const user = userEvent.setup(); setup(OLD, descriptor);
  await user.click(screen.getByRole("button", { name: "打开 Trace" }));
  await screen.findByRole("heading", { name: "old.original" });
  const dialog = screen.getByRole("dialog", { name: "Agent Trace" });
  expect(dialog).toHaveAttribute("open"); expect(within(dialog).getByText("Profile R1")).toBeVisible();
  await user.click(within(dialog).getByRole("button", { name: "后续事件" }));
  await waitFor(() => expect(within(dialog).getByRole("heading", { name: "old.original" }).parentElement).toHaveTextContent("#12"));
  expect(legacyReads).toBe(0); expect(writes).toBe(0);
});

it("keeps close/Escape/focus restoration in the existing dialog shell", async () => {
  const user = userEvent.setup(); setup(); const entry = screen.getByRole("button", { name: "打开 Trace" });
  await user.click(entry); await screen.findByRole("heading", { name: "old.original" });
  await user.click(screen.getByRole("button", { name: "关闭 Agent Trace" }));
  expect(screen.queryByRole("dialog", { name: "Agent Trace" })).not.toBeInTheDocument(); expect(entry).toHaveFocus();
  await user.click(entry); await screen.findByRole("heading", { name: "old.original" });
  fireEvent(screen.getByRole("dialog", { name: "Agent Trace" }), new Event("cancel", { cancelable: true }));
  expect(screen.queryByRole("dialog", { name: "Agent Trace" })).not.toBeInTheDocument(); expect(entry).toHaveFocus();
});

it("ignores late previous Run pages after changing the selected execution", async () => {
  let release: () => void = () => {};
  const pending = new Promise<void>((resolve) => { release = resolve; }); let started = false;
  server.use(http.get("*/native-trace", async ({ request }) => {
    const id = new URL(request.url).pathname.split("/").at(-2)!;
    if (id === CURRENT) { started = true; await pending; }
    return HttpResponse.json({ ...page(id), has_more: false });
  }));
  const user = userEvent.setup(); const view = setup(CURRENT);
  await user.click(screen.getByRole("button", { name: "打开 Trace" })); await waitFor(() => expect(started).toBe(true));
  view.change(OLD); await screen.findByRole("heading", { name: "old.original" });
  await act(async () => { release(); await pending; });
  expect(screen.queryByText("current.original")).not.toBeInTheDocument(); expect(screen.getByText("Profile R1")).toBeVisible();
});

it.each(["Session address", "principal", "execution"] as const)("remounts all reader state after a %s change", async (changedScope) => {
  const user = userEvent.setup(); const view = setup(CURRENT, true);
  await user.click(screen.getByRole("button", { name: "打开 Trace" }));
  await screen.findByRole("heading", { name: "current.original" });
  expect(sessionReads).toBe(0);
  const dialog = screen.getByRole("dialog", { name: "Agent Trace" });
  await user.click(within(dialog).getByRole("button", { name: /current\.original.*#3/ }));
  await user.click(within(dialog).getByText("公开数据", { selector: "summary" }));
  await user.type(within(dialog).getByRole("searchbox", { name: "搜索已加载记录" }), "Original");
  await user.selectOptions(within(dialog).getByRole("combobox", { name: "记录类型" }), "current.original");
  const previousReader = dialog.querySelector(".trace-reader");
  expect(previousReader).toHaveAttribute("data-detail-open", "true");
  expect(within(dialog).getByRole("heading", { name: "current.original" }).parentElement).toHaveTextContent("#3");
  expect(within(dialog).getByRole("button", { name: "跟随尾部" })).toBeVisible();
  expect(within(dialog).getByText("公开数据", { selector: "summary" }).parentElement).toHaveAttribute("open");

  if (changedScope === "principal") {
    localSession = { ...localSession, principal_id: "human-B" };
    await act(async () => { await view.client.invalidateQueries({ queryKey: ["worker-trace-session"] }); });
  } else view.change(changedScope === "execution" ? OLD : CURRENT, changedScope === "Session address" ? SESSION : null);
  await waitFor(() => expect(dialog.querySelector(".trace-reader")).not.toBe(previousReader));
  const heading = await within(dialog).findByRole("heading", { name: changedScope === "execution" ? "old.original" : "current.original" });
  expect(heading.parentElement).toHaveTextContent("#8");
  expect(dialog.querySelector(".trace-reader")).toHaveAttribute("data-detail-open", "false");
  expect(within(dialog).getByRole("searchbox", { name: "搜索已加载记录" })).toHaveValue("");
  expect(within(dialog).getByRole("combobox", { name: "记录类型" })).toHaveValue("");
  expect(within(dialog).queryByRole("button", { name: "跟随尾部" })).not.toBeInTheDocument();
  expect(within(dialog).getByText("公开数据", { selector: "summary" }).parentElement).not.toHaveAttribute("open");
  if (changedScope === "principal") {
    await user.click(within(dialog).getByText("公开数据", { selector: "summary" }));
    expect(within(dialog).getByRole("region", { name: "公开数据" })).toHaveTextContent('"principal": "human-B"');
    expect(nativeGets).toEqual([{ id: CURRENT, after: "0" }, { id: CURRENT, after: "0" }]);
  }
  if (changedScope === "Session address") expect(sessionReads).toBe(1);
  if (changedScope === "execution") {
    expect(within(dialog).getByText("Profile R1")).toBeVisible();
    expect(within(dialog).queryByText("Profile R2")).not.toBeInTheDocument();
    expect(nativeGets).toEqual([{ id: CURRENT, after: "0" }, { id: OLD, after: "0" }]);
  }
  expect(legacyReads).toBe(0); expect(writes).toBe(0);
});

it("shows denied saved reads before Trace with no writer or legacy fallback", async () => {
  server.use(http.get(`/api/v1/workspaces/${WS}/agent-executions/:ex`, () =>
    HttpResponse.json({ error: { code: "permission_denied", message: "private-secret", details: {} } }, { status: 403 })));
  const user = userEvent.setup(); setup(); await user.click(screen.getByRole("button", { name: "打开 Trace" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("无权读取 Agent Trace");
  expect(document.body).not.toHaveTextContent("private-secret"); expect(legacyReads).toBe(0); expect(writes).toBe(0);
});

it("keeps a genuine legacy unknown Session unavailable", async () => {
  const legacyWorker = { ...worker, adapter_type: "opencode" };
  server.use(
    http.get("/api/v1/executions/:ex", () => HttpResponse.json({ ...execution(OLD), assigned_worker_summary: legacyWorker })),
    http.get(`/api/v1/workspaces/${WS}/agent-executions/:ex`, () => HttpResponse.json({ ...saved(OLD),
      profile_ref: null, binding_version: null, run_id: null, link_status: null, event_cursor: null })),
  );
  const user = userEvent.setup(); setup(OLD, true); await user.click(screen.getByRole("button", { name: "打开 Trace" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Agent Trace 暂不可用"); expect(legacyReads).toBe(0);
});

it("uses the real legacy descriptor Session and descending endpoint", async () => {
  const legacyWorker = { ...worker, adapter_type: "opencode" };
  const session: ExecutionSession = { id: SESSION, workspace_id: WS, execution_id: OLD, worker_id: WORKER,
    adapter_session_id: "legacy-session", status: "closed", input_mode: "queued", last_sequence: 8,
    last_activity_at: NOW, version: 5, adapter_module_id: null, adapter_config_version: null, capability_snapshot: null,
    created_at: NOW, updated_at: NOW, workspace_config_version: null, workspace_module_id: null, workspace_ref: null };
  server.use(
    http.get("/api/v1/executions/:ex", () => HttpResponse.json({ ...execution(OLD), assigned_worker_summary: legacyWorker })),
    http.get(`/api/v1/workspaces/${WS}/agent-executions/:ex`, () => HttpResponse.json({ ...saved(OLD),
      profile_ref: null, binding_version: null, run_id: null, link_status: null, event_cursor: null })),
    http.get("/api/v1/gateway/sessions", () => HttpResponse.json({ items: [session], next_cursor: null })),
    http.get("/api/v1/tasks/:root/executions/:ex/sessions/:session/trace", ({ request }) => {
      legacyReads += 1;
      const before = new URL(request.url).searchParams.get("before_sequence");
      return HttpResponse.json({ schema_version: 1, execution_id: OLD, session_id: SESSION, worker_id: WORKER,
        status: "completed", current_action: null, plan_generation: null, checkpoint_sequence: null, freshness_at: NOW,
        last_change_cursor: 1, timing: null, context_branch: null, previous_before_sequence: before === null ? 8 : null,
        events: [{ schema_version: 1, workspace_id: WS, root_task_id: ROOT, work_item_key: null,
          plan_snapshot_id: null, execution_id: OLD, session_id: SESSION, sequence: before === null ? 8 : 3,
          occurred_at: NOW, category: "tool", status: "completed", title: before === null ? "Legacy latest" : "Legacy earlier",
          summary: "Legacy evidence", duration_ms: null, usage: null, artifact_refs: [], provider_payload_ref: null, provider_extensions: {} }] });
    }),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  render(<QueryClientProvider client={client}><MemoryRouter><WorkerTraceDialog workspaceId={WS}
    descriptor={{ root_task_id: ROOT, execution_id: OLD, session_id: SESSION, route: "ignored", raw_ref: null }} onClose={() => {}} />
  </MemoryRouter></QueryClientProvider>);
  const user = userEvent.setup(); await screen.findByRole("heading", { name: "Legacy latest" });
  await user.click(screen.getByRole("button", { name: "更早事件" })); await screen.findByRole("button", { name: /Legacy earlier/ });
  expect(legacyReads).toBe(2); expect(writes).toBe(0);
});
