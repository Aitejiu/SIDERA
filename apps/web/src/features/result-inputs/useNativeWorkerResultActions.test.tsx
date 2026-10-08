import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { LocalSession } from "../local-session/api";
import type { CommandRequest, CommandResponse, CommandStatusResponse } from "../assignments/types";
import type { GovernanceAction } from "../collaboration/types";
import type { WorkerWorkspace } from "../worker-items/types";
import { workerResultInputsFixture } from "@/test/resultInputFixtures";
import { server } from "@/test/server";
import { nativeWorkerResultActionDraftKey } from "./nativeWorkerResultActions";
import { resultInputActions, type WorkerResultInputsView } from "./api";
import { useNativeWorkerResultActions } from "./useNativeWorkerResultActions";

const WS = "20000000-0000-0000-0000-000000000001";
const WI = "40000000-0000-0000-0000-000000000001";
const ROOT = "30000000-0000-0000-0000-000000000001";
const PRINCIPAL = "10000000-0000-0000-0000-000000000001";
const HOLD = "b0000000-0000-0000-0000-000000000001";
const PROPOSAL = "b0000000-0000-0000-0000-000000000002";
const SELECTION = "b0000000-0000-0000-0000-000000000003";
const ASSEMBLY = "b0000000-0000-0000-0000-000000000004";
const SESSION: LocalSession = {
  session_id: "d0000000-0000-0000-0000-000000000001", workspace_id: WS,
  principal_id: PRINCIPAL, expires_at: "2099-01-01T00:00:00Z",
};

function workspaceFixture(workerItemId = WI): WorkerWorkspace {
  return {
    workspace_id: WS,
    worker_item: {
      id: workerItemId, root_task_id: ROOT, stable_key: "worker", title: "Worker", responsibility: "Review results",
      status: "active", version: 9, context_version: 4, acknowledged_context_version: 4, updated_at: "2026-10-07T00:00:00Z",
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
      governance_mode: "paused", release_state: "proposed", attention_state: "decision_required",
      available_actions: ["pause", "approve", "reject", "resume_automatic"], active_hold_id: HOLD, hold_resource_version: 3,
      active_proposal: {
        proposal_id: PROPOSAL, resource_version: 8, status: "pending", summary: "Review", reason_code: "ready",
        target_request_version: 17, created_at: "2026-10-07T00:00:00Z",
        input_selection_id: SELECTION, input_selection_hash: "a".repeat(64),
      },
    },
  };
}

function inputsFixture(): WorkerResultInputsView {
  const view = workerResultInputsFixture();
  view.workspace_id = WS; view.root_task_id = ROOT; view.worker_item_id = WI; view.plan_generation = 2;
  view.available_actions = [];
  view.delivery.consumer_id = WI;
  view.delivery.assembly_request_id = ASSEMBLY; view.delivery.assembly_version = 4;
  view.delivery.assembly_status = "blocked"; view.delivery.assembly_request_fingerprint = "f".repeat(64);
  return view;
}

function reply(original: CommandRequest): CommandResponse {
  const version = (original.expected_version ?? 0) + 1;
  const common = { command_id: original.command_id, aggregate_id: original.target_id ?? "", aggregate_version: version, event_ids: [] };
  switch (original.command_type) {
    case "PauseCollaborationRelease": return { ...common, aggregate_id: HOLD, aggregate_version: 1, data: { hold_id: HOLD, hold_version: 1, governance_mode: "paused" } };
    case "ApproveCollaborationReleaseProposal": return { ...common, data: { proposal_id: PROPOSAL, proposal_status: "approved", result: "applied" } };
    case "RejectCollaborationReleaseProposal": return { ...common, data: { proposal_id: PROPOSAL, proposal_status: "rejected" } };
    case "ResumeAutomaticCollaboration": return { ...common, data: { hold_id: HOLD, hold_version: version, governance_mode: "automatic" } };
    default: return { ...common, data: { context_assembly_request_id: ASSEMBLY, status: "queued" } };
  }
}

