import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import { beforeEach, expect, it } from "vitest";

import type { AgentExecutionView } from "@/features/assignments/types";
import type { LocalSession } from "@/features/local-session/api";
import { server } from "@/test/server";
import { WorkerTracePage } from "./WorkerTracePage";
import type { Execution, ExecutionSession } from "./types";

const WS = "20000000-0000-0000-0000-000000000001";
const ROOT = "30000000-0000-0000-0000-000000000001";
const TASK = "40000000-0000-0000-0000-000000000001";
const WI = "50000000-0000-0000-0000-000000000001";
const OLD = "60000000-0000-0000-0000-000000000001";
const CURRENT = "60000000-0000-0000-0000-000000000002";
const SESSION = "70000000-0000-0000-0000-000000000002";
const RUN_A = "80000000-0000-0000-0000-000000000001";
const RUN_B = "80000000-0000-0000-0000-000000000002";
const WORKER = "90000000-0000-0000-0000-000000000001";
const PROFILE = "10000000-0000-0000-0000-000000000001";
const NOW = "2026-10-07T00:00:00Z";
const route = (id: string, sessionId = "unknown") => `/workspaces/${WS}/tasks/${ROOT}/executions/${id}/sessions/${sessionId}/trace`;
function execution(id: string): Execution {
  return { id, workspace_id: WS, task_id: TASK, worker_item_id: WI, lineage_id: id,
    attempt: 1, max_attempts: 3, purpose: "Trace", status: id === OLD ? "completed" : "running",
    capability_requirements: [], pinned_worker_id: null, assigned_worker_id: WORKER,
    pinned_worker_summary: null, assigned_worker_summary: null, binding_summary: null,
    active_lease_id: null, last_fencing_token: 1, not_before: NOW, failure_code: null,
    started_at: NOW, finished_at: null, version: 7, stop_requested: false,
    source_execution_id: null, source_session_id: null, created_at: NOW, updated_at: NOW,
    adapter_forbidden: [], adapter_preferences: ["agent-run"], blocked_since: null,
    requires_operator: false, scheduling_status: "running", waiting_details_summary: null, waiting_reason: null };
}
function saved(id: string): AgentExecutionView {
  return { workspace_id: WS, execution_id: id, root_task_id: ROOT, subtask_id: TASK,
    worker_item_id: WI, status: execution(id).status, updated_at: NOW,
    profile_ref: { workspace_id: WS, profile_id: PROFILE, revision: id === OLD ? 1 : 2 },
    binding_version: id === OLD ? 2 : 3, run_id: id === OLD ? RUN_A : RUN_B,
    link_status: "accepted", event_cursor: 12 };
}
function page(id: string, sequences = [3, 8], more = true) {
  const original = saved(id);
  return { workspace_id: WS, worker_item_id: WI, execution_id: id, run_id: original.run_id,
    profile_ref: original.profile_ref, next_sequence: sequences.at(-1) ?? 0, has_more: more,
    events: sequences.map((sequence) => ({ run_id: original.run_id, sequence,
      kind: `${id === OLD ? "historical" : "current"}.${sequence}`, occurred_at: NOW,
      operation_id: null, response_id: null, data: { text: `${id === OLD ? "Old R1" : "Current R2"} ${sequence}`,
        principal: localSession.principal_id } })) };
}
let nativeGets: Array<{ id: string; after: string | null }> = [];
let sessionReads = 0;
let localSession: LocalSession;
let legacyGets = 0;
let writes = 0;
function Navigation() {
  const navigate = useNavigate();
  return <><button onClick={() => void navigate(route(OLD))}>历史执行</button><button onClick={() => void navigate(route(CURRENT))}>当前执行</button>
    <button onClick={() => void navigate(route(CURRENT, SESSION))}>原始 Session</button></>;
}
function setup(id = CURRENT) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  const view = render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[route(id)]}>
    <Navigation /><Routes><Route path="/workspaces/:workspaceId/tasks/:rootTaskId/executions/:executionId/sessions/:sessionId/trace"
      element={<WorkerTracePage />} /></Routes></MemoryRouter></QueryClientProvider>);
  return { ...view, client };
}
beforeEach(() => {
  nativeGets = []; sessionReads = 0; legacyGets = 0; writes = 0;
  localSession = { session_id: "human-session", workspace_id: WS,
    principal_id: "human", expires_at: "2099-01-01T00:00:00Z" };
  const nativeSession: ExecutionSession = { id: SESSION, workspace_id: WS, execution_id: CURRENT, worker_id: WORKER,
    adapter_session_id: `agent-run:${RUN_B}`, status: "closed", input_mode: "queued", last_sequence: 8,
    last_activity_at: NOW, version: 5, adapter_module_id: null, adapter_config_version: null, capability_snapshot: null,
    created_at: NOW, updated_at: NOW, workspace_config_version: null, workspace_module_id: null, workspace_ref: null };
  server.use(
    http.get("/product/v1/session", () => HttpResponse.json(localSession)),
    http.get("/api/v1/executions/:ex", ({ params }) => HttpResponse.json(execution(String(params.ex)))),
    http.get(`/api/v1/workspaces/${WS}/agent-executions/:ex`, ({ params }) => HttpResponse.json(saved(String(params.ex)))),
    http.get("/api/v1/gateway/sessions", () => {
      sessionReads += 1; return HttpResponse.json({ items: [nativeSession], next_cursor: null });
    }),
    http.get(`/api/v1/workspaces/${WS}/worker-items/${WI}/executions/:ex/native-trace`, ({ params, request }) => {
      const id = String(params.ex); const after = new URL(request.url).searchParams.get("after_sequence");
      nativeGets.push({ id, after }); return HttpResponse.json(after === "8" ? page(id, [12], false) : page(id));
    }),
    http.get("/api/v1/tasks/:root/executions/:ex/sessions/:session/trace", () => { legacyGets += 1; return HttpResponse.json({}); }),
    http.post("*", () => { writes += 1; return HttpResponse.json({}); }),
  );
});

