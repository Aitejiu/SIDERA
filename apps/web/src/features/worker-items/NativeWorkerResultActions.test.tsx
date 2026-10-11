import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CommandRequest, CommandResponse, NativeWorkerContextView } from "../assignments/types";
import type { LocalSession } from "../local-session/api";
import type { WorkerWorkspace } from "./types";
import { workerResultInputsFixture } from "@/test/resultInputFixtures";
import { server } from "@/test/server";
import { resultView } from "../results/testFixtures";
import { nativeWorkerResultActionDraftKey } from "../result-inputs/nativeWorkerResultActions";
import { WorkerItemPage } from "./WorkerItemPage";

const WS = "20000000-0000-0000-0000-000000000001";
const WI = "40000000-0000-0000-0000-000000000001";
const B = "40000000-0000-0000-0000-000000000002";
const ROOT = "30000000-0000-0000-0000-000000000001";
const PRINCIPAL = "10000000-0000-0000-0000-000000000001";
const HOLD = "b0000000-0000-0000-0000-000000000001";
const PROPOSAL = "b0000000-0000-0000-0000-000000000002";
const SELECTION = "b0000000-0000-0000-0000-000000000003";
const ASSEMBLY = "b0000000-0000-0000-0000-000000000004";
const SESSION: LocalSession = {
  session_id: "d0000000-0000-0000-0000-000000000001", workspace_id: WS, principal_id: PRINCIPAL, expires_at: "2099-01-01T00:00:00Z",
};

function workspaceFixture(workerItemId = WI): WorkerWorkspace {
  return {
    workspace_id: WS,
    worker_item: {
      id: workerItemId, root_task_id: ROOT, stable_key: "worker", title: workerItemId === WI ? "Result worker A" : "Result worker B",
      responsibility: "Review results", status: "active", version: 9, context_version: 4,
      acknowledged_context_version: 4, updated_at: "2026-10-07T00:00:00Z",
    },
    current_placement: {
      worker_item_key: "worker", subtask_key: "summary", subtask_task_id: ROOT, role: "implementer",
      capability_requirements: [], plan_generation: 2, plan_snapshot_id: ASSEMBLY,
    },
    current_runtime: null, current_step: null, next_action: null, latest_checkpoint: null,
    placement_history: [], execution_history: [], artifacts: [], evidence: [],
    usage: { input_tokens: 0, output_tokens: 0, cached_tokens: 0, cost_microunits: 0 },
    can_intervene: false, read_only_reason: "no_active_session", intervention_context: null,
    trace_descriptor: null, last_change_cursor: 1, as_of: "2026-10-07T00:00:00Z",
    collaboration: {
      enabled: true, state: "waiting_release", waiting_summary: null, upstream_links: [], governance_history: [],
      governance_mode: "automatic", release_state: "unreleased", attention_state: "none", available_actions: ["pause"],
      active_hold_id: null, hold_resource_version: null, active_proposal: null,
    },
  };
}

function pendingProposal(workspace: WorkerWorkspace, selection = true) {
  workspace.collaboration = {
    ...workspace.collaboration!, governance_mode: "paused", release_state: "proposed", attention_state: "decision_required",
    active_hold_id: HOLD, hold_resource_version: 3, available_actions: ["approve", "reject", "resume_automatic"],
    active_proposal: {
      proposal_id: PROPOSAL, resource_version: 8, status: "pending", summary: "Fixed review", reason_code: "ready",
      target_request_version: 17, created_at: "2026-10-07T00:00:00Z",
      input_selection_id: selection ? SELECTION : null, input_selection_hash: selection ? "a".repeat(64) : null,
    },
  };
}

function inputsFixture(workerItemId = WI) {
  const view = workerResultInputsFixture();
  view.workspace_id = WS; view.root_task_id = ROOT; view.worker_item_id = workerItemId; view.plan_generation = 2;
  view.request = null; view.execution = null; view.available_actions = [];
  view.delivery.consumer_id = workerItemId;
  view.delivery.assembly_request_id = ASSEMBLY; view.delivery.assembly_version = 4;
  view.delivery.assembly_status = "blocked"; view.delivery.assembly_request_fingerprint = "f".repeat(64);
  return view;
}

