import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import type { DefaultBodyType } from "msw";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createInvalidationQueue } from "@/app/WorkspaceStreamProvider";
import { PROFILE_REF } from "@/features/agents/testFixtures";
import type { LocalSession } from "@/features/local-session/api";
import type { ProjectionChange } from "@/shared/api/projectionStream";
import { workerResultInputsFixture } from "@/test/resultInputFixtures";
import { server } from "@/test/server";
import type {
  AgentBindingView,
  AgentExecutionView,
  CommandRequest,
  CommandResponse,
  CommandStatusResponse,
  NativeWorkerContextView,
} from "../assignments/types";
import { resultView } from "../results/testFixtures";
import { nativeInterventionDraftKey } from "../trace/nativeIntervention";
import type { NativeInterventionDraft } from "../trace/nativeIntervention";
import type { Execution, ExecutionInput, ExecutionSession } from "../trace/types";
import type { WorkerActivity, WorkerWorkspace } from "./types";
import { WorkerItemPage } from "./WorkerItemPage";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const WORKER_ITEM_ID = "40000000-0000-0000-0000-000000000001";
const ROOT_TASK_ID = "30000000-0000-0000-0000-000000000001";
const SUBTASK_ID = "30000000-0000-0000-0000-000000000002";
const EXECUTION_ID = "60000000-0000-0000-0000-000000000001";
const SESSION_ID = "70000000-0000-0000-0000-000000000001";
const WORKER_ID = "80000000-0000-0000-0000-000000000001";
const RUN_ID = "90000000-0000-0000-0000-000000000001";
const INPUT_ID = "50000000-0000-0000-0000-000000000001";
const WORKER_ITEM_B_ID = "40000000-0000-0000-0000-000000000002";
const EXECUTION_B_ID = "60000000-0000-0000-0000-000000000002";
const SESSION_B_ID = "70000000-0000-0000-0000-000000000002";
const WORKER_B_ID = "80000000-0000-0000-0000-000000000002";
const RUN_B_ID = "90000000-0000-0000-0000-000000000002";
const PRINCIPAL_ID = "10000000-0000-0000-0000-000000000001";
const PROFILE_HEAD_R2 = { ...PROFILE_REF, revision: 2 };
const PAGE_CSRF_TOKEN = "csrf-native-page";
const SESSION: LocalSession = {
  session_id: "d0000000-0000-0000-0000-000000000001",
  workspace_id: WORKSPACE_ID,
  principal_id: PRINCIPAL_ID,
  expires_at: "2099-01-01T00:00:00Z",
};
const binding: AgentBindingView = {
  workspace_id: WORKSPACE_ID,
  target_type: "worker_item",
  target_id: WORKER_ITEM_ID,
  assignee: { kind: "profile", id: PROFILE_REF.profile_id },
  profile_ref: PROFILE_REF,
  version: 2,
  runnable: true,
  not_runnable_reasons: [],
  created_at: "2026-10-07T00:00:00Z",
  changed_at: "2026-10-07T00:00:00Z",
};
const executionPin: AgentExecutionView = {
  workspace_id: WORKSPACE_ID,
  worker_item_id: WORKER_ITEM_ID,
  execution_id: EXECUTION_ID,
  subtask_id: SUBTASK_ID,
  root_task_id: ROOT_TASK_ID,
  status: "running",
  link_status: "accepted",
  run_id: RUN_ID,
  profile_ref: PROFILE_REF,
  binding_version: 2,
  event_cursor: 5,
  updated_at: "2026-10-07T00:00:00Z",
};
const context: NonNullable<NativeWorkerContextView["context"]> = {
  worker_item_id: WORKER_ITEM_ID,
  execution_mode: "native_profile",
  execution_id: EXECUTION_ID,
  session_id: SESSION_ID,
  session_version: 5,
  worker_id: WORKER_ID,
  binding_version: 2,
  run_id: RUN_ID,
  plan_generation: 2,
  context_version: 4,
};
const nativeContext: NativeWorkerContextView = {
  workspace_id: WORKSPACE_ID,
  worker_item_id: WORKER_ITEM_ID,
  execution_mode: "native_profile",
  worker_item_version: 9,
  binding,
  execution: executionPin,
  execution_version: 7,
  context,
  input_enabled: true,
  read_only_reason: null,
  run: {
    state: "running",
    input_open: true,
    profile_ref: PROFILE_REF,
    runtime_ref: { workspace_id: WORKSPACE_ID, runtime_id: "a0000000-0000-0000-0000-000000000001" },
    actual_runtime_version: "1",
    applied_fields: [],
    config_hash: "config-hash",
    observed_model: null,
    reason_code: null,
    selected_model: null,
  },
  as_of: "2026-10-07T00:00:00Z",
};
const workspace: WorkerWorkspace = {
  workspace_id: WORKSPACE_ID,
  worker_item: {
    id: WORKER_ITEM_ID,
    stable_key: "native-worker",
    title: "Native worker",
    responsibility: "Implement the callback contract.",
    root_task_id: ROOT_TASK_ID,
    status: "active",
    context_version: 4,
    acknowledged_context_version: 4,
    version: 9,
    updated_at: "2026-10-07T00:00:00Z",
  },
  current_placement: {
    worker_item_key: "native-worker",
    subtask_key: "callback-idempotency",
    subtask_task_id: SUBTASK_ID,
    role: "implementer",
    capability_requirements: [],
    plan_generation: 2,
    plan_snapshot_id: "b0000000-0000-0000-0000-000000000001",
  },
  placement_history: [],
  current_runtime: {
    adapter_type: "agent-run",
    attempt: 1,
    execution_id: EXECUTION_ID,
    execution_status: "running",
    fencing_token: 2,
    input_mode: "queued",
    lease_id: "c0000000-0000-0000-0000-000000000001",
    session_id: SESSION_ID,
    session_status: "active",
    session_version: 5,
    task_id: SUBTASK_ID,
    worker_display_name: "Native worker",
    worker_id: WORKER_ID,
  },
  execution_history: [],
  current_step: "Validate callback",
  next_action: "Continue validation",
  latest_checkpoint: null,
  usage: { input_tokens: 0, output_tokens: 0, cached_tokens: 0, cost_microunits: 0 },
  artifacts: [],
  evidence: [],
  can_intervene: true,
  read_only_reason: null,
  intervention_context: {
    worker_item_id: WORKER_ITEM_ID,
    plan_generation: 2,
    context_version: 4,
    execution_id: EXECUTION_ID,
    session_id: SESSION_ID,
    session_version: 5,
    worker_id: WORKER_ID,
  },
  trace_descriptor: {
    root_task_id: ROOT_TASK_ID,
    execution_id: EXECUTION_ID,
    session_id: SESSION_ID,
    route: "native",
    raw_ref: null,
  },
  last_change_cursor: 5,
  as_of: "2026-10-07T00:00:00Z",
  plan_sync: null,
  active_handoff: null,
};
const execution: Execution = {
  active_lease_id: "c0000000-0000-0000-0000-000000000001",
  adapter_forbidden: [],
  adapter_preferences: ["agent-run"],
  assigned_worker_id: WORKER_ID,
  assigned_worker_summary: null,
  attempt: 1,
  binding_summary: null,
  blocked_since: null,
  capability_requirements: [],
  created_at: "2026-10-07T00:00:00Z",
  failure_code: null,
  finished_at: null,
  id: EXECUTION_ID,
  last_fencing_token: 2,
  lineage_id: EXECUTION_ID,
  max_attempts: 3,
  not_before: "2026-10-07T00:00:00Z",
  pinned_worker_id: null,
  pinned_worker_summary: null,
  purpose: "worker_item",
  requires_operator: false,
  scheduling_status: "assigned",
  source_execution_id: null,
  source_session_id: null,
  started_at: "2026-10-07T00:00:00Z",
  status: "running",
  stop_requested: false,
  task_id: SUBTASK_ID,
  updated_at: "2026-10-07T00:00:00Z",
  version: 7,
  waiting_details_summary: null,
  waiting_reason: null,
  worker_item_id: WORKER_ITEM_ID,
  workspace_id: WORKSPACE_ID,
};
const session: ExecutionSession = {
  adapter_config_version: null,
  adapter_module_id: null,
  adapter_session_id: "native-session",
  capability_snapshot: null,
  created_at: "2026-10-07T00:00:00Z",
  execution_id: EXECUTION_ID,
  id: SESSION_ID,
  input_mode: "queued",
  last_activity_at: "2026-10-07T00:00:00Z",
  last_sequence: 3,
  status: "active",
  updated_at: "2026-10-07T00:00:00Z",
  version: 5,
  worker_id: WORKER_ID,
  workspace_config_version: null,
  workspace_id: WORKSPACE_ID,
  workspace_module_id: null,
  workspace_ref: null,
};