function installHandlers() {
  const world = {
    workspace: workspaceFixture(), inputs: inputsFixture(), session: SESSION,
    host: "sidera-product", lost: false, inputsFail: false,
    holdWorkspace: null as Promise<void> | null, holdPost: null as Promise<void> | null,
    journal: null as CommandStatusResponse | null,
  };
  const posted: CommandRequest[] = [];
  const gets: string[] = [];
  const csrf: Array<string | null> = [];
  const persisted: Array<string | null> = [];
  let workspaceReads = 0;
  server.use(
    http.get(/\/health(?:z)?$/, () => HttpResponse.json({ status: "ok", service: world.host })),
    http.get("/product/v1/session", () => HttpResponse.json(world.session)),
    http.get("/api/v1/workspaces/:workspaceId/worker-items/:workerItemId/workspace", async () => {
      workspaceReads += 1;
      await world.holdWorkspace;
      return HttpResponse.json(world.workspace);
    }),
    http.get("/api/v1/workspaces/:workspaceId/worker-items/:workerItemId/result-inputs", () => world.inputsFail
      ? HttpResponse.json({ error: { message: "private source detail" } }, { status: 503 }) : HttpResponse.json(world.inputs)),
    http.post("/api/v1/commands", async ({ request }) => {
      const command = await request.json() as CommandRequest;
      posted.push(command); csrf.push(request.headers.get("X-Sidera-CSRF"));
      persisted.push(localStorage.getItem(nativeWorkerResultActionDraftKey({ workspace_id: WS, root_task_id: ROOT, worker_item_id: command.command_type === "PauseCollaborationRelease" ? command.target_id ?? WI : WI, principal_id: PRINCIPAL })));
      await world.holdPost;
      return world.lost ? HttpResponse.error() : HttpResponse.json(reply(command));
    }),
    http.get("/api/v1/workspaces/:workspaceId/commands/:commandId", ({ params }) => {
      gets.push(String(params.commandId));
      const original = posted[0];
      return HttpResponse.json(world.journal ?? (original ? {
        command_id: original.command_id, command_type: original.command_type, target_type: original.target_type,
        target_id: original.target_id, expected_version: original.expected_version, payload: original.payload,
        status: "applied", result: reply(original),
      } : {}));
    }),
  );
  return { world, posted, gets, csrf, persisted, workspaceReads: () => workspaceReads };
}

function mount(workspace = workspaceFixture(), inputs = inputsFixture(), refresh = async () => undefined) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  const hook = renderHook((props: { workspace: WorkerWorkspace; inputs: WorkerResultInputsView | undefined }) => useNativeWorkerResultActions({
    workspaceId: props.workspace.workspace_id, workerItemId: props.workspace.worker_item.id,
    workspace: props.workspace, resultInputs: props.inputs, onRefresh: refresh,
  }), { wrapper, initialProps: { workspace, inputs } });
  return { ...hook, client };
}

async function ready(hook: ReturnType<typeof mount>) {
  await waitFor(() => expect(hook.result.current.canSubmit).toBe(true));
}