function reply(original: CommandRequest, stale = false): CommandResponse {
  const version = (original.expected_version ?? 0) + 1;
  const common = { command_id: original.command_id, aggregate_id: original.target_id ?? "", aggregate_version: version, event_ids: [] };
  switch (original.command_type) {
    case "PauseCollaborationRelease": return { ...common, aggregate_id: HOLD, aggregate_version: 1, data: { hold_id: HOLD, hold_version: 1, governance_mode: "paused" } };
    case "ApproveCollaborationReleaseProposal": return { ...common, data: { proposal_id: PROPOSAL, proposal_status: stale ? "stale" : "approved", result: stale ? "stale" : "applied" } };
    case "RejectCollaborationReleaseProposal": return { ...common, data: { proposal_id: PROPOSAL, proposal_status: "rejected" } };
    case "ResumeAutomaticCollaboration": return { ...common, data: { hold_id: HOLD, hold_version: version, governance_mode: "automatic" } };
    default: return { ...common, data: { context_assembly_request_id: ASSEMBLY, status: "queued" } };
  }
}

function installHandlers() {
  const world = {
    workspaces: new Map([[WI, workspaceFixture()], [B, workspaceFixture(B)]]), session: SESSION,
    host: "sidera-product", lost: false, stale: false, inputsFail: false,
    holdWorkspace: null as Promise<void> | null, holdPost: null as Promise<void> | null,
  };
  const posted: CommandRequest[] = []; const csrf: Array<string | null> = []; const gets: string[] = [];
  const workspaceReads = new Map<string, number>(); const nonCommandWrites: string[] = [];
  server.use(
    http.get(/\/health(?:z)?$/, () => HttpResponse.json({ status: "ok", service: world.host })),
    http.get("/product/v1/session", () => HttpResponse.json(world.session)),
    http.get("/api/v1/workspaces/:workspaceId/assignments/worker_item/:workerItemId", () => HttpResponse.json(null)),
    http.get("/api/v1/workspaces/:workspaceId/worker-items/:workerItemId/native-context", ({ params }) => HttpResponse.json({
      workspace_id: WS, worker_item_id: String(params.workerItemId), execution_mode: "native_profile", worker_item_version: 9,
      binding: null, execution: null, execution_version: null, context: null, run: null,
      input_enabled: false, read_only_reason: "no_active_session", as_of: "2026-10-07T00:00:00Z",
    } satisfies NativeWorkerContextView)),
    http.get("/api/v1/workspaces/:workspaceId/worker-items/:workerItemId/workspace", async ({ params }) => {
      const id = String(params.workerItemId); workspaceReads.set(id, (workspaceReads.get(id) ?? 0) + 1);
      if (id === WI) await world.holdWorkspace;
      return HttpResponse.json(world.workspaces.get(id));
    }),
    http.get("/api/v1/workspaces/:workspaceId/worker-items/:workerItemId/activity", () => HttpResponse.json({ items: [], last_change_cursor: 1, next_cursor: null })),
    http.get("/api/v1/workspaces/:workspaceId/worker-items/:workerItemId/result-inputs", ({ params }) => world.inputsFail
      ? HttpResponse.json({ error: { message: "private source detail" } }, { status: 503 }) : HttpResponse.json(inputsFixture(String(params.workerItemId)))),
    http.get("/api/v1/tasks/:rootTaskId/graph", () => HttpResponse.json({ overview: { title: "Result task" }, nodes: [{ subtask_key: "summary", title: "Summary" }] })),
    http.get("/api/v1/workspaces/:workspaceId/tasks/:rootTaskId/results", () => HttpResponse.json(resultView({ workspace_id: WS, root_task_id: ROOT, task_title: "Result task" }))),
    http.get("/api/v1/workspaces/:workspaceId/commands/:commandId", ({ params }) => {
      gets.push(String(params.commandId)); const original = posted.find(command => command.command_id === params.commandId);
      return original ? HttpResponse.json({
        command_id: original.command_id, command_type: original.command_type, target_type: original.target_type,
        target_id: original.target_id, expected_version: original.expected_version, payload: original.payload,
        status: "applied", result: reply(original, world.stale),
      }) : HttpResponse.json({}, { status: 404 });
    }),
    http.post("/api/v1/commands", async ({ request }) => {
      const original = await request.json() as CommandRequest; posted.push(original); csrf.push(request.headers.get("X-Sidera-CSRF"));
      await world.holdPost;
      return world.lost ? HttpResponse.error() : HttpResponse.json(reply(original, world.stale));
    }),
    http.post(/.*/, ({ request }) => { nonCommandWrites.push(new URL(request.url).pathname); return HttpResponse.error(); }),
  );
  return { world, posted, csrf, gets, workspaceReads, nonCommandWrites };
}