type NativePageWorld = {
  workspace: WorkerWorkspace;
  binding: AgentBindingView;
  context: NativeWorkerContextView;
  execution: Execution;
  session: ExecutionSession;
  activities: WorkerActivity[];
};

const worldA: NativePageWorld = { workspace, binding, context: nativeContext, execution, session, activities: [] };
const bindingB: AgentBindingView = { ...binding, target_id: WORKER_ITEM_B_ID };
const contextB: NonNullable<NativeWorkerContextView["context"]> = {
  ...context,
  worker_item_id: WORKER_ITEM_B_ID,
  execution_id: EXECUTION_B_ID,
  session_id: SESSION_B_ID,
  session_version: 6,
  worker_id: WORKER_B_ID,
  run_id: RUN_B_ID,
};
const nativeContextB: NativeWorkerContextView = {
  ...nativeContext,
  worker_item_id: WORKER_ITEM_B_ID,
  binding: bindingB,
  execution: {
    ...executionPin,
    worker_item_id: WORKER_ITEM_B_ID,
    execution_id: EXECUTION_B_ID,
    run_id: RUN_B_ID,
  },
  execution_version: 8,
  context: contextB,
};
const workspaceB: WorkerWorkspace = {
  ...workspace,
  worker_item: { ...workspace.worker_item, id: WORKER_ITEM_B_ID, stable_key: "native-worker-b", title: "Replacement worker" },
  current_runtime: {
    ...workspace.current_runtime!,
    execution_id: EXECUTION_B_ID,
    session_id: SESSION_B_ID,
    session_version: 6,
    worker_id: WORKER_B_ID,
  },
  intervention_context: {
    ...workspace.intervention_context!,
    worker_item_id: WORKER_ITEM_B_ID,
    execution_id: EXECUTION_B_ID,
    session_id: SESSION_B_ID,
    session_version: 6,
    worker_id: WORKER_B_ID,
  },
  trace_descriptor: {
    ...workspace.trace_descriptor!,
    execution_id: EXECUTION_B_ID,
    session_id: SESSION_B_ID,
  },
};
const executionB: Execution = {
  ...execution,
  id: EXECUTION_B_ID,
  lineage_id: EXECUTION_B_ID,
  worker_item_id: WORKER_ITEM_B_ID,
  version: 8,
};
const sessionB: ExecutionSession = {
  ...session,
  id: SESSION_B_ID,
  execution_id: EXECUTION_B_ID,
  worker_id: WORKER_B_ID,
  version: 6,
};
const worldB: NativePageWorld = {
  workspace: workspaceB,
  binding: bindingB,
  context: nativeContextB,
  execution: executionB,
  session: sessionB,
  activities: [],
};

function workerWorkspaceChange(cursor: number): ProjectionChange {
  return {
    workspace_id: WORKSPACE_ID,
    cursor,
    schema_version: 1,
    projection_type: "worker_item_workspace",
    resource_id: WORKER_ITEM_ID,
    change_type: "ExecutionStarted",
    resource_version: 9,
    occurred_at: "2026-10-07T00:00:01Z",
    correlation_id: "b0000000-0000-0000-0000-000000000002",
  };
}

function RouteControls() {
  const navigate = useNavigate();
  return (
    <nav aria-label="Test WorkerItem routes">
      <button type="button" onClick={() => navigate(`/workspaces/${WORKSPACE_ID}/worker-items/${WORKER_ITEM_ID}`)}>Open worker A</button>
      <button type="button" onClick={() => navigate(`/workspaces/${WORKSPACE_ID}/worker-items/${WORKER_ITEM_B_ID}`)}>Open worker B</button>
    </nav>
  );
}