it("opens current then historical original pins with unknown Session and forward pages", async () => {
  const user = userEvent.setup(); setup();
  await screen.findByRole("heading", { name: "current.8" });
  expect(screen.getByText("Profile R2")).toBeVisible();
  await user.click(screen.getByRole("button", { name: "历史执行" }));
  await screen.findByRole("heading", { name: "historical.8" });
  expect(screen.getByText("Profile R1")).toBeVisible(); expect(screen.queryByText("current.8")).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "后续事件" }));
  await screen.findByRole("heading", { name: "historical.12" });
  await user.click(screen.getByText("原始执行", { selector: "summary" }));
  expect(screen.getByText(RUN_A)).toBeVisible(); expect(screen.getByText(OLD)).toBeVisible();
  expect(nativeGets).toEqual([{ id: CURRENT, after: "0" }, { id: OLD, after: "0" }, { id: OLD, after: "8" }]);
  expect(legacyGets).toBe(0); expect(writes).toBe(0);
});

it("ignores late current Run events after switching to historical execution", async () => {
  let release: () => void = () => {};
  const pending = new Promise<void>((resolve) => { release = resolve; });
  let started = false;
  server.use(http.get(`/api/v1/workspaces/${WS}/worker-items/${WI}/executions/:ex/native-trace`, async ({ params }) => {
    if (params.ex === CURRENT) { started = true; await pending; }
    return HttpResponse.json(page(String(params.ex), [3], false));
  }));
  const user = userEvent.setup(); setup(); await waitFor(() => expect(started).toBe(true));
  await user.click(screen.getByRole("button", { name: "历史执行" }));
  await screen.findByRole("heading", { name: "historical.3" });
  await act(async () => { release(); await pending; });
  expect(screen.queryByText("current.3")).not.toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "historical.3" })).toBeVisible(); expect(legacyGets).toBe(0);
});