function RouteControls() {
  const navigate = useNavigate();
  return <><button onClick={() => navigate(`/workspaces/${WS}/worker-items/${WI}`)}>Worker A</button><button onClick={() => navigate(`/workspaces/${WS}/worker-items/${B}`)}>Worker B</button></>;
}

function renderPage(id = WI) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return {
    ...render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[`/workspaces/${WS}/worker-items/${id}`]}>
      <RouteControls /><Routes><Route path="/workspaces/:workspaceId/worker-items/:workerItemId" element={<WorkerItemPage />} /></Routes>
    </MemoryRouter></QueryClientProvider>), client,
  };
}

async function governance(name: string, submitLabel: string, reasonLabel?: string) {
  const user = userEvent.setup();
  await waitFor(() => expect(screen.getByRole("button", { name })).toBeEnabled());
  await user.click(screen.getByRole("button", { name }));
  const dialog = screen.getByRole("dialog");
  if (reasonLabel) await user.type(within(dialog).getByRole("textbox", { name: reasonLabel }), "Review first");
  await user.click(within(dialog).getByRole("button", { name: submitLabel }));
}

describe("native WorkerItem result actions Page", () => {
  beforeEach(() => { server.resetHandlers(); localStorage.clear(); document.cookie = "sidera_csrf=phase4c-page; Path=/"; });
  afterEach(() => { vi.restoreAllMocks(); document.cookie = "sidera_csrf=; Max-Age=0; Path=/"; });

  it("pauses an unadmitted closed-input WorkerItem with Wi9/generation2 and maps a new hold1", async () => {
    const handlers = installHandlers(); renderPage();
    await governance("暂停自动放行", "确认暂停自动放行", "暂停原因");
    await screen.findByText("已暂停自动放行");
    expect(handlers.posted).toHaveLength(1); expect(handlers.csrf).toEqual(["phase4c-page"]);
    expect(handlers.posted[0]).toMatchObject({
      actor: { type: "user", id: PRINCIPAL }, target_type: "worker_item", target_id: WI, expected_version: 9,
      payload: { transport: "web", reason: "Review first", target_plan_generation: 2 },
    });
    expect(screen.getByText("原操作已处理")).toBeVisible(); expect(handlers.nonCommandWrites).toEqual([]);
  });

  it("resumes the hold at its own version while the Agent input remains closed", async () => {
    const handlers = installHandlers(); pendingProposal(handlers.world.workspaces.get(WI)!); renderPage();
    await governance("恢复自动放行", "确认恢复自动放行"); await screen.findByText("已恢复自动放行");
    expect(handlers.posted[0]).toMatchObject({ command_type: "ResumeAutomaticCollaboration", target_type: "collaboration_governance_hold", target_id: HOLD, expected_version: 3 });
  });

  it("retries only the exact assembly request and acknowledges queued rather than assembled", async () => {
    const handlers = installHandlers(); const page = renderPage();
    await waitFor(() => expect(page.client.getQueryData(["native-worker-result-authority", WS, WI])).toMatchObject({ kind: "product" }));
    await userEvent.setup().click(await screen.findByRole("button", { name: "重试上下文组装" }));
    await screen.findByText("上下文组装重试已排队");
    expect(handlers.posted[0]).toMatchObject({
      command_type: "RetryContextAssembly", actor: { type: "user", id: PRINCIPAL }, target_type: "context_assembly", target_id: ASSEMBLY,
      expected_version: 4, payload: { request_fingerprint: "f".repeat(64) },
    });
    expect(handlers.csrf).toEqual(["phase4c-page"]);
  });

  it.each([true, false])("retains fixed approval Selection and truthful stale handling, selection=%s", async selection => {
    const handlers = installHandlers(); pendingProposal(handlers.world.workspaces.get(WI)!, selection); handlers.world.stale = true; renderPage();
    await governance("批准 Proposal", "批准这次放行"); await screen.findByText("Proposal 已失效");
    expect(handlers.posted[0]).toMatchObject({ target_id: PROPOSAL, expected_version: 8, payload: {
      transport: "web", reason: null, expected_input_selection_id: selection ? SELECTION : null,
      expected_input_selection_hash: selection ? "a".repeat(64) : null,
    } });
    expect(screen.queryByText(/任务已完成|已开始执行/)).not.toBeInTheDocument();
  });

  it("shares an unknown original across both UIs and recovers it after current proposal and inputs disappear", async () => {
    const handlers = installHandlers(); pendingProposal(handlers.world.workspaces.get(WI)!); handlers.world.lost = true;
    const page = renderPage(); await governance("批准 Proposal", "批准这次放行"); await screen.findByText("原操作结果未知");
    await userEvent.setup().click(screen.getByRole("button", { name: "重试上下文组装" }));
    expect(handlers.posted).toHaveLength(1); expect(screen.getByRole("button", { name: "恢复自动放行" })).toBeDisabled();
    handlers.world.workspaces.get(WI)!.collaboration = null; handlers.world.inputsFail = true; page.unmount();
    const identity = vi.spyOn(crypto, "randomUUID"); const reloaded = renderPage();
    await screen.findByText("原操作结果未知"); await screen.findByText("结果输入暂不可用");
    expect(screen.queryByRole("region", { name: "协同放行控制" })).not.toBeInTheDocument();
    vi.spyOn(reloaded.client, "invalidateQueries").mockRejectedValue(new Error("private refresh detail"));
    await userEvent.setup().click(screen.getByRole("button", { name: "查询原操作" })); await screen.findByText("Proposal 已批准");
    expect(handlers.gets).toEqual([handlers.posted[0]?.command_id]); expect(handlers.posted).toHaveLength(1); expect(identity).not.toHaveBeenCalled();
    expect(screen.queryByText(/private/)).not.toBeInTheDocument();
  });

  it("keeps a correlated POST handled through Page refresh failure and permits original GET", async () => {
    const handlers = installHandlers(); const page = renderPage();
    await screen.findByRole("heading", { name: "Result worker A" });
    vi.spyOn(page.client, "invalidateQueries").mockRejectedValue(new Error("private refresh detail"));
    await governance("暂停自动放行", "确认暂停自动放行", "暂停原因"); await screen.findByText("已暂停自动放行");
    await userEvent.setup().click(screen.getByRole("button", { name: "查询原操作" }));
    await waitFor(() => expect(handlers.gets).toHaveLength(1)); expect(handlers.posted).toHaveLength(1);
  });

  it.each(["unknown", "expired"])("keeps %s authority from entering the Page legacy writer", async mode => {
    const handlers = installHandlers(); if (mode === "unknown") handlers.world.host = "unknown";
    else handlers.world.session = { ...SESSION, expires_at: "2000-01-01T00:00:00Z" };
    const page = renderPage(); await screen.findByRole("heading", { name: "Result worker A" });
    await waitFor(() => expect(page.client.getQueryData(["native-worker-result-authority", WS, WI])).toMatchObject({ kind: "unavailable" }));
    expect(screen.getByRole("button", { name: "暂停自动放行" })).toBeDisabled();
    await userEvent.setup().click(screen.getByRole("button", { name: "重试上下文组装" }));
    expect(handlers.posted).toHaveLength(0); expect(handlers.nonCommandWrites).toEqual([]);
  });

  it("preserves genuine legacy submission when the host positively identifies itself", async () => {
    const handlers = installHandlers(); handlers.world.host = "sidera-main-agent-gateway"; renderPage();
    await governance("暂停自动放行", "确认暂停自动放行", "暂停原因");
    await waitFor(() => expect(handlers.posted).toHaveLength(1));
    expect(handlers.posted[0]?.actor).toEqual({ type: "local", id: "aitejiu" });
  });

  it("fences a held A preflight after route B mounts without identity or persistence", async () => {
    const handlers = installHandlers(); renderPage();
    await waitFor(() => expect(screen.getByRole("button", { name: "暂停自动放行" })).toBeEnabled());
    let release: () => void = () => undefined; handlers.world.holdWorkspace = new Promise<void>(resolve => { release = resolve; });
    const identity = vi.spyOn(crypto, "randomUUID"); const saves = vi.spyOn(Storage.prototype, "setItem");
    await governance("暂停自动放行", "确认暂停自动放行", "暂停原因");
    await waitFor(() => expect(handlers.workspaceReads.get(WI)).toBe(2));
    await userEvent.setup().click(screen.getByRole("button", { name: "Worker B" })); await screen.findByRole("heading", { name: "Result worker B" });
    await act(async () => { release(); });
    await waitFor(() => expect(screen.getByRole("button", { name: "暂停自动放行" })).toBeEnabled());
    expect(identity).not.toHaveBeenCalled(); expect(saves).not.toHaveBeenCalled(); expect(handlers.posted).toHaveLength(0);
  });

  it("keeps B's own unresolved draft and GET-recovers A after a late POST", async () => {
    const handlers = installHandlers(); const page = renderPage();
    await governance("暂停自动放行", "确认暂停自动放行", "暂停原因"); await screen.findByText("已暂停自动放行");
    const aKey = nativeWorkerResultActionDraftKey({ workspace_id: WS, root_task_id: ROOT, worker_item_id: WI, principal_id: PRINCIPAL });
    const savedA = JSON.parse(localStorage.getItem(aKey) ?? "null") as { original: CommandRequest; scope: { worker_item_id: string }; review: { action: { target_id: string } }; status: string; aggregate_id: string | null; aggregate_version: number | null; outcome: string | null };
    const bKey = nativeWorkerResultActionDraftKey({ workspace_id: WS, root_task_id: ROOT, worker_item_id: B, principal_id: PRINCIPAL });
    const bDraft = {
      ...savedA, scope: { ...savedA.scope, worker_item_id: B },
      original: {
        ...savedA.original, target_id: B, command_id: "c0000000-0000-0000-0000-000000000002",
        correlation_id: "d0000000-0000-0000-0000-000000000002", idempotency_key: "phase4c-b-original",
      },
      review: { ...savedA.review, action: { ...savedA.review.action, target_id: B } },
      status: "unknown", aggregate_id: null, aggregate_version: null, outcome: null,
    };
    localStorage.setItem(bKey, JSON.stringify(bDraft));
    let release: () => void = () => undefined; handlers.world.holdPost = new Promise<void>(resolve => { release = resolve; });
    await governance("暂停自动放行", "确认暂停自动放行", "暂停原因"); await waitFor(() => expect(handlers.posted).toHaveLength(2));
    await userEvent.setup().click(screen.getByRole("button", { name: "Worker B" })); await screen.findByRole("heading", { name: "Result worker B" });
    await screen.findByText("原操作结果未知"); const savedB = localStorage.getItem(bKey);
    await act(async () => { release(); }); expect(localStorage.getItem(bKey)).toBe(savedB);
    page.unmount(); renderPage(); await screen.findByText("原操作处理中");
    await userEvent.setup().click(screen.getByRole("button", { name: "查询原操作" })); await screen.findByText("已暂停自动放行");
    expect(handlers.gets).toEqual([handlers.posted[1]?.command_id]); expect(handlers.posted).toHaveLength(2);
  });
});