describe("native WorkerItem result action owner", () => {
  beforeEach(() => { server.resetHandlers(); localStorage.clear(); document.cookie = "sidera_csrf=phase4c-csrf; Path=/"; });
  afterEach(() => { vi.restoreAllMocks(); document.cookie = "sidera_csrf=; Max-Age=0; Path=/"; });

  it("expires idle human authority and reauthorizes without identity or writes", async () => {
    const handlers = installHandlers(); const hook = mount(); await ready(hook);
    const identity = vi.spyOn(crypto, "randomUUID"); const storage = vi.spyOn(Storage.prototype, "setItem");
    const short = { ...SESSION, expires_at: new Date(Date.now() + 400).toISOString() };
    handlers.world.session = short;
    act(() => hook.result.current.onSessionAuthorized(short));
    await waitFor(() => expect(hook.result.current.authority).toEqual({ kind: "product", session: short }));
    await waitFor(() => expect(hook.result.current.authorized).toBe(true));
    await waitFor(() => expect(hook.result.current.authorized).toBe(false), { timeout: 1500 });
    expect(hook.result.current.canSubmit).toBe(false); expect(hook.result.current.state?.original).toBeNull();
    expect(identity).not.toHaveBeenCalled(); expect(storage).not.toHaveBeenCalled(); expect(handlers.posted).toHaveLength(0);
    handlers.world.session = SESSION;
    act(() => hook.result.current.onSessionAuthorized(SESSION)); await ready(hook);
    expect(hook.result.current.authorized).toBe(true);
    expect(identity).not.toHaveBeenCalled(); expect(storage).not.toHaveBeenCalled(); expect(handlers.posted).toHaveLength(0);
  });

  it("preserves a known original through idle expiry and same-principal reauthorization", async () => {
    const handlers = installHandlers(); const hook = mount(); await ready(hook);
    await act(async () => { await hook.result.current.onGovernanceAction("pause", "Review"); });
    const recorded = hook.result.current.state;
    const key = nativeWorkerResultActionDraftKey({ workspace_id: WS, root_task_id: ROOT, worker_item_id: WI, principal_id: PRINCIPAL });
    const stored = localStorage.getItem(key);
    const identity = vi.spyOn(crypto, "randomUUID"); const storage = vi.spyOn(Storage.prototype, "setItem");
    const short = { ...SESSION, expires_at: new Date(Date.now() + 400).toISOString() };
    handlers.world.session = short;
    act(() => hook.result.current.onSessionAuthorized(short));
    await waitFor(() => expect(hook.result.current.authority).toEqual({ kind: "product", session: short }));
    await waitFor(() => expect(hook.result.current.authorized).toBe(true));
    await waitFor(() => expect(hook.result.current.authorized).toBe(false), { timeout: 1500 });
    expect(hook.result.current.canSubmit).toBe(false); expect(hook.result.current.state).toEqual(recorded);
    handlers.world.session = SESSION;
    act(() => hook.result.current.onSessionAuthorized(SESSION)); await ready(hook);
    expect(hook.result.current.state).toEqual(recorded); expect(hook.result.current.authorized).toBe(true);
    const foreign = { ...SESSION, principal_id: "10000000-0000-0000-0000-000000000002" };
    handlers.world.session = foreign;
    act(() => hook.result.current.onSessionAuthorized(foreign));
    await waitFor(() => expect(hook.result.current.state?.scope.principal_id).toBe(foreign.principal_id));
    expect(hook.result.current.state?.original).toBeNull(); expect(localStorage.getItem(key)).toBe(stored);
    handlers.world.session = SESSION;
    act(() => hook.result.current.onSessionAuthorized(SESSION));
    await waitFor(() => expect(hook.result.current.state).toEqual(recorded));
    expect(identity).not.toHaveBeenCalled(); expect(storage).not.toHaveBeenCalled(); expect(handlers.posted).toHaveLength(1);
  });

  it.each(["pause", "approve", "reject", "resume_automatic", "retry_context_assembly"])("submits current human %s with exact action pins and save-before-POST", async name => {
    const handlers = installHandlers();
    const hook = mount();
    await ready(hook);
    await act(async () => {
      if (name === "retry_context_assembly") await hook.result.current.onAction(resultInputActions(handlers.world.inputs)[0]!, "  Reviewed  ");
      else await hook.result.current.onGovernanceAction(name as GovernanceAction, "  Reviewed  ");
    });
    expect(handlers.posted).toHaveLength(1);
    const original = handlers.posted[0]!;
    expect(original.actor).toEqual({ type: "user", id: PRINCIPAL });
    expect(handlers.csrf).toEqual(["phase4c-csrf"]);
    expect(JSON.parse(handlers.persisted[0] ?? "null")).toMatchObject({ status: "pending", original, review: { reason: "Reviewed" } });
    const pins: Record<string, { type: string; version: number; payload: Record<string, unknown> }> = {
      pause: { type: "worker_item", version: 9, payload: { transport: "web", reason: "Reviewed", target_plan_generation: 2 } },
      approve: { type: "collaboration_release_proposal", version: 8, payload: { transport: "web", reason: "Reviewed", expected_input_selection_id: SELECTION, expected_input_selection_hash: "a".repeat(64) } },
      reject: { type: "collaboration_release_proposal", version: 8, payload: { transport: "web", reason: "Reviewed" } },
      resume_automatic: { type: "collaboration_governance_hold", version: 3, payload: { transport: "web", reason: "Reviewed" } },
      retry_context_assembly: { type: "context_assembly", version: 4, payload: { request_fingerprint: "f".repeat(64) } },
    };
    expect(original).toMatchObject({ target_type: pins[name]?.type, expected_version: pins[name]?.version, payload: pins[name]?.payload });
    expect(hook.result.current.state?.status).toBe("applied");
  });

  it("shares a synchronous entry lock across governance and assembly callbacks", async () => {
    const handlers = installHandlers(); const hook = mount(); await ready(hook);
    await act(async () => {
      await Promise.all([hook.result.current.onGovernanceAction("pause", "Review"), hook.result.current.onAction(resultInputActions(handlers.world.inputs)[0]!)]);
    });
    expect(handlers.posted).toHaveLength(1);
    expect(handlers.workspaceReads()).toBe(1);
  });

  it.each(["proposal", "generation", "selection", "fingerprint", "scope", "source_failure"])("refuses changed or unavailable fresh %s without identity or POST", async change => {
    const handlers = installHandlers(); const hook = mount(); await ready(hook);
    const identity = vi.spyOn(crypto, "randomUUID");
    if (change === "proposal" && handlers.world.workspace.collaboration?.active_proposal) handlers.world.workspace.collaboration.active_proposal.resource_version = 9;
    if (change === "generation") handlers.world.workspace.current_placement.plan_generation = 3;
    if (change === "selection" && handlers.world.workspace.collaboration?.active_proposal) handlers.world.workspace.collaboration.active_proposal.input_selection_hash = "b".repeat(64);
    if (change === "scope") handlers.world.workspace.worker_item.root_task_id = "30000000-0000-0000-0000-000000000009";
    if (change === "fingerprint") handlers.world.inputs.delivery.assembly_request_fingerprint = "c".repeat(64);
    if (change === "source_failure") handlers.world.inputsFail = true;
    await act(async () => {
      if (["fingerprint", "source_failure"].includes(change)) await hook.result.current.onAction(resultInputActions(inputsFixture())[0]!);
      else await hook.result.current.onGovernanceAction("approve", "Review");
    });
    expect(handlers.posted).toHaveLength(0); expect(identity).not.toHaveBeenCalled();
    expect(hook.result.current.state?.original).toBeNull();
  });

  it.each(["expired", "foreign", "unavailable"])("refuses %s fresh human authority", async mode => {
    const handlers = installHandlers(); const hook = mount(); await ready(hook);
    handlers.world.session = { ...SESSION, ...(mode === "expired" ? { expires_at: "2000-01-01T00:00:00Z" } : { principal_id: "foreign" }) };
    if (mode === "unavailable") handlers.world.host = "unknown";
    const identity = vi.spyOn(crypto, "randomUUID");
    await act(async () => { await hook.result.current.onGovernanceAction("pause", "Review"); });
    expect(handlers.posted).toHaveLength(0); expect(identity).not.toHaveBeenCalled();
  });

  it("does not depend on a failed result-input read for fresh governance", async () => {
    const handlers = installHandlers(); handlers.world.inputsFail = true;
    const hook = mount(); await ready(hook);
    await act(async () => { await hook.result.current.onGovernanceAction("pause", "Review"); });
    expect(handlers.posted).toHaveLength(1); expect(hook.result.current.state?.outcome).toBe("paused");
  });

  it("blocks NEW writes on invalid hydration and retries that same slot explicitly", async () => {
    const handlers = installHandlers();
    const key = nativeWorkerResultActionDraftKey({ workspace_id: WS, root_task_id: ROOT, worker_item_id: WI, principal_id: PRINCIPAL });
    localStorage.setItem(key, "{");
    const hook = mount(); await waitFor(() => expect(hook.result.current.storageError).not.toBeNull());
    await act(async () => { await hook.result.current.onGovernanceAction("pause", "Review"); hook.result.current.retryStorage(); });
    expect(handlers.posted).toHaveLength(0); expect(localStorage.getItem(key)).toBe("{");
    expect(hook.result.current.canSubmit).toBe(false);
  });

  it("keeps an initial failed save editable with no unsent pending original", async () => {
    const handlers = installHandlers(); const hook = mount(); await ready(hook);
    const save = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("private storage detail"); });
    await act(async () => { await hook.result.current.onGovernanceAction("pause", "Review"); });
    expect(handlers.posted).toHaveLength(0); expect(hook.result.current.state?.original).toBeNull();
    save.mockRestore(); act(() => hook.result.current.retryStorage());
    await ready(hook); expect(hook.result.current.state?.status).toBe("idle");
  });

  it("retains the sent original after post-submit save failure for save retry and GET", async () => {
    const handlers = installHandlers(); const hook = mount(); await ready(hook);
    const realSave = Storage.prototype.setItem; let writes = 0;
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (this: Storage, key, value) {
      if (++writes > 1) throw new Error("private storage detail"); realSave.call(this, key, value);
    });
    await act(async () => { await hook.result.current.onGovernanceAction("pause", "Review"); });
    const original = hook.result.current.state?.original;
    expect(original?.command_id).toBe(handlers.posted[0]?.command_id);
    expect(hook.result.current.state?.outcome).toBe("paused"); expect(hook.result.current.storageError).not.toBeNull();
    vi.restoreAllMocks(); act(() => hook.result.current.retryStorage());
    const identity = vi.spyOn(crypto, "randomUUID");
    await act(async () => { await hook.result.current.lookupOriginal(); });
    expect(handlers.gets).toEqual([original?.command_id]); expect(handlers.posted).toHaveLength(1); expect(identity).not.toHaveBeenCalled();
  });

  it("recovers only the lost original after reload and keeps handling through refresh failure", async () => {
    const handlers = installHandlers(); handlers.world.lost = true;
    const hook = mount(); await ready(hook);
    await act(async () => { await hook.result.current.onGovernanceAction("approve", "Review"); });
    expect(hook.result.current.state?.status).toBe("unknown");
    await act(async () => { await hook.result.current.onAction(resultInputActions(handlers.world.inputs)[0]!); });
    expect(handlers.posted).toHaveLength(1); hook.unmount();
    handlers.world.workspace.collaboration = null; handlers.world.inputsFail = true;
    const identity = vi.spyOn(crypto, "randomUUID");
    const reloaded = mount(workspaceFixture(), inputsFixture(), async () => { throw new Error("private refresh detail"); });
    await waitFor(() => expect(reloaded.result.current.state?.status).toBe("unknown"));
    await act(async () => { await reloaded.result.current.lookupOriginal(); });
    expect(reloaded.result.current.state?.status).toBe("applied"); expect(reloaded.result.current.state?.outcome).toBe("approved");
    expect(handlers.gets).toEqual([handlers.posted[0]?.command_id]); expect(identity).not.toHaveBeenCalled(); expect(handlers.posted).toHaveLength(1);
    expect(reloaded.result.current.error).not.toContain("private");
  });

  it("keeps a correlated POST handled when refresh fails and preserves known aggregate against conflict", async () => {
    const handlers = installHandlers(); const hook = mount(workspaceFixture(), inputsFixture(), async () => { throw new Error("private refresh detail"); });
    await ready(hook); await act(async () => { await hook.result.current.onGovernanceAction("pause", "Review"); });
    expect(hook.result.current.state?.outcome).toBe("paused");
    const original = handlers.posted[0]!;
    handlers.world.journal = { command_id: original.command_id, status: "applied", result: { ...reply(original), aggregate_id: PROPOSAL, data: { hold_id: PROPOSAL, hold_version: 1, governance_mode: "paused" } } };
    await act(async () => { await hook.result.current.lookupOriginal(); });
    expect(hook.result.current.state?.aggregate_id).toBe(HOLD); expect(hook.result.current.state?.review?.reason).toBe("Review");
  });

  it("invalidates a held preflight on unmount before any identity, save, or POST", async () => {
    const handlers = installHandlers(); let release: () => void = () => undefined;
    handlers.world.holdWorkspace = new Promise<void>(resolve => { release = resolve; });
    const hook = mount(); await ready(hook); const identity = vi.spyOn(crypto, "randomUUID"); const storage = vi.spyOn(Storage.prototype, "setItem");
    let pending: Promise<unknown> = Promise.resolve();
    act(() => { pending = hook.result.current.onGovernanceAction("pause", "Review"); });
    await waitFor(() => expect(handlers.workspaceReads()).toBe(1)); hook.unmount();
    const b = mount(workspaceFixture("40000000-0000-0000-0000-000000000002")); await ready(b);
    await act(async () => { release(); await pending; });
    expect(identity).not.toHaveBeenCalled(); expect(storage).not.toHaveBeenCalled(); expect(handlers.posted).toHaveLength(0);
  });

  it("preserves A's saved original while a held POST resolves after B mounts", async () => {
    const handlers = installHandlers(); let release: () => void = () => undefined;
    handlers.world.holdPost = new Promise<void>(resolve => { release = resolve; });
    const a = mount(); await ready(a); let pending: Promise<unknown> = Promise.resolve();
    act(() => { pending = a.result.current.onGovernanceAction("pause", "Review"); });
    await waitFor(() => expect(handlers.posted).toHaveLength(1)); a.unmount();
    const b = mount(workspaceFixture("40000000-0000-0000-0000-000000000002")); await ready(b);
    const key = nativeWorkerResultActionDraftKey({ workspace_id: WS, root_task_id: ROOT, worker_item_id: WI, principal_id: PRINCIPAL });
    const saved = localStorage.getItem(key);
    await act(async () => { release(); await pending; });
    expect(localStorage.getItem(key)).toBe(saved); expect(b.result.current.state?.original).toBeNull();
    expect(handlers.posted).toHaveLength(1);
  });
});