it.each(["Session address", "principal", "execution"] as const)("remounts all reader state after a %s change", async (changedScope) => {
  const user = userEvent.setup(); const view = setup();
  await screen.findByRole("heading", { name: "current.8" });
  expect(sessionReads).toBe(0);
  await user.click(screen.getByRole("button", { name: /current\.3.*#3/ }));
  await user.click(screen.getByText("公开数据", { selector: "summary" }));
  await user.type(screen.getByRole("searchbox", { name: "搜索已加载记录" }), "Current R2");
  await user.selectOptions(screen.getByRole("combobox", { name: "记录类型" }), "current.3");
  const previousReader = view.container.querySelector(".trace-reader");
  expect(previousReader).toHaveAttribute("data-detail-open", "true");
  expect(screen.getByRole("heading", { name: "current.3" })).toBeVisible();
  expect(screen.getByRole("button", { name: "跟随尾部" })).toBeVisible();
  expect(screen.getByText("公开数据", { selector: "summary" }).parentElement).toHaveAttribute("open");

  if (changedScope === "principal") {
    localSession = { ...localSession, principal_id: "human-B" };
    await act(async () => { await view.client.invalidateQueries({ queryKey: ["worker-trace-session"] }); });
  } else {
    await user.click(screen.getByRole("button", { name: changedScope === "execution" ? "历史执行" : "原始 Session" }));
  }
  await screen.findByRole("heading", { name: changedScope === "execution" ? "historical.8" : "current.8" });
  const reader = view.container.querySelector(".trace-reader");
  expect(reader).not.toBe(previousReader); expect(reader).toHaveAttribute("data-detail-open", "false");
  expect(screen.getByRole("searchbox", { name: "搜索已加载记录" })).toHaveValue("");
  expect(screen.getByRole("combobox", { name: "记录类型" })).toHaveValue("");
  expect(screen.queryByRole("button", { name: "跟随尾部" })).not.toBeInTheDocument();
  expect(screen.getByText("公开数据", { selector: "summary" }).parentElement).not.toHaveAttribute("open");
  expect(screen.queryByRole("heading", { name: "current.3" })).not.toBeInTheDocument();
  if (changedScope === "principal") {
    await user.click(screen.getByText("公开数据", { selector: "summary" }));
    expect(screen.getByRole("region", { name: "公开数据" })).toHaveTextContent('"principal": "human-B"');
    expect(nativeGets).toEqual([{ id: CURRENT, after: "0" }, { id: CURRENT, after: "0" }]);
  }
  if (changedScope === "Session address") expect(sessionReads).toBe(1);
  if (changedScope === "execution") {
    expect(screen.getByText("Profile R1")).toBeVisible();
    expect(screen.queryByText("Profile R2")).not.toBeInTheDocument();
    expect(nativeGets).toEqual([{ id: CURRENT, after: "0" }, { id: OLD, after: "0" }]);
  }
  expect(legacyGets).toBe(0); expect(writes).toBe(0);
});

it.each([[403, "permission_denied", "无权读取 Agent Trace"],
  [503, "native_trace_unsupported", "此执行不支持 Agent Trace"],
  [503, "service_unavailable", "Agent Trace 暂不可用"]])("renders %i/%s state without a legacy request or writer", async (status, code, text) => {
  server.use(http.get("*/native-trace", () => HttpResponse.json({
    error: { code, message: "private-native-handle-secret", details: {} },
  }, { status })));
  setup(); expect(await screen.findByRole("alert")).toHaveTextContent(text);
  expect(document.body).not.toHaveTextContent("private-native-handle-secret"); expect(legacyGets).toBe(0); expect(writes).toBe(0);
});

it("offers the existing session authorization entry before protected reads", async () => {
  server.use(http.get("/product/v1/session", () => HttpResponse.json({ error: { code: "local_session_required" } }, { status: 401 })));
  setup(); expect(await screen.findByRole("button", { name: "授权本地会话" })).toBeVisible();
  expect(nativeGets).toEqual([]); expect(legacyGets).toBe(0); expect(writes).toBe(0);
});