function renderPage(
  initialWorkerItemId = WORKER_ITEM_ID,
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } }),
) {
  const view = render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/worker-items/${initialWorkerItemId}`]}>
        <RouteControls />
        <Routes>
          <Route path="/workspaces/:workspaceId/worker-items/:workerItemId" element={<WorkerItemPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...view, client };
}

function inputPayload(command: CommandRequest): {
  context: { execution_id: string; session_id: string };
  client_idempotency_key: string;
  text: string;
} {
  if (command.command_type !== "SendExecutionInput"
    || typeof command.payload !== "object"
    || command.payload === null
    || Array.isArray(command.payload)) {
    throw new Error("expected a SendExecutionInput payload");
  }
  const payload = command.payload as Record<string, unknown>;
  const rawContext = payload.context;
  if (typeof rawContext !== "object" || rawContext === null || Array.isArray(rawContext)
    || typeof payload.client_idempotency_key !== "string"
    || typeof payload.text !== "string") {
    throw new Error("SendExecutionInput fixture payload is incomplete");
  }
  const contextValue = rawContext as Record<string, unknown>;
  if (typeof contextValue.execution_id !== "string" || typeof contextValue.session_id !== "string") {
    throw new Error("SendExecutionInput fixture context is incomplete");
  }
  return {
    context: { execution_id: contextValue.execution_id, session_id: contextValue.session_id },
    client_idempotency_key: payload.client_idempotency_key,
    text: payload.text,
  };
}

function commandResponse(command: CommandRequest): CommandResponse {
  const aggregateId = command.command_type === "SendExecutionInput" ? INPUT_ID : command.target_id;
  if (typeof aggregateId !== "string") throw new Error("command target must be concrete");
  return {
    command_id: command.command_id,
    aggregate_id: aggregateId,
    aggregate_version: typeof command.expected_version === "number" ? command.expected_version + 1 : 1,
    event_ids: [],
  };
}

function inputRecord(command: CommandRequest, status: ExecutionInput["status"], version: number): ExecutionInput {
  const payload = inputPayload(command);
  return {
    id: INPUT_ID,
    workspace_id: WORKSPACE_ID,
    execution_id: payload.context.execution_id,
    session_id: payload.context.session_id,
    client_idempotency_key: payload.client_idempotency_key,
    status,
    operation_id: null,
    failure_code: null,
    version,
    text_length: payload.text.length,
    attachment_count: 0,
    created_at: "2026-10-07T00:00:00Z",
    updated_at: "2026-10-07T00:00:00Z",
  };
}

function supersededActivity(body: string): WorkerActivity {
  return {
    activity_id: "b0000000-0000-0000-0000-000000000001",
    source_type: "execution_input",
    source_id: INPUT_ID,
    execution_id: EXECUTION_B_ID,
    session_id: SESSION_B_ID,
    occurred_at: "2026-10-07T00:00:00Z",
    kind: "user_input",
    title: "用户介入已失效",
    summary: body,
    correlation_id: "b0000000-0000-0000-0000-000000000002",
    body,
    delivery_status: "superseded",
    artifact_ids: [],
    trace_descriptor: null,
  };
}

function storedDraft(workerItemId: string): NativeInterventionDraft {
  const raw = localStorage.getItem(nativeInterventionDraftKey({
    workspace_id: WORKSPACE_ID,
    principal_id: PRINCIPAL_ID,
    worker_item_id: workerItemId,
  }));
  if (raw === null) throw new Error(`missing persisted draft for ${workerItemId}`);
  return JSON.parse(raw) as NativeInterventionDraft;
}

function installHandlers(options: {
  beforeNativeContextResponse?: (workerItemId: string) => Promise<void>;
} = {}) {
  const worlds = new Map<string, NativePageWorld>([[WORKER_ITEM_ID, worldA], [WORKER_ITEM_B_ID, worldB]]);
  const posted: CommandRequest[] = [];
  const csrfHeaders: Array<string | null> = [];
  const commandGets: string[] = [];
  const inputRequests: Array<{ executionId: string | null; sessionId: string | null; cursor: string | null }> = [];
  const candidateReads: string[] = [];
  const workspaceReads = new Map<string, number>();
  const contextReads = new Map<string, number>();
  const completedContextReads = new Map<string, number>();
  const unavailableContexts = new Set<string>();
  const deniedContexts = new Set<string>();
  const commandStatuses = new Map<string, CommandStatusResponse>();
  let postMode: "normal" | "lost" | "deferred" = "normal";
  let deferredPostResolver: (() => void) | null = null;
  let inputPageResolver: (
    executionId: string | null,
    sessionId: string | null,
    cursor: string | null,
  ) => {
    items: ExecutionInput[];
    next_cursor: string | null;
  } = () => ({ items: [], next_cursor: null });
  const worldFor = (workerItemId: string | undefined) => workerItemId === undefined ? undefined : worlds.get(workerItemId);
  const nonCommandWrites: string[] = [];
  const recordNonCommandWrite = ({ request }: { request: Request }) => {
    nonCommandWrites.push(`${request.method} ${new URL(request.url).pathname}`);
    return HttpResponse.error();
  };

  server.use(
    http.get("/main-agent/v1/health", () => HttpResponse.json({
      status: "ok", service: "sidera-product",
    })),
    http.get("/product/v1/session", () => HttpResponse.json(SESSION)),
    http.get("/api/v1/workspaces/:workspaceId/assignments/worker_item/:workerItemId", ({ params }) => {
      const current = worldFor(String(params.workerItemId));
      return current ? HttpResponse.json(current.binding) : HttpResponse.json({}, { status: 404 });
    }),
    http.get("/api/v1/workspaces/:workspaceId/worker-items/:workerItemId/native-context", async ({ params }) => {
      const workerItemId = String(params.workerItemId);
      contextReads.set(workerItemId, (contextReads.get(workerItemId) ?? 0) + 1);
      await options.beforeNativeContextResponse?.(workerItemId);
      completedContextReads.set(workerItemId, (completedContextReads.get(workerItemId) ?? 0) + 1);
      const current = worldFor(workerItemId);
      if (current === undefined) return HttpResponse.json({}, { status: 404 });
      if (deniedContexts.has(workerItemId)) {
        return HttpResponse.json({ detail: "native context denied" }, { status: 403 });
      }
      if (unavailableContexts.has(workerItemId)) {
        return HttpResponse.json({ detail: "native context unavailable" }, { status: 503 });
      }
      return HttpResponse.json(current.context);
    }),
    http.get("/api/v1/workspaces/:workspaceId/worker-items/:workerItemId/workspace", ({ params }) => {
      const workerItemId = String(params.workerItemId);
      workspaceReads.set(workerItemId, (workspaceReads.get(workerItemId) ?? 0) + 1);
      const current = worldFor(workerItemId);
      return current ? HttpResponse.json(current.workspace) : HttpResponse.json({}, { status: 404 });
    }),
    http.get("/api/v1/workspaces/:workspaceId/worker-items/:workerItemId/activity", ({ params }) => {
      const current = worldFor(String(params.workerItemId));
      return current ? HttpResponse.json({ items: current.activities, last_change_cursor: 5, next_cursor: null })
        : HttpResponse.json({ items: [], last_change_cursor: 0, next_cursor: null });
    }),
    http.get("/api/v1/workspaces/:workspaceId/worker-items/:workerItemId/result-inputs", () => HttpResponse.json(workerResultInputsFixture())),
    http.get("/api/v1/workspaces/:workspaceId/worker-items/:workerItemId/handoff-candidates", ({ params }) => {
      const workerItemId = String(params.workerItemId);
      candidateReads.push(workerItemId);
      return HttpResponse.json({ candidates: [], as_of: "2026-10-07T00:00:00Z" });
    }),
    http.get("/api/v1/tasks/:rootTaskId/graph", () => HttpResponse.json({
      overview: { title: "Callback contract" },
      nodes: [{ subtask_key: "callback-idempotency", title: "Callback idempotency" }],
    })),
    http.get("/api/v1/workspaces/:workspaceId/tasks/:rootTaskId/results", ({ params }) => HttpResponse.json(resultView({
      workspace_id: String(params.workspaceId),
      root_task_id: String(params.rootTaskId),
      task_title: "Callback contract",
    }))),
    http.get("/api/v1/executions/:executionId", ({ params }) => {
      const current = [...worlds.values()].find((world) => world.execution.id === params.executionId);
      return current ? HttpResponse.json(current.execution) : HttpResponse.json({}, { status: 404 });
    }),
    http.get("/api/v1/gateway/sessions", () => HttpResponse.json({
      items: [...worlds.values()].map((world) => world.session),
      next_cursor: null,
    })),
    http.get("/api/v1/gateway/inputs", ({ request }) => {
      const search = new URL(request.url).searchParams;
      const executionId = search.get("execution_id");
      const sessionId = search.get("session_id");
      const cursor = search.get("cursor");
      inputRequests.push({ executionId, sessionId, cursor });
      return HttpResponse.json(inputPageResolver(executionId, sessionId, cursor));
    }),
    http.get("/api/v1/workspaces/:workspaceId/commands/:commandId", ({ params }) => {
      const commandId = String(params.commandId);
      commandGets.push(commandId);
      const original = posted.find((command) => command.command_id === commandId);
      const status = commandStatuses.get(commandId) ?? (original === undefined ? null : {
        command_id: original.command_id,
        command_type: original.command_type,
        target_type: original.target_type,
        target_id: original.target_id,
        expected_version: original.expected_version,
        status: "applied",
        result: null,
      } satisfies CommandStatusResponse);
      return status === null ? HttpResponse.json({}, { status: 404 }) : HttpResponse.json(status);
    }),
    http.post("/api/v1/commands", async ({ request }) => {
      const command = await request.json() as CommandRequest;
      posted.push(command);
      csrfHeaders.push(request.headers.get("X-Sidera-CSRF"));
      if (postMode === "lost") return HttpResponse.error();
      if (postMode === "deferred") {
        return await new Promise<HttpResponse<DefaultBodyType>>((resolve) => {
          deferredPostResolver = () => resolve(HttpResponse.json(commandResponse(command)));
        });
      }
      return HttpResponse.json(commandResponse(command));
    }),
    http.post(/.*/, recordNonCommandWrite),
    http.put(/.*/, recordNonCommandWrite),
    http.patch(/.*/, recordNonCommandWrite),
    http.delete(/.*/, recordNonCommandWrite),
  );

  return {
    posted,
    csrfHeaders,
    commandGets,
    inputRequests,
    candidateReads,
    nonCommandWrites,
    setPostMode(mode: "normal" | "lost" | "deferred") { postMode = mode; },
    releaseDeferredPost() {
      if (deferredPostResolver === null) throw new Error("no deferred command POST is waiting");
      deferredPostResolver();
      deferredPostResolver = null;
    },
    setContextUnavailable(workerItemId: string, unavailable: boolean) {
      if (unavailable) unavailableContexts.add(workerItemId);
      else unavailableContexts.delete(workerItemId);
    },
    setContextDenied(workerItemId: string, denied: boolean) {
      if (denied) deniedContexts.add(workerItemId);
      else deniedContexts.delete(workerItemId);
    },
    setWorld(workerItemId: string, world: NativePageWorld) { worlds.set(workerItemId, world); },
    setSessionStatus(workerItemId: string, status: string) {
      const current = worlds.get(workerItemId);
      if (current === undefined) throw new Error("unknown WorkerItem world");
      worlds.set(workerItemId, { ...current, session: { ...current.session, status } });
    },
    setInputPageResolver(resolver: typeof inputPageResolver) { inputPageResolver = resolver; },
    setCommandStatus(commandId: string, status: CommandStatusResponse) { commandStatuses.set(commandId, status); },
    setActivities(workerItemId: string, activities: WorkerActivity[]) {
      const current = worlds.get(workerItemId);
      if (current === undefined) throw new Error("unknown WorkerItem world");
      worlds.set(workerItemId, { ...current, activities });
    },
    getWorkspaceReads(workerItemId: string) { return workspaceReads.get(workerItemId) ?? 0; },
    getContextReads(workerItemId: string) { return contextReads.get(workerItemId) ?? 0; },
    getCompletedContextReads(workerItemId: string) { return completedContextReads.get(workerItemId) ?? 0; },
  };
}

afterEach(() => {
  server.resetHandlers();
  vi.restoreAllMocks();
  localStorage.clear();
  document.cookie = "sidera_csrf=; Max-Age=0; Path=/";
});

describe("NativeWorkerIntervention Page", () => {
  beforeEach(() => {
    server.resetHandlers();
    document.cookie = `sidera_csrf=${PAGE_CSRF_TOKEN}; Path=/`;
  });

  it.each([
    ["A", worldA],
    ["B", worldB],
  ] as const)("keeps world %s canonical Session counters valid", (_name, world) => {
    expect(Number.isSafeInteger(world.session.version)).toBe(true);
    expect(world.session.version).toBeGreaterThan(0);
    expect(Number.isSafeInteger(world.session.last_sequence)).toBe(true);
    expect(world.session.last_sequence).toBeGreaterThanOrEqual(0);
    expect(world.session.last_sequence).toBeLessThan(world.session.version);
    expect(world.context.context!.session_version).toBe(world.session.version);
    expect(world.context.context).not.toHaveProperty("session_last_sequence");
  });

  it("renders the native composer for a native WorkerItem source", async () => {
    installHandlers();
    renderPage();

    expect(await screen.findByRole("heading", { name: "介入当前 native Execution" })).toBeVisible();
    await waitFor(() => expect(screen.getByRole("textbox", { name: "介入这个 Worker" })).toBeEnabled());
  });

  it("enables a mounted pre-admission composer when its WorkerItem workspace changes", async () => {
    const handlers = installHandlers();
    handlers.setWorld(WORKER_ITEM_ID, {
      ...worldA,
      context: {
        ...nativeContext,
        execution: null,
        execution_version: null,
        context: null,
        run: null,
        input_enabled: false,
        read_only_reason: "execution_not_admitted",
      },
      workspace: {
        ...workspace,
        current_runtime: null,
        intervention_context: null,
        trace_descriptor: null,
        can_intervene: false,
        read_only_reason: "execution_not_admitted",
      },
    });
    const user = userEvent.setup();
    const page = renderPage();
    const queue = createInvalidationQueue(page.client, WORKSPACE_ID);

    try {
      const textbox = await screen.findByRole("textbox", { name: "介入这个 Worker" });
      await waitFor(() => expect(handlers.getCompletedContextReads(WORKER_ITEM_ID)).toBe(1));
      expect(textbox).toBeDisabled();
      await user.click(screen.getByText("查看精确 native context"));
      expect(screen.getByText("当前 native context 尚未确认。")).toBeVisible();
      const contextReadsBefore = handlers.getContextReads(WORKER_ITEM_ID);

      handlers.setWorld(WORKER_ITEM_ID, worldA);
      act(() => queue.enqueue(workerWorkspaceChange(1)));

      await waitFor(() => expect(textbox).toBeEnabled());
      expect(handlers.getContextReads(WORKER_ITEM_ID)).toBeGreaterThan(contextReadsBefore);
      expect(await screen.findByText(RUN_ID)).toBeVisible();
      expect(screen.getByRole("button", { name: "发送到当前 Session" })).toBeDisabled();
      expect(handlers.posted).toHaveLength(0);
      expect(handlers.nonCommandWrites).toEqual([]);
    } finally {
      queue.close();
    }
  });

  it("preserves the reviewed draft and disables Send when the stream closes its native Run", async () => {
    const handlers = installHandlers();
    const user = userEvent.setup();
    const page = renderPage();
    const queue = createInvalidationQueue(page.client, WORKSPACE_ID);

    try {
      const textbox = await screen.findByRole("textbox", { name: "介入这个 Worker" });
      await waitFor(() => expect(textbox).toBeEnabled());
      const initialReads = handlers.getContextReads(WORKER_ITEM_ID);
      act(() => queue.enqueue(workerWorkspaceChange(1)));
      await waitFor(() => expect(handlers.getContextReads(WORKER_ITEM_ID)).toBeGreaterThan(initialReads));
      await user.type(textbox, "Keep this reviewed draft after completion");
      await user.click(screen.getByRole("button", { name: "复核当前 context" }));
      const send = screen.getByRole("button", { name: "发送到当前 Session" });
      await waitFor(() => expect(send).toBeEnabled());
      const originalReview = storedDraft(WORKER_ITEM_ID).review;
      expect(originalReview?.context.run_id).toBe(RUN_ID);
      const contextReadsBefore = handlers.getContextReads(WORKER_ITEM_ID);
      await user.click(screen.getByText("查看精确 native context"));

      handlers.setWorld(WORKER_ITEM_ID, {
        ...worldA,
        context: {
          ...nativeContext,
          worker_item_version: 10,
          execution: { ...executionPin, status: "succeeded", updated_at: "2026-10-07T00:00:02Z" },
          execution_version: 8,
          context: { ...context, session_version: 6 },
          input_enabled: false,
          read_only_reason: "session_closed",
          run: { ...nativeContext.run!, state: "completed", input_open: false },
          as_of: "2026-10-07T00:00:02Z",
        },
        workspace: {
          ...workspace,
          worker_item: { ...workspace.worker_item, version: 10, updated_at: "2026-10-07T00:00:02Z" },
          current_runtime: {
            ...workspace.current_runtime!,
            execution_status: "succeeded",
            session_status: "closed",
            session_version: 6,
            lease_id: null,
          },
          intervention_context: { ...workspace.intervention_context!, session_version: 6 },
          can_intervene: false,
          read_only_reason: "session_closed",
          last_change_cursor: 6,
          as_of: "2026-10-07T00:00:02Z",
        },
        execution: {
          ...execution,
          active_lease_id: null,
          status: "succeeded",
          version: 8,
          finished_at: "2026-10-07T00:00:02Z",
          updated_at: "2026-10-07T00:00:02Z",
        },
        session: { ...session, status: "closed", version: 6, updated_at: "2026-10-07T00:00:02Z" },
      });
      act(() => queue.enqueue({
        ...workerWorkspaceChange(2),
        change_type: "ExecutionSucceeded",
        resource_version: 10,
      }));

      expect(await screen.findByText(`${SESSION_ID} · V6`)).toBeVisible();
      await waitFor(() => expect(send).toBeDisabled());
      expect(handlers.getContextReads(WORKER_ITEM_ID)).toBeGreaterThan(contextReadsBefore);
      expect(textbox).toHaveValue("Keep this reviewed draft after completion");
      expect(storedDraft(WORKER_ITEM_ID).review).toEqual(originalReview);
      expect(handlers.posted).toHaveLength(0);

      await user.click(screen.getByRole("button", { name: "复核当前 context" }));
      await waitFor(() => expect(storedDraft(WORKER_ITEM_ID).review).toMatchObject({
        context: { run_id: RUN_ID, session_version: 6 },
        execution_version: 8,
      }));
      expect(send).toBeDisabled();
      expect(textbox).toHaveValue("Keep this reviewed draft after completion");
      expect(handlers.posted).toHaveLength(0);
      expect(handlers.nonCommandWrites).toEqual([]);
    } finally {
      queue.close();
    }
  });

  it("keeps the old review pinned across a streamed owner and Run change until explicit review", async () => {
    const handlers = installHandlers();
    const user = userEvent.setup();
    const page = renderPage(WORKER_ITEM_B_ID);
    const queue = createInvalidationQueue(page.client, WORKSPACE_ID);

    try {
      const bTextbox = await screen.findByRole("textbox", { name: "介入这个 Worker" });
      await waitFor(() => expect(bTextbox).toBeEnabled());
      await user.type(bTextbox, "B independent draft");
      const unrelatedDraft = storedDraft(WORKER_ITEM_B_ID);

      await user.click(screen.getByRole("button", { name: "Open worker A" }));
      const textbox = await screen.findByRole("textbox", { name: "介入这个 Worker" });
      await waitFor(() => expect(textbox).toBeEnabled());
      const initialReads = handlers.getContextReads(WORKER_ITEM_ID);
      act(() => queue.enqueue(workerWorkspaceChange(1)));
      await waitFor(() => expect(handlers.getContextReads(WORKER_ITEM_ID)).toBeGreaterThan(initialReads));
      await user.type(textbox, "A draft stays pinned until review");
      await user.click(screen.getByRole("button", { name: "复核当前 context" }));
      const send = screen.getByRole("button", { name: "发送到当前 Session" });
      await waitFor(() => expect(send).toBeEnabled());
      const originalReview = storedDraft(WORKER_ITEM_ID).review;
      expect(originalReview?.context.run_id).toBe(RUN_ID);
      const contextReadsBefore = handlers.getContextReads(WORKER_ITEM_ID);
      await user.click(screen.getByText("查看精确 native context"));
      const replacementBinding: AgentBindingView = { ...binding, version: 3, profile_ref: PROFILE_HEAD_R2 };

      handlers.setWorld(WORKER_ITEM_ID, {
        ...worldB,
        binding: replacementBinding,
        context: {
          ...nativeContextB,
          worker_item_id: WORKER_ITEM_ID,
          worker_item_version: 10,
          binding: replacementBinding,
          execution: {
            ...nativeContextB.execution!,
            worker_item_id: WORKER_ITEM_ID,
            profile_ref: PROFILE_HEAD_R2,
            binding_version: 3,
          },
          context: { ...contextB, worker_item_id: WORKER_ITEM_ID, binding_version: 3 },
          run: { ...nativeContextB.run!, profile_ref: PROFILE_HEAD_R2 },
        },
        workspace: {
          ...workspaceB,
          worker_item: { ...workspace.worker_item, version: 10 },
          intervention_context: { ...workspaceB.intervention_context!, worker_item_id: WORKER_ITEM_ID },
        },
        execution: { ...executionB, worker_item_id: WORKER_ITEM_ID, assigned_worker_id: WORKER_B_ID },
      });
      act(() => queue.enqueue({ ...workerWorkspaceChange(2), resource_version: 10 }));

      expect(await screen.findByText(RUN_B_ID)).toBeVisible();
      await waitFor(() => expect(send).toBeDisabled());
      expect(handlers.getContextReads(WORKER_ITEM_ID)).toBeGreaterThan(contextReadsBefore);
      expect(textbox).toHaveValue("A draft stays pinned until review");
      expect(storedDraft(WORKER_ITEM_ID).review).toEqual(originalReview);
      expect(storedDraft(WORKER_ITEM_B_ID)).toEqual(unrelatedDraft);
      expect(handlers.posted).toHaveLength(0);

      await user.click(screen.getByRole("button", { name: "复核当前 context" }));
      await waitFor(() => expect(storedDraft(WORKER_ITEM_ID).review).toMatchObject({
        context: {
          worker_item_id: WORKER_ITEM_ID,
          execution_id: EXECUTION_B_ID,
          session_id: SESSION_B_ID,
          worker_id: WORKER_B_ID,
          run_id: RUN_B_ID,
          binding_version: 3,
        },
        profile_ref: PROFILE_HEAD_R2,
        binding_version: 3,
      }));
      await waitFor(() => expect(send).toBeEnabled());
      expect(textbox).toHaveValue("A draft stays pinned until review");
      expect(storedDraft(WORKER_ITEM_B_ID)).toEqual(unrelatedDraft);
      expect(handlers.posted).toHaveLength(0);
      expect(handlers.nonCommandWrites).toEqual([]);
    } finally {
      queue.close();
    }
  });

  it("shows the saved R1 execution pin separately from current binding head R2", async () => {
    const handlers = installHandlers();
    const currentBinding = { ...binding, profile_ref: PROFILE_HEAD_R2 };
    handlers.setWorld(WORKER_ITEM_ID, {
      ...worldA,
      binding: currentBinding,
      context: { ...nativeContext, binding: currentBinding },
    });
    const user = userEvent.setup();
    renderPage();

    const textbox = await screen.findByRole("textbox", { name: "介入这个 Worker" });
    await waitFor(() => expect(textbox).toBeEnabled());
    await user.click(screen.getByText("查看精确 native context"));
    expect(await screen.findByText(`${PROFILE_REF.profile_id} · r1`)).toBeVisible();
    expect(screen.getByText(`${PROFILE_HEAD_R2.profile_id} · r2`)).toBeVisible();
    expect(screen.getByRole("button", { name: "发送到当前 Session" })).toBeDisabled();
    expect(handlers.posted).toHaveLength(0);
    expect(handlers.candidateReads).toEqual([]);
    expect(handlers.nonCommandWrites).toEqual([]);
  });

  it("keeps text review pinned to the old Run until explicit review after source refresh", async () => {
    const handlers = installHandlers();
    const user = userEvent.setup();
    const page = renderPage();
    const textbox = await screen.findByRole("textbox", { name: "介入这个 Worker" });
    await waitFor(() => expect(textbox).toBeEnabled());
    await user.type(textbox, "initial reviewed text");
    await user.click(screen.getByRole("button", { name: "复核当前 context" }));
    const send = screen.getByRole("button", { name: "发送到当前 Session" });
    await waitFor(() => expect(send).toBeEnabled());
    expect(storedDraft(WORKER_ITEM_ID).review?.context.run_id).toBe(RUN_ID);

    const changedRunId = "90000000-0000-0000-0000-000000000003";
    handlers.setWorld(WORKER_ITEM_ID, {
      ...worldA,
      context: {
        ...nativeContext,
        context: { ...context, run_id: changedRunId },
        execution: { ...executionPin, run_id: changedRunId },
      },
    });
    await page.client.invalidateQueries();
    await waitFor(() => expect(screen.getByRole("button", { name: "发送到当前 Session" })).toBeDisabled());
    await user.click(screen.getByText("查看精确 native context"));
    expect(await screen.findByText(changedRunId)).toBeVisible();

    await user.clear(textbox);
    await user.type(textbox, "edited after Run changed");
    expect(storedDraft(WORKER_ITEM_ID)).toMatchObject({
      text: "edited after Run changed",
      review: { context: { run_id: RUN_ID } },
    });
    expect(handlers.posted).toHaveLength(0);

    await user.click(screen.getByRole("button", { name: "复核当前 context" }));
    await waitFor(() => expect(storedDraft(WORKER_ITEM_ID).review?.context.run_id).toBe(changedRunId));
    await waitFor(() => expect(screen.getByRole("button", { name: "发送到当前 Session" })).toBeEnabled());
    expect(textbox).toHaveValue("edited after Run changed");
    expect(handlers.posted).toHaveLength(0);
    expect(handlers.candidateReads).toEqual([]);
    expect(handlers.nonCommandWrites).toEqual([]);
  });

  it("keeps A and B drafts separate when A's POST resolves after the Page unmounts", async () => {
    const handlers = installHandlers();
    const user = userEvent.setup();
    renderPage();
    const textbox = await screen.findByRole("textbox", { name: "介入这个 Worker" });
    await waitFor(() => expect(textbox).toBeEnabled());
    await user.type(textbox, "A original draft");
    await user.click(screen.getByRole("button", { name: "复核当前 context" }));
    const send = screen.getByRole("button", { name: "发送到当前 Session" });
    await waitFor(() => expect(send).toBeEnabled());
    handlers.setPostMode("deferred");
    await user.click(send);
    await waitFor(() => expect(handlers.posted).toHaveLength(1));

    await user.click(screen.getByRole("button", { name: "Open worker B" }));
    expect(await screen.findByRole("heading", { name: "介入当前 native Execution" })).toBeVisible();
    const bTextbox = await screen.findByRole("textbox", { name: "介入这个 Worker" });
    await waitFor(() => expect(bTextbox).toBeEnabled());
    await user.type(bTextbox, "B independent draft");
    const beforeLateResponseA = storedDraft(WORKER_ITEM_ID);
    const beforeLateResponseB = storedDraft(WORKER_ITEM_B_ID);
    expect(beforeLateResponseA.input.status).toBe("pending");
    expect(beforeLateResponseA.input.original?.command_id).toBe(handlers.posted[0]?.command_id);
    expect(beforeLateResponseB).toMatchObject({ text: "B independent draft", input: { status: "idle", original: null } });

    handlers.releaseDeferredPost();
    await new Promise<void>((resolve) => window.setTimeout(resolve, 0));
    await user.click(screen.getByRole("button", { name: "Open worker A" }));
    const reloadedATextbox = await screen.findByRole("textbox", { name: "介入这个 Worker" });
    await waitFor(() => expect(reloadedATextbox).toBeEnabled());
    expect(storedDraft(WORKER_ITEM_ID).input.status).toBe("pending");
    expect(storedDraft(WORKER_ITEM_B_ID).text).toBe("B independent draft");

    await user.click(await screen.findByRole("button", { name: "查询原始输入命令" }));
    await waitFor(() => expect(handlers.commandGets).toEqual([handlers.posted[0]?.command_id]));
    expect(handlers.posted).toHaveLength(1);
  });

  it("does not create A's original when its held Page preflight resolves after navigating to B", async () => {
    let holdNextAContext = false;
    let signalContextStarted!: () => void;
    let releaseAContext!: () => void;
    const contextStarted = new Promise<void>((resolve) => { signalContextStarted = resolve; });
    const heldContext = new Promise<void>((resolve) => { releaseAContext = resolve; });
    const handlers = installHandlers({
      beforeNativeContextResponse: async (workerItemId) => {
        if (workerItemId !== WORKER_ITEM_ID || !holdNextAContext) return;
        holdNextAContext = false;
        signalContextStarted();
        await heldContext;
      },
    });
    const user = userEvent.setup();
    const page = renderPage();
    const aTextbox = await screen.findByRole("textbox", { name: "介入这个 Worker" });
    await waitFor(() => expect(aTextbox).toBeEnabled());
    await user.type(aTextbox, "A held preflight draft");
    await user.click(screen.getByRole("button", { name: "复核当前 context" }));
    const send = screen.getByRole("button", { name: "发送到当前 Session" });
    await waitFor(() => expect(send).toBeEnabled());
    const keyA = nativeInterventionDraftKey({
      workspace_id: WORKSPACE_ID,
      principal_id: PRINCIPAL_ID,
      worker_item_id: WORKER_ITEM_ID,
    });
    const beforePreflight = localStorage.getItem(keyA);
    const completedReadsBefore = handlers.getCompletedContextReads(WORKER_ITEM_ID);
    const uuidSpy = vi.spyOn(globalThis.crypto, "randomUUID");
    holdNextAContext = true;

    await act(async () => {
      await user.click(send);
      await contextStarted;
    });
    expect(handlers.posted).toHaveLength(0);
    expect(storedDraft(WORKER_ITEM_ID).input).toMatchObject({ status: "idle", original: null });

    await user.click(screen.getByRole("button", { name: "Open worker B" }));
    const bTextbox = await screen.findByRole("textbox", { name: "介入这个 Worker" });
    await waitFor(() => expect(bTextbox).toBeEnabled());
    await user.type(bTextbox, "B stays independent during A preflight");
    const keyB = nativeInterventionDraftKey({
      workspace_id: WORKSPACE_ID,
      principal_id: PRINCIPAL_ID,
      worker_item_id: WORKER_ITEM_B_ID,
    });
    const beforeReleaseB = localStorage.getItem(keyB);

    releaseAContext();
    await waitFor(() => expect(handlers.getCompletedContextReads(WORKER_ITEM_ID)).toBeGreaterThan(completedReadsBefore));
    await new Promise<void>((resolve) => window.setTimeout(resolve, 0));

    expect(localStorage.getItem(keyA)).toBe(beforePreflight);
    expect(localStorage.getItem(keyB)).toBe(beforeReleaseB);
    expect(storedDraft(WORKER_ITEM_ID).input).toMatchObject({ status: "idle", original: null });
    expect(storedDraft(WORKER_ITEM_B_ID)).toMatchObject({
      text: "B stays independent during A preflight",
      input: { status: "idle", original: null },
    });
    expect(handlers.posted).toHaveLength(0);
    expect(uuidSpy).not.toHaveBeenCalled();
    expect(handlers.candidateReads).toEqual([]);
    expect(handlers.nonCommandWrites).toEqual([]);
    uuidSpy.mockRestore();
    page.unmount();
  });

  it("looks up an unknown original command after an unavailable native-context reload without writes", async () => {
    const handlers = installHandlers();
    const user = userEvent.setup();
    const firstPage = renderPage();
    const textbox = await screen.findByRole("textbox", { name: "介入这个 Worker" });
    await waitFor(() => expect(textbox).toBeEnabled());
    await user.type(textbox, "Recover this original input");
    await user.click(screen.getByRole("button", { name: "复核当前 context" }));
    const send = screen.getByRole("button", { name: "发送到当前 Session" });
    await waitFor(() => expect(send).toBeEnabled());
    handlers.setPostMode("lost");
    await user.click(send);
    await waitFor(() => expect(screen.getByLabelText("原生操作状态")).toHaveTextContent("输入命令：状态未确认"));
    const commandId = handlers.posted[0]?.command_id;
    expect(commandId).toBeDefined();
    firstPage.unmount();

    handlers.setContextUnavailable(WORKER_ITEM_ID, true);
    handlers.setSessionStatus(WORKER_ITEM_ID, "closed");
    renderPage(WORKER_ITEM_ID, new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } }));
    expect(await screen.findByRole("heading", { name: "介入当前 native Execution" })).toBeVisible();
    const reloadedTextbox = await screen.findByRole("textbox", { name: "介入这个 Worker" });
    await waitFor(() => expect(reloadedTextbox).toBeDisabled());
    expect(screen.getByRole("button", { name: "发送到当前 Session" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "停止 Execution" })).toBeDisabled();
    await user.click(await screen.findByRole("button", { name: "查询原始输入命令" }));
    await waitFor(() => expect(handlers.commandGets).toEqual([commandId]));
    expect(handlers.posted).toHaveLength(1);
    expect(handlers.candidateReads).toEqual([]);
    expect(handlers.nonCommandWrites).toEqual([]);
  });

  it("does not submit when fresh native facts are denied", async () => {
    const handlers = installHandlers();
    const user = userEvent.setup();
    renderPage();
    const textbox = await screen.findByRole("textbox", { name: "介入这个 Worker" });
    await waitFor(() => expect(textbox).toBeEnabled());
    await user.type(textbox, "Denied fresh source must not submit");
    await user.click(screen.getByRole("button", { name: "复核当前 context" }));
    const send = screen.getByRole("button", { name: "发送到当前 Session" });
    await waitFor(() => expect(send).toBeEnabled());
    const contextReadsBefore = handlers.getContextReads(WORKER_ITEM_ID);
    handlers.setContextDenied(WORKER_ITEM_ID, true);

    await user.click(send);
    await waitFor(() => expect(handlers.getContextReads(WORKER_ITEM_ID)).toBeGreaterThan(contextReadsBefore));
    expect(await screen.findByRole("alert")).toHaveTextContent("当前 native 执行事实暂不可用");
    expect(handlers.posted).toHaveLength(0);
    expect(storedDraft(WORKER_ITEM_ID).input).toMatchObject({ status: "idle", original: null });
    expect(handlers.candidateReads).toEqual([]);
    expect(handlers.nonCommandWrites).toEqual([]);
  });

  it("does not submit when the Page cannot persist its pending original", async () => {
    const handlers = installHandlers();
    const user = userEvent.setup();
    renderPage();
    const textbox = await screen.findByRole("textbox", { name: "介入这个 Worker" });
    await waitFor(() => expect(textbox).toBeEnabled());
    await user.type(textbox, "Storage must succeed before submit");
    await user.click(screen.getByRole("button", { name: "复核当前 context" }));
    const send = screen.getByRole("button", { name: "发送到当前 Session" });
    await waitFor(() => expect(send).toBeEnabled());
    const key = nativeInterventionDraftKey({
      workspace_id: WORKSPACE_ID,
      principal_id: PRINCIPAL_ID,
      worker_item_id: WORKER_ITEM_ID,
    });
    const setItem = Storage.prototype.setItem;
    let failPendingWrite = true;
    const setItemSpy = vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (this: Storage, storageKey: string, value: string) {
      if (failPendingWrite && storageKey === key && JSON.parse(value).input.status === "pending") {
        failPendingWrite = false;
        throw new Error("storage blocked");
      }
      setItem.call(this, storageKey, value);
    });

    await user.click(send);
    expect(await screen.findByText("本地介入记录暂不可用；保存成功前不会提交。")).toBeVisible();
    expect(handlers.posted).toHaveLength(0);
    expect(storedDraft(WORKER_ITEM_ID).input).toMatchObject({ status: "idle", original: null });
    expect(handlers.candidateReads).toEqual([]);
    expect(handlers.nonCommandWrites).toEqual([]);
    setItemSpy.mockRestore();
  });

  it("retains a correlated handled POST when Page refresh fails and still permits original GET", async () => {
    const handlers = installHandlers();
    const user = userEvent.setup();
    const page = renderPage();
    const textbox = await screen.findByRole("textbox", { name: "介入这个 Worker" });
    await waitFor(() => expect(textbox).toBeEnabled());
    await user.type(textbox, "Handled before refresh failure");
    await user.click(screen.getByRole("button", { name: "复核当前 context" }));
    const send = screen.getByRole("button", { name: "发送到当前 Session" });
    await waitFor(() => expect(send).toBeEnabled());

    const invalidateQueries = page.client.invalidateQueries.bind(page.client);
    vi.spyOn(page.client, "invalidateQueries").mockImplementation((filters, options) => {
      if (handlers.posted.length > 0) return Promise.reject(new Error("refresh unavailable"));
      return invalidateQueries(filters, options);
    });
    await user.click(send);
    await waitFor(() => expect(handlers.posted).toHaveLength(1));
    const commandId = handlers.posted[0]?.command_id;
    expect(commandId).toBeDefined();
    expect(await screen.findByRole("alert")).toHaveTextContent("操作已记录，但工作区刷新暂不可用。");
    expect(screen.getByLabelText("原生操作状态")).toHaveTextContent("输入命令：命令已处理");

    await user.click(screen.getByRole("button", { name: "查询原始输入命令" }));
    await waitFor(() => expect(handlers.commandGets).toEqual([commandId]));
    expect(handlers.posted).toHaveLength(1);
    expect(storedDraft(WORKER_ITEM_ID).input).toMatchObject({ status: "applied", original: { command_id: commandId } });
  });

  it("allows an independent version-7 Stop after UNKNOWN input and recovers that exact Stop by GET", async () => {
    const handlers = installHandlers();
    const user = userEvent.setup();
    renderPage();
    const textbox = await screen.findByRole("textbox", { name: "介入这个 Worker" });
    await waitFor(() => expect(textbox).toBeEnabled());
    await user.type(textbox, "Input reply is uncertain");
    await user.click(screen.getByRole("button", { name: "复核当前 context" }));
    const send = screen.getByRole("button", { name: "发送到当前 Session" });
    await waitFor(() => expect(send).toBeEnabled());
    handlers.setPostMode("lost");
    await user.click(send);
    await waitFor(() => expect(handlers.posted).toHaveLength(1));
    await waitFor(() => expect(screen.getByLabelText("原生操作状态")).toHaveTextContent("输入命令：状态未确认"));
    expect(storedDraft(WORKER_ITEM_ID).input.status).toBe("unknown");

    handlers.setPostMode("normal");
    const stop = screen.getByRole("button", { name: "停止 Execution" });
    await waitFor(() => expect(stop).toBeEnabled());
    await user.click(stop);
    await waitFor(() => expect(handlers.posted).toHaveLength(2));
    const inputCommand = handlers.posted[0];
    const stopCommand = handlers.posted[1];
    if (inputCommand === undefined || stopCommand === undefined) throw new Error("expected input and Stop originals");
    expect(inputCommand.command_type).toBe("SendExecutionInput");
    expect(stopCommand).toMatchObject({
      actor: { type: "user", id: PRINCIPAL_ID },
      command_type: "StopExecution",
      target_type: "execution",
      target_id: EXECUTION_ID,
      expected_version: 7,
      payload: {},
    });
    expect(handlers.csrfHeaders).toEqual([PAGE_CSRF_TOKEN, PAGE_CSRF_TOKEN]);
    expect(storedDraft(WORKER_ITEM_ID)).toMatchObject({
      input: { status: "unknown", original: inputCommand },
      stop: { status: "applied", original: stopCommand },
    });
    expect(handlers.posted.filter((command) => command.command_type === "SendExecutionInput")).toHaveLength(1);

    await user.click(screen.getByRole("button", { name: "查询原始 Stop 命令" }));
    await waitFor(() => expect(handlers.commandGets).toEqual([stopCommand.command_id]));
    expect(screen.getByLabelText("原生操作状态")).toHaveTextContent("Execution：停止请求已记录");
    expect(handlers.posted).toHaveLength(2);
    expect(handlers.candidateReads).toEqual([]);
    expect(handlers.nonCommandWrites).toEqual([]);
  });

  it("recovers a missing first-page input through more, then refreshes it to delivered", async () => {
    const handlers = installHandlers();
    const user = userEvent.setup();
    renderPage();
    const textbox = await screen.findByRole("textbox", { name: "介入这个 Worker" });
    await waitFor(() => expect(textbox).toBeEnabled());
    await user.type(textbox, "Read native delivery");
    await user.click(screen.getByRole("button", { name: "复核当前 context" }));
    const send = screen.getByRole("button", { name: "发送到当前 Session" });
    await waitFor(() => expect(send).toBeEnabled());
    await user.click(send);
    await waitFor(() => expect(handlers.posted).toHaveLength(1));
    const sent = handlers.posted[0];
    if (sent === undefined) throw new Error("expected one submitted input command");
    let firstPageRead = 0;
    handlers.setInputPageResolver((_executionId, _sessionId, cursor) => {
      if (cursor === "opaque:missing-page-2") {
        return { items: [inputRecord(sent, "queued", 6)], next_cursor: null };
      }
      firstPageRead += 1;
      return firstPageRead === 1
        ? { items: [], next_cursor: "opaque:missing-page-2" }
        : { items: [inputRecord(sent, "delivered", 7)], next_cursor: null };
    });

    await user.click(screen.getByRole("button", { name: "读取原生投递状态" }));
    await waitFor(() => expect(screen.getByLabelText("原生操作状态")).toHaveTextContent("实际投递：尚未找到原生投递记录"));
    await waitFor(() => expect(screen.getByRole("button", { name: "读取更多" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "读取更多" }));
    await waitFor(() => expect(screen.getByLabelText("原生操作状态")).toHaveTextContent("实际投递：已排队"));
    await user.click(screen.getByRole("button", { name: "读取原生投递状态" }));
    await waitFor(() => expect(screen.getByLabelText("原生操作状态")).toHaveTextContent("实际投递：已投递"));
    expect(handlers.inputRequests).toEqual([
      { executionId: EXECUTION_ID, sessionId: SESSION_ID, cursor: null },
      { executionId: EXECUTION_ID, sessionId: SESSION_ID, cursor: "opaque:missing-page-2" },
      { executionId: EXECUTION_ID, sessionId: SESSION_ID, cursor: null },
    ]);
    expect(screen.queryByRole("button", { name: "读取更多" })).not.toBeInTheDocument();
    expect(handlers.posted).toHaveLength(1);
    expect(handlers.candidateReads).toEqual([]);
  });

  it("rejects a delivery page whose input execution identity differs from the reviewed source", async () => {
    const handlers = installHandlers();
    const user = userEvent.setup();
    renderPage();
    const textbox = await screen.findByRole("textbox", { name: "介入这个 Worker" });
    await waitFor(() => expect(textbox).toBeEnabled());
    await user.type(textbox, "Reject foreign status identity");
    await user.click(screen.getByRole("button", { name: "复核当前 context" }));
    const send = screen.getByRole("button", { name: "发送到当前 Session" });
    await waitFor(() => expect(send).toBeEnabled());
    await user.click(send);
    await waitFor(() => expect(handlers.posted).toHaveLength(1));
    const sent = handlers.posted[0];
    if (sent === undefined) throw new Error("expected one submitted input command");
    handlers.setInputPageResolver(() => ({
      items: [{ ...inputRecord(sent, "delivered", 6), execution_id: EXECUTION_B_ID }],
      next_cursor: null,
    }));

    await user.click(screen.getByRole("button", { name: "读取原生投递状态" }));
    await waitFor(() => expect(screen.getByLabelText("原生操作状态")).toHaveTextContent("实际投递：投递状态暂不可用"));
    expect(handlers.inputRequests).toEqual([{ executionId: EXECUTION_ID, sessionId: SESSION_ID, cursor: null }]);
    expect(screen.queryByRole("button", { name: "读取更多" })).not.toBeInTheDocument();
    expect(handlers.posted).toHaveLength(1);
  });

  it("stops pagination when an input version regresses", async () => {
    const handlers = installHandlers();
    const user = userEvent.setup();
    renderPage();
    const textbox = await screen.findByRole("textbox", { name: "介入这个 Worker" });
    await waitFor(() => expect(textbox).toBeEnabled());
    await user.type(textbox, "Reject an older version");
    await user.click(screen.getByRole("button", { name: "复核当前 context" }));
    const send = screen.getByRole("button", { name: "发送到当前 Session" });
    await waitFor(() => expect(send).toBeEnabled());
    await user.click(send);
    await waitFor(() => expect(handlers.posted).toHaveLength(1));
    const sent = handlers.posted[0];
    if (sent === undefined) throw new Error("expected one submitted input command");
    handlers.setInputPageResolver((_executionId, _sessionId, cursor) => cursor === null
      ? { items: [inputRecord(sent, "queued", 6)], next_cursor: "opaque:version-2" }
      : { items: [inputRecord(sent, "queued", 5)], next_cursor: "opaque:version-3" });

    await user.click(screen.getByRole("button", { name: "读取原生投递状态" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "读取更多" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "读取更多" }));
    await waitFor(() => expect(screen.getByLabelText("原生操作状态")).toHaveTextContent("实际投递：投递状态暂不可用"));
    expect(handlers.inputRequests.map(({ cursor }) => cursor)).toEqual([null, "opaque:version-2"]);
    expect(screen.queryByRole("button", { name: "读取更多" })).not.toBeInTheDocument();
    expect(handlers.posted).toHaveLength(1);
  });

  it("stops pagination when the server repeats an opaque cursor", async () => {
    const handlers = installHandlers();
    const user = userEvent.setup();
    renderPage();
    const textbox = await screen.findByRole("textbox", { name: "介入这个 Worker" });
    await waitFor(() => expect(textbox).toBeEnabled());
    await user.type(textbox, "Reject a repeated cursor");
    await user.click(screen.getByRole("button", { name: "复核当前 context" }));
    const send = screen.getByRole("button", { name: "发送到当前 Session" });
    await waitFor(() => expect(send).toBeEnabled());
    await user.click(send);
    await waitFor(() => expect(handlers.posted).toHaveLength(1));
    handlers.setInputPageResolver((_executionId, _sessionId, cursor) => ({
      items: [],
      next_cursor: cursor === null ? "opaque:repeat" : "opaque:repeat",
    }));

    await user.click(screen.getByRole("button", { name: "读取原生投递状态" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "读取更多" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "读取更多" }));
    await waitFor(() => expect(screen.getByLabelText("原生操作状态")).toHaveTextContent("实际投递：投递状态暂不可用"));
    expect(handlers.inputRequests.map(({ cursor }) => cursor)).toEqual([null, "opaque:repeat"]);
    expect(screen.queryByRole("button", { name: "读取更多" })).not.toBeInTheDocument();
    expect(handlers.posted).toHaveLength(1);
  });

  it("loads a superseded activity body from a prior Execution into the current Page context", async () => {
    const handlers = installHandlers();
    const body = "Canonical historical body for a fresh current-Execution input";
    handlers.setActivities(WORKER_ITEM_ID, [supersededActivity(body)]);
    const user = userEvent.setup();
    renderPage();
    const textbox = await screen.findByRole("textbox", { name: "介入这个 Worker" });
    await waitFor(() => expect(textbox).toBeEnabled());
    expect(await screen.findByText(body, { selector: "blockquote" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "选择此失效输入" }));
    await user.click(screen.getByRole("button", { name: "载入为新介入草稿" }));
    expect(textbox).toHaveValue(body);
    await user.click(screen.getByRole("button", { name: "复核当前 context" }));
    const send = screen.getByRole("button", { name: "发送到当前 Session" });
    await waitFor(() => expect(send).toBeEnabled());
    await user.click(send);
    await waitFor(() => expect(handlers.posted).toHaveLength(1));
    const submitted = handlers.posted[0];
    if (submitted === undefined) throw new Error("expected one submitted input command");
    expect(inputPayload(submitted)).toEqual({
      context: { execution_id: EXECUTION_ID, session_id: SESSION_ID },
      client_idempotency_key: expect.any(String),
      text: body,
    });
    expect(handlers.posted).toHaveLength(1);
  });
});
