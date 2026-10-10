import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { createElement, type PropsWithChildren } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PROFILE_REF } from "@/features/agents/testFixtures";
import { nativeWorkerContextQueryKey } from "@/features/assignments/api";
import type { LocalSession } from "@/features/local-session/api";
import { server } from "@/test/server";
import type {
  AgentBindingView,
  AgentExecutionView,
  CommandRequest,
  CommandResponse,
  CommandStatusResponse,
  NativeWorkerContextView,
} from "@/features/assignments/types";
import type { WorkerWorkspace } from "@/features/worker-items/types";
import { workerWorkspaceQueryKey } from "@/features/worker-items/api";
import type { Execution, ExecutionInput, ExecutionSession } from "./types";
import {
  loadNativeInterventionDraft,
  nativeInterventionDraftKey,
  saveNativeInterventionDraft,
  type NativeInterventionDraft,
  type NativeInterventionReview,
} from "./nativeIntervention";
import { useNativeIntervention } from "./useNativeIntervention";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const WORKER_ITEM_ID = "40000000-0000-0000-0000-000000000001";
const ROOT_TASK_ID = "30000000-0000-0000-0000-000000000001";
const SUBTASK_ID = "30000000-0000-0000-0000-000000000002";
const EXECUTION_ID = "60000000-0000-0000-0000-000000000001";
const SESSION_ID = "70000000-0000-0000-0000-000000000001";
const WORKER_ID = "90000000-0000-0000-0000-000000000001";
const RUN_ID = "80000000-0000-0000-0000-000000000001";
const REPLACEMENT_EXECUTION_ID = "60000000-0000-0000-0000-000000000002";
const REPLACEMENT_SESSION_ID = "70000000-0000-0000-0000-000000000002";
const REPLACEMENT_WORKER_ID = "90000000-0000-0000-0000-000000000002";
const REPLACEMENT_RUN_ID = "80000000-0000-0000-0000-000000000002";
const REPLACEMENT_LEASE_ID = "a0000000-0000-0000-0000-000000000002";
const INPUT_ID = "50000000-0000-0000-0000-000000000001";
const PRINCIPAL_ID = "10000000-0000-0000-0000-000000000001";
const BASE = `/api/v1/workspaces/${WORKSPACE_ID}`;

const localSession: LocalSession = {
  session_id: "d0000000-0000-0000-0000-000000000001",
  workspace_id: WORKSPACE_ID,
  principal_id: PRINCIPAL_ID,
  expires_at: "2099-01-01T00:00:00Z",
};

const binding: AgentBindingView = {
  workspace_id: WORKSPACE_ID,
  target_type: "worker_item",
  target_id: WORKER_ITEM_ID,
  version: 2,
  assignee: { kind: "profile", id: PROFILE_REF.profile_id },
  profile_ref: PROFILE_REF,
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
  link_status: "running",
  run_id: RUN_ID,
  profile_ref: PROFILE_REF,
  binding_version: 2,
  event_cursor: 3,
  updated_at: "2026-10-07T00:00:00Z",
};

const context: NonNullable<NativeWorkerContextView["context"]> = {
  worker_item_id: WORKER_ITEM_ID,
  plan_generation: 2,
  context_version: 4,
  execution_id: EXECUTION_ID,
  session_id: SESSION_ID,
  session_version: 5,
  worker_id: WORKER_ID,
  execution_mode: "native_profile",
  binding_version: 2,
  run_id: RUN_ID,
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
    runtime_ref: { workspace_id: WORKSPACE_ID, runtime_id: "c0000000-0000-0000-0000-000000000001" },
    actual_runtime_version: "1",
    applied_fields: [],
    config_hash: "config-hash",
    observed_model: null,
    reason_code: null,
    selected_model: null,
  },
  as_of: "2026-10-07T00:00:00Z",
};

const workspace = {
  workspace_id: WORKSPACE_ID,
  worker_item: {
    id: WORKER_ITEM_ID,
    root_task_id: ROOT_TASK_ID,
    status: "active",
    version: 9,
    context_version: 4,
    acknowledged_context_version: 4,
  },
  current_placement: { plan_generation: 2, subtask_task_id: SUBTASK_ID },
  current_runtime: {
    adapter_type: "agent-run",
    execution_id: EXECUTION_ID,
    execution_status: "running",
    session_id: SESSION_ID,
    session_status: "active",
    session_version: 5,
    worker_id: WORKER_ID,
    task_id: SUBTASK_ID,
    lease_id: "a0000000-0000-0000-0000-000000000001",
  },
  active_handoff: null,
} as unknown as WorkerWorkspace;

const execution = {
  id: EXECUTION_ID,
  workspace_id: WORKSPACE_ID,
  worker_item_id: WORKER_ITEM_ID,
  task_id: SUBTASK_ID,
  version: 7,
  status: "running",
  stop_requested: false,
  active_lease_id: "a0000000-0000-0000-0000-000000000001",
} as unknown as Execution;

const session = {
  id: SESSION_ID,
  workspace_id: WORKSPACE_ID,
  execution_id: EXECUTION_ID,
  worker_id: WORKER_ID,
  version: 5,
  status: "active",
} as unknown as ExecutionSession;

const streamingNativeContext: NativeWorkerContextView = {
  ...nativeContext,
  context: { ...context, session_last_sequence: 3 },
  execution: { ...executionPin, event_cursor: 99 },
};
const streamingWorkspace: WorkerWorkspace = {
  ...workspace,
  current_runtime: { ...workspace.current_runtime!, session_version: 4 },
};
const streamingSession: ExecutionSession = { ...session, version: 7, last_sequence: 5 };

function streamingFacts(version = 10, sequence = 8) {
  return {
    localSession: { ...localSession },
    nativeContext: {
      ...streamingNativeContext,
      context: { ...context, session_version: version, session_last_sequence: sequence },
      binding: { ...binding },
      execution: { ...executionPin },
      run: { ...nativeContext.run! },
    },
    session: { ...session, version, last_sequence: sequence },
    execution: { ...execution },
    workspace: {
      ...streamingWorkspace,
      worker_item: { ...workspace.worker_item },
      current_placement: { ...workspace.current_placement },
      current_runtime: { ...streamingWorkspace.current_runtime! },
    },
  };
}

const staleStreamingCases: Array<{ label: string; change: (facts: ReturnType<typeof streamingFacts>) => void }> = [
  { label: "changed version baseline", change: (facts) => {
    facts.session.last_sequence = 7;
    facts.nativeContext.context.session_last_sequence = 7;
  } },
  { label: "regressed observation", change: (facts) => {
    facts.session.version = 6;
    facts.session.last_sequence = 4;
    facts.nativeContext.context.session_version = 6;
    facts.nativeContext.context.session_last_sequence = 4;
  } },
  { label: "changed Profile revision", change: (facts) => {
    const profile = { ...PROFILE_REF, revision: 2 };
    facts.nativeContext.execution.profile_ref = profile;
    facts.nativeContext.run.profile_ref = profile;
    facts.nativeContext.binding.profile_ref = profile;
  } },
  { label: "changed binding", change: (facts) => {
    facts.nativeContext.context.binding_version = 3;
    facts.nativeContext.execution.binding_version = 3;
    facts.nativeContext.binding.version = 3;
  } },
  { label: "changed Run", change: (facts) => {
    facts.nativeContext.context.run_id = REPLACEMENT_RUN_ID;
    facts.nativeContext.execution.run_id = REPLACEMENT_RUN_ID;
  } },
  { label: "changed Session", change: (facts) => {
    facts.nativeContext.context.session_id = REPLACEMENT_SESSION_ID;
    facts.session.id = REPLACEMENT_SESSION_ID;
    facts.workspace.current_runtime.session_id = REPLACEMENT_SESSION_ID;
  } },
  { label: "changed Worker", change: (facts) => {
    facts.nativeContext.context.worker_id = REPLACEMENT_WORKER_ID;
    facts.session.worker_id = REPLACEMENT_WORKER_ID;
    facts.workspace.current_runtime.worker_id = REPLACEMENT_WORKER_ID;
  } },
  { label: "changed Execution", change: (facts) => {
    facts.nativeContext.context.execution_id = REPLACEMENT_EXECUTION_ID;
    facts.nativeContext.execution.execution_id = REPLACEMENT_EXECUTION_ID;
    facts.session.execution_id = REPLACEMENT_EXECUTION_ID;
    facts.execution.id = REPLACEMENT_EXECUTION_ID;
    facts.workspace.current_runtime.execution_id = REPLACEMENT_EXECUTION_ID;
  } },
  { label: "changed GEN", change: (facts) => {
    facts.nativeContext.context.plan_generation = 3;
    facts.workspace.current_placement.plan_generation = 3;
  } },
  { label: "changed context", change: (facts) => {
    facts.nativeContext.context.context_version = 5;
    facts.workspace.worker_item.context_version = 5;
    facts.workspace.worker_item.acknowledged_context_version = 5;
  } },
  { label: "changed Execution version", change: (facts) => {
    facts.nativeContext.execution_version = 8;
    facts.execution.version = 8;
  } },
  { label: "terminal Execution", change: (facts) => {
    facts.execution.status = "succeeded";
    facts.workspace.current_runtime.execution_status = "succeeded";
  } },
  { label: "terminal Run", change: (facts) => {
    facts.nativeContext.run.state = "completed";
    facts.nativeContext.run.input_open = false;
  } },
  { label: "closed Session", change: (facts) => {
    facts.session.status = "closed";
    facts.workspace.current_runtime.session_status = "closed";
  } },
  { label: "inactive WorkerItem", change: (facts) => { facts.workspace.worker_item.status = "blocked"; } },
  { label: "unacknowledged context", change: (facts) => { facts.workspace.worker_item.acknowledged_context_version = 3; } },
  { label: "active handoff", change: (facts) => { facts.workspace.active_handoff = { status: "prepared" } as NonNullable<WorkerWorkspace["active_handoff"]>; } },
  { label: "requested Stop", change: (facts) => { facts.execution.stop_requested = true; } },
  { label: "changed principal", change: (facts) => { facts.localSession.principal_id = REPLACEMENT_WORKER_ID; } },
  { label: "expired authority", change: (facts) => { facts.localSession.expires_at = "2000-01-01T00:00:00Z"; } },
];

const replacementContext: NonNullable<NativeWorkerContextView["context"]> = {
  ...context,
  execution_id: REPLACEMENT_EXECUTION_ID,
  session_id: REPLACEMENT_SESSION_ID,
  session_version: 8,
  worker_id: REPLACEMENT_WORKER_ID,
  run_id: REPLACEMENT_RUN_ID,
};
const replacementNativeContext: NativeWorkerContextView = {
  ...nativeContext,
  execution: {
    ...executionPin,
    execution_id: REPLACEMENT_EXECUTION_ID,
    run_id: REPLACEMENT_RUN_ID,
  },
  execution_version: 8,
  context: replacementContext,
};
const replacementWorkspace = {
  ...workspace,
  current_runtime: {
    ...workspace.current_runtime!,
    execution_id: REPLACEMENT_EXECUTION_ID,
    execution_status: "running",
    lease_id: REPLACEMENT_LEASE_ID,
    session_id: REPLACEMENT_SESSION_ID,
    session_status: "active",
    session_version: 8,
    worker_id: REPLACEMENT_WORKER_ID,
  },
} as unknown as WorkerWorkspace;
const replacementExecution = {
  ...execution,
  active_lease_id: REPLACEMENT_LEASE_ID,
  assigned_worker_id: REPLACEMENT_WORKER_ID,
  id: REPLACEMENT_EXECUTION_ID,
  lineage_id: REPLACEMENT_EXECUTION_ID,
  version: 8,
} as Execution;
const replacementSession = {
  ...session,
  execution_id: REPLACEMENT_EXECUTION_ID,
  id: REPLACEMENT_SESSION_ID,
  version: 8,
  worker_id: REPLACEMENT_WORKER_ID,
} as ExecutionSession;

const originalStopA: CommandRequest = {
  actor: { type: "user", id: PRINCIPAL_ID },
  command_id: "f0000000-0000-0000-0000-000000000001",
  workspace_id: WORKSPACE_ID,
  command_type: "StopExecution",
  target_type: "execution",
  target_id: EXECUTION_ID,
  expected_version: 7,
  idempotency_key: "f0000000-0000-0000-0000-000000000002",
  correlation_id: "f0000000-0000-0000-0000-000000000003",
  payload: {},
};
const originalStopReviewA: NativeInterventionReview = {
  context,
  execution_version: 7,
  profile_ref: PROFILE_REF,
  binding_version: context.binding_version,
};

function appliedStopDraft(status: "applied" | "pending" | "unknown" = "applied"): NativeInterventionDraft {
  return {
    schema_version: 1,
    scope: { workspace_id: WORKSPACE_ID, principal_id: PRINCIPAL_ID, worker_item_id: WORKER_ITEM_ID },
    text: "",
    client_idempotency_key: null,
    review: null,
    input: { original: null, review: null, status: "idle", aggregate_id: null, aggregate_version: null, rejection_code: null },
    stop: {
      original: originalStopA,
      review: originalStopReviewA,
      status,
      aggregate_id: null,
      aggregate_version: null,
      rejection_code: null,
    },
  };
}

function saveStopDraft(status: "applied" | "pending" | "unknown" = "applied"): NativeInterventionDraft {
  const draft = appliedStopDraft(status);
  const saved = saveNativeInterventionDraft(draft.scope, draft);
  if (!saved.ok) throw new Error("could not save the scoped original Stop fixture");
  return draft;
}

function hookOptions(onRefresh: () => Promise<void> = async () => {}) {
  return {
    workspaceId: WORKSPACE_ID,
    workerItemId: WORKER_ITEM_ID,
    workspace,
    execution,
    nativeContext,
    onRefresh,
  };
}

function queryProvider(client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })) {
  return function Provider({ children }: PropsWithChildren) {
    return createElement(QueryClientProvider, { client }, children);
  };
}

function installSendAndStopHandlers(options: {
  getLocalSession?: () => LocalSession;
  getNativeContext?: () => NativeWorkerContextView;
  getExecution?: (executionId: string) => Execution | undefined;
  getSession?: (sessionId: string) => ExecutionSession | undefined;
  commandResponse?: (command: CommandRequest) => CommandResponse;
  beforeCommandResponse?: (command: CommandRequest) => Promise<void>;
  commandStatus?: (commandId: string, posted: readonly CommandRequest[]) => unknown;
  inputPage?: (page: number, posted: readonly CommandRequest[]) => unknown;
  beforeNativeContextResponse?: () => Promise<void>;
} = {}) {
  const posted: CommandRequest[] = [];
  const draftsAtPost: unknown[] = [];
  const csrfHeaders: Array<string | null> = [];
  const inputCursors: Array<string | null> = [];
  const commandGets: string[] = [];
  let inputPage = 0;
  let nativeContextReads = 0;
  server.use(
    http.get("/product/v1/session", () => HttpResponse.json(options.getLocalSession?.() ?? localSession)),
    http.get(`${BASE}/worker-items/${WORKER_ITEM_ID}/native-context`, async () => {
      nativeContextReads += 1;
      await options.beforeNativeContextResponse?.();
      return HttpResponse.json(options.getNativeContext?.() ?? nativeContext);
    }),
    http.get("/api/v1/executions/:executionId", ({ params }) => HttpResponse.json(
      options.getExecution?.(String(params.executionId)) ?? execution,
    )),
    http.get("/api/v1/gateway/sessions", ({ request }) => {
      const sessionId = new URL(request.url).searchParams.get("session_id") ?? "";
      return HttpResponse.json({ items: [options.getSession?.(sessionId) ?? session], next_cursor: null });
    }),
    http.get("/api/v1/gateway/inputs", ({ request }) => {
      inputPage += 1;
      inputCursors.push(new URL(request.url).searchParams.get("cursor"));
      return HttpResponse.json(options.inputPage?.(inputPage, posted) ?? { items: [], next_cursor: null });
    }),
    http.get(/\/api\/v1\/workspaces\/[^/]+\/commands\/[^/]+$/, ({ request }) => {
      const commandId = new URL(request.url).pathname.split("/").at(-1) ?? "";
      commandGets.push(commandId);
      const original = posted.find((command) => command.command_id === commandId);
      return HttpResponse.json(options.commandStatus?.(commandId, posted) ?? {
        command_id: commandId,
        command_type: original?.command_type ?? null,
        target_type: original?.target_type ?? null,
        target_id: original?.target_id ?? null,
        expected_version: original?.expected_version ?? null,
        status: "pending",
        result: null,
      } satisfies CommandStatusResponse);
    }),
    http.post("/api/v1/commands", async ({ request }) => {
      const command = await request.json() as CommandRequest;
      posted.push(command);
      csrfHeaders.push(request.headers.get("X-Sidera-CSRF"));
      const rawDraft = localStorage.getItem(nativeInterventionDraftKey({
        workspace_id: WORKSPACE_ID,
        principal_id: PRINCIPAL_ID,
        worker_item_id: WORKER_ITEM_ID,
      }));
      draftsAtPost.push(rawDraft === null ? null : JSON.parse(rawDraft));
      await options.beforeCommandResponse?.(command);
      return HttpResponse.json(options.commandResponse?.(command) ?? {
        command_id: command.command_id,
        aggregate_id: command.command_type === "SendExecutionInput" ? INPUT_ID : command.target_id ?? EXECUTION_ID,
        aggregate_version: command.command_type === "SendExecutionInput" ? 1 : (command.expected_version ?? 0) + 1,
        event_ids: [],
      } satisfies CommandResponse);
    }),
  );
  return { posted, draftsAtPost, csrfHeaders, inputCursors, commandGets, nativeContextReads: () => nativeContextReads };
}

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((complete) => { resolve = complete; });
  return { promise, resolve };
}

function observedInput(command: CommandRequest, status: ExecutionInput["status"]): ExecutionInput {
  const payload = command.payload;
  return {
    id: INPUT_ID,
    workspace_id: WORKSPACE_ID,
    execution_id: EXECUTION_ID,
    session_id: SESSION_ID,
    client_idempotency_key: String(payload.client_idempotency_key),
    status,
    operation_id: null,
    failure_code: null,
    version: 1,
    text_length: String(payload.text).length,
    attachment_count: 0,
    created_at: "2026-10-07T00:00:00Z",
    updated_at: "2026-10-07T00:00:00Z",
  };
}

afterEach(() => {
  vi.restoreAllMocks();
  server.resetHandlers();
  localStorage.clear();
  document.cookie = "sidera_csrf=; Max-Age=0; Path=/";
});

describe("useNativeIntervention", () => {
  it("persists canonical review during streaming despite an older cached runtime", async () => {
    const observations = installSendAndStopHandlers({
      getNativeContext: () => streamingNativeContext,
      getSession: () => streamingSession,
    });
    const { result } = renderHook(() => useNativeIntervention({
      ...hookOptions(),
      workspace: streamingWorkspace,
      nativeContext: streamingNativeContext,
    }), { wrapper: queryProvider() });

    await waitFor(() => expect(result.current.sessionAuthorized).toBe(true));
    act(() => result.current.onTextChange("review the streaming Session"));
    await act(async () => { await result.current.reviewCurrentContext(); });

    expect(result.current.error).toBeNull();
    expect(result.current.storageError).toBeNull();
    expect(result.current.nativeFactsAvailable).toBe(true);
    expect(result.current.canSend).toBe(true);
    expect(loadNativeInterventionDraft({
      workspace_id: WORKSPACE_ID,
      principal_id: PRINCIPAL_ID,
      worker_item_id: WORKER_ITEM_ID,
    })).toMatchObject({
      ok: true,
      draft: { review: { context: { ...context, session_version: 7, session_last_sequence: 5 } } },
    });
    expect(observations.posted).toHaveLength(0);
    expect(streamingNativeContext.context).toEqual({ ...context, session_last_sequence: 3 });
  });

  it("sends once after approved observation progress and persists the exact fresh original before POST", async () => {
    let currentNativeContext = streamingNativeContext;
    let currentSession = streamingSession;
    const observations = installSendAndStopHandlers({
      getNativeContext: () => currentNativeContext,
      getSession: () => currentSession,
    });
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    const { result } = renderHook(() => useNativeIntervention({
      ...hookOptions(),
      workspace: streamingWorkspace,
      nativeContext: streamingNativeContext,
    }), { wrapper: queryProvider() });

    await waitFor(() => expect(result.current.sessionAuthorized).toBe(true));
    act(() => result.current.onTextChange("  keep  streaming text\n"));
    await act(async () => { await result.current.reviewCurrentContext(); });
    const approved = loadNativeInterventionDraft({
      workspace_id: WORKSPACE_ID,
      principal_id: PRINCIPAL_ID,
      worker_item_id: WORKER_ITEM_ID,
    });
    expect(approved).toMatchObject({
      ok: true,
      draft: { review: { context: { session_version: 7, session_last_sequence: 5 } } },
    });

    currentNativeContext = {
      ...streamingNativeContext,
      context: { ...context, session_version: 10, session_last_sequence: 8 },
    };
    currentSession = { ...session, version: 10, last_sequence: 8 };
    vi.spyOn(crypto, "randomUUID").mockReturnValueOnce("f0000000-0000-0000-0000-000000000010");
    await act(async () => { await Promise.all([result.current.send(), result.current.send()]); });

    expect(result.current.error).toBeNull();
    expect(observations.posted).toHaveLength(1);
    const original = observations.posted[0];
    expect(original).toEqual({
      command_id: expect.any(String),
      workspace_id: WORKSPACE_ID,
      command_type: "SendExecutionInput",
      actor: { type: "user", id: PRINCIPAL_ID },
      target_type: "execution_session",
      target_id: SESSION_ID,
      expected_version: 10,
      idempotency_key: expect.any(String),
      correlation_id: expect.any(String),
      payload: {
        context: { ...context, session_version: 10, session_last_sequence: 8 },
        client_idempotency_key: "f0000000-0000-0000-0000-000000000010",
        text: "keep  streaming text",
        attachment_refs: [],
      },
    });
    expect(observations.draftsAtPost).toHaveLength(1);
    expect(observations.draftsAtPost[0]).toMatchObject({
      review: { context: { session_version: 7, session_last_sequence: 5 } },
      text: "  keep  streaming text\n",
      client_idempotency_key: "f0000000-0000-0000-0000-000000000010",
      input: {
        status: "pending",
        original,
        review: { context: { ...context, session_version: 10, session_last_sequence: 8 } },
      },
    });
    expect(result.current.inputAction).toMatchObject({ status: "applied", original });
    expect(Object.isFrozen(result.current.inputAction?.original)).toBe(true);
    expect(Object.isFrozen(result.current.inputAction?.original?.payload.context)).toBe(true);
    expect(observations.csrfHeaders).toEqual(["csrf-native"]);
  });

  it("keeps Stop on Execution expected_version while Session observations advance", async () => {
    let currentNativeContext = streamingNativeContext;
    let currentSession = streamingSession;
    const observations = installSendAndStopHandlers({
      getNativeContext: () => currentNativeContext,
      getSession: () => currentSession,
    });
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    const { result } = renderHook(() => useNativeIntervention({
      ...hookOptions(),
      workspace: streamingWorkspace,
      nativeContext: streamingNativeContext,
    }), { wrapper: queryProvider() });

    await waitFor(() => expect(result.current.sessionAuthorized).toBe(true));
    await act(async () => { await result.current.reviewCurrentContext(); });
    currentNativeContext = {
      ...streamingNativeContext,
      context: { ...context, session_version: 10, session_last_sequence: 8 },
    };
    currentSession = { ...session, version: 10, last_sequence: 8 };
    await act(async () => { await result.current.requestStop(); });

    expect(result.current.error).toBeNull();
    expect(observations.posted).toHaveLength(1);
    expect(observations.posted[0]).toMatchObject({
      command_type: "StopExecution",
      target_type: "execution",
      target_id: EXECUTION_ID,
      expected_version: 7,
      payload: {},
    });
    expect(observations.draftsAtPost[0]).toMatchObject({
      stop: {
        status: "pending",
        original: observations.posted[0],
        review: { context: { session_version: 10, session_last_sequence: 8 }, execution_version: 7 },
      },
    });
  });

  it.each(staleStreamingCases)("rejects Send and Stop on $label despite forward proof", async ({ change }) => {
    const initial = streamingFacts(7, 5);
    let current = initial;
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
    const observations = installSendAndStopHandlers({
      getLocalSession: () => current.localSession,
      getNativeContext: () => current.nativeContext,
      getExecution: () => current.execution,
      getSession: () => current.session,
    });
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    const { result } = renderHook(() => useNativeIntervention({
      ...hookOptions(async () => { client.setQueryData(workerWorkspaceQueryKey(WORKSPACE_ID, WORKER_ITEM_ID), current.workspace); }),
      workspace: initial.workspace,
      nativeContext: initial.nativeContext,
    }), { wrapper: queryProvider(client) });

    await waitFor(() => expect(result.current.sessionAuthorized).toBe(true));
    act(() => result.current.onTextChange("retain approved draft"));
    await act(async () => { await result.current.reviewCurrentContext(); });
    expect(result.current.canSend).toBe(true);
    expect(result.current.canStop).toBe(true);
    const scope = { workspace_id: WORKSPACE_ID, principal_id: PRINCIPAL_ID, worker_item_id: WORKER_ITEM_ID };
    const saved = loadNativeInterventionDraft(scope);
    const uuidSpy = vi.spyOn(crypto, "randomUUID");
    current = streamingFacts();
    change(current);

    await act(async () => { await result.current.send(); });
    await act(async () => { await result.current.requestStop(); });

    expect(observations.posted).toHaveLength(0);
    expect(uuidSpy).not.toHaveBeenCalled();
    expect(result.current.error).not.toBeNull();
    expect(loadNativeInterventionDraft(scope)).toEqual(saved);
    expect(result.current.inputAction).toMatchObject({ status: "idle", original: null });
    expect(result.current.stopAction).toMatchObject({ status: "idle", original: null });
  });

  it.each(["missing Execution lease", "mismatched runtime lease", "missing runtime lease"])(
    "keeps Stop fenced for %s after observation progress",
    async (label) => {
      const initial = streamingFacts(7, 5);
      let current = initial;
      const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
      const observations = installSendAndStopHandlers({
        getNativeContext: () => current.nativeContext,
        getExecution: () => current.execution,
        getSession: () => current.session,
      });
      document.cookie = "sidera_csrf=csrf-native; Path=/";
      const { result } = renderHook(() => useNativeIntervention({
        ...hookOptions(async () => { client.setQueryData(workerWorkspaceQueryKey(WORKSPACE_ID, WORKER_ITEM_ID), current.workspace); }),
        workspace: initial.workspace,
        nativeContext: initial.nativeContext,
      }), { wrapper: queryProvider(client) });
      await waitFor(() => expect(result.current.canStop).toBe(true));
      current = streamingFacts();
      if (label === "missing Execution lease") current.execution.active_lease_id = null;
      else current.workspace.current_runtime.lease_id = label === "missing runtime lease" ? null : REPLACEMENT_LEASE_ID;

      await act(async () => { await result.current.requestStop(); });

      expect(observations.posted).toHaveLength(0);
      expect(result.current.stopAction).toMatchObject({ status: "idle", original: null });
    },
  );

  it.each(["Send", "Stop"] as const)("does not %s with revoked authority during streaming", async (intent) => {
    const current = streamingFacts(7, 5);
    const observations = installSendAndStopHandlers({
      getNativeContext: () => current.nativeContext,
      getSession: () => current.session,
    });
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    const { result } = renderHook(() => useNativeIntervention({
      ...hookOptions(),
      workspace: current.workspace,
      nativeContext: current.nativeContext,
    }), { wrapper: queryProvider() });
    await waitFor(() => expect(result.current.sessionAuthorized).toBe(true));
    act(() => result.current.onTextChange("must remain local after revocation"));
    await act(async () => { await result.current.reviewCurrentContext(); });
    server.use(http.get("/product/v1/session", () => new HttpResponse(null, { status: 401 })));
    const uuidSpy = vi.spyOn(crypto, "randomUUID");

    await act(async () => { await (intent === "Send" ? result.current.send() : result.current.requestStop()); });

    expect(observations.posted).toHaveLength(0);
    expect(uuidSpy).not.toHaveBeenCalled();
    expect(result.current.inputAction?.original).toBeNull();
    expect(result.current.stopAction?.original).toBeNull();
  });

  it.each([undefined, null])("does not promote native read proof %s from canonical last_sequence", async (proof) => {
    const oldContext: NativeWorkerContextView = {
      ...nativeContext,
      context: { ...context, session_version: 7, ...(proof === undefined ? {} : { session_last_sequence: proof }) },
    };
    let currentNativeContext = oldContext;
    let currentSession = streamingSession;
    const observations = installSendAndStopHandlers({
      getNativeContext: () => currentNativeContext,
      getSession: () => currentSession,
    });
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    const { result } = renderHook(() => useNativeIntervention({
      ...hookOptions(),
      nativeContext: oldContext,
      workspace: { ...workspace, current_runtime: { ...workspace.current_runtime!, session_version: 7 } },
    }), { wrapper: queryProvider() });
    await waitFor(() => expect(result.current.sessionAuthorized).toBe(true));
    act(() => result.current.onTextChange("keep proof-less review exact"));
    await act(async () => { await result.current.reviewCurrentContext(); });
    expect(result.current.canSend).toBe(true);
    const scope = { workspace_id: WORKSPACE_ID, principal_id: PRINCIPAL_ID, worker_item_id: WORKER_ITEM_ID };
    const saved = loadNativeInterventionDraft(scope);
    if (!saved.ok || saved.draft === null) throw new Error("old exact read must produce a valid review");
    expect(saved.draft.review?.context).toEqual({ ...context, session_version: 7 });
    expect(saved.draft.review?.context).not.toHaveProperty("session_last_sequence");
    currentNativeContext = {
      ...streamingNativeContext,
      context: { ...context, session_version: 10, session_last_sequence: 8 },
    };
    currentSession = { ...session, version: 10, last_sequence: 8 };
    const uuidSpy = vi.spyOn(crypto, "randomUUID");

    await act(async () => { await result.current.send(); });

    expect(observations.posted).toHaveLength(0);
    expect(uuidSpy).not.toHaveBeenCalled();
    expect(loadNativeInterventionDraft(scope)).toEqual(saved);
    expect(result.current.canSend).toBe(false);
  });

  it.each(["input disabled", "Run input closed"])("keeps Send gated for %s during observation progress", async (gate) => {
    let current = streamingFacts(7, 5);
    const initial = current;
    const observations = installSendAndStopHandlers({
      getNativeContext: () => current.nativeContext,
      getSession: () => current.session,
    });
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    const { result } = renderHook(() => useNativeIntervention({
      ...hookOptions(),
      workspace: initial.workspace,
      nativeContext: initial.nativeContext,
    }), { wrapper: queryProvider() });
    await waitFor(() => expect(result.current.sessionAuthorized).toBe(true));
    act(() => result.current.onTextChange("respect native input gates"));
    await act(async () => { await result.current.reviewCurrentContext(); });
    expect(result.current.canSend).toBe(true);
    current = streamingFacts();
    if (gate === "input disabled") current.nativeContext.input_enabled = false;
    else current.nativeContext.run.input_open = false;
    const uuidSpy = vi.spyOn(crypto, "randomUUID");

    await act(async () => { await result.current.send(); });

    expect(observations.posted).toHaveLength(0);
    expect(uuidSpy).not.toHaveBeenCalled();
    expect(result.current.inputAction).toMatchObject({ status: "idle", original: null });
  });

  it.each([
    { label: "old approval, proved current at equal version", approvalProof: undefined, currentProof: 5, version: 7, allowed: true },
    { label: "proved approval, old current at equal version", approvalProof: 5, currentProof: undefined, version: 7, allowed: true },
    { label: "old approval cannot advance", approvalProof: undefined, currentProof: 8, version: 10, allowed: false },
    { label: "old current cannot advance", approvalProof: 5, currentProof: undefined, version: 10, allowed: false },
  ])("uses exact-version fallback for $label", async ({ approvalProof, currentProof, version, allowed }) => {
    const draft = appliedStopDraft();
    draft.stop = draft.input;
    draft.text = "old and new proof boundaries";
    draft.review = {
      ...originalStopReviewA,
      context: { ...context, session_version: 7, ...(approvalProof === undefined ? {} : { session_last_sequence: approvalProof }) },
    };
    expect(saveNativeInterventionDraft(draft.scope, draft)).toEqual({ ok: true });
    const currentContext: NativeWorkerContextView = {
      ...nativeContext,
      context: { ...context, session_version: version, ...(currentProof === undefined ? {} : { session_last_sequence: currentProof }) },
    };
    const observations = installSendAndStopHandlers({
      getNativeContext: () => currentContext,
      getSession: () => ({ ...session, version, last_sequence: currentProof } as ExecutionSession),
    });
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    const { result } = renderHook(() => useNativeIntervention({
      ...hookOptions(),
      nativeContext: currentContext,
      workspace: { ...workspace, current_runtime: { ...workspace.current_runtime!, session_version: version } },
    }), { wrapper: queryProvider() });
    await waitFor(() => expect(result.current.nativeFactsAvailable).toBe(true));
    expect(result.current.canSend).toBe(allowed);

    await act(async () => { await result.current.send(); });

    expect(observations.posted).toHaveLength(allowed ? 1 : 0);
    const loaded = loadNativeInterventionDraft(draft.scope);
    expect(loaded).toMatchObject({ ok: true, draft: { review: draft.review } });
    if (allowed) {
      const originalContext = observations.posted[0]!.payload.context;
      expect(originalContext).toEqual(currentContext.context);
      expect(result.current.inputAction?.status).toBe("applied");
    }
  });

  it.each([
    { label: "old pending input and new pending Stop", proof: undefined, status: "pending" },
    { label: "old UNKNOWN input and new UNKNOWN Stop", proof: undefined, status: "unknown" },
    { label: "new pending input and old pending Stop", proof: 3, status: "pending" },
    { label: "new UNKNOWN input and old UNKNOWN Stop", proof: 3, status: "unknown" },
  ] as const)("restores $label and recovers immutable originals by GET only", async ({ proof, status }) => {
    const draft = appliedStopDraft(status);
    const originalContext = proof === undefined ? context : { ...context, session_last_sequence: proof };
    const originalReview = { ...originalStopReviewA, context: originalContext };
    const original: CommandRequest = {
      ...originalStopA,
      command_id: "f0000000-0000-0000-0000-000000000004",
      command_type: "SendExecutionInput",
      target_type: "execution_session",
      target_id: SESSION_ID,
      expected_version: 5,
      idempotency_key: "f0000000-0000-0000-0000-000000000005",
      correlation_id: "f0000000-0000-0000-0000-000000000006",
      payload: {
        context: originalContext,
        client_idempotency_key: "f0000000-0000-0000-0000-000000000007",
        text: "unchanged original body",
        attachment_refs: [],
      },
    };
    draft.text = "editable draft remains separate";
    draft.client_idempotency_key = String(original.payload.client_idempotency_key);
    draft.review = { ...originalReview, context: { ...context, session_version: 10, session_last_sequence: 8 } };
    draft.input = { ...draft.stop, original, review: originalReview };
    draft.stop.review = { ...originalStopReviewA, context: proof === undefined ? { ...context, session_last_sequence: 3 } : context };
    const originalJson = JSON.stringify(original);
    const inputReviewJson = JSON.stringify(draft.input.review);
    const stopReviewJson = JSON.stringify(draft.stop.review);
    const raw = JSON.stringify(draft);
    localStorage.setItem(nativeInterventionDraftKey(draft.scope), raw);
    let mismatch = true;
    const observations = installSendAndStopHandlers({
      getNativeContext: () => streamingNativeContext,
      getSession: () => streamingSession,
      commandStatus: (commandId) => {
        const savedOriginal = commandId === original.command_id ? original : originalStopA;
        return {
          command_id: commandId,
          command_type: savedOriginal.command_type,
          target_type: savedOriginal.target_type,
          target_id: savedOriginal.target_id,
          expected_version: (savedOriginal.expected_version ?? 0) + (mismatch ? 2 : 0),
          status: "applied",
          result: {
            command_id: commandId,
            aggregate_id: commandId === original.command_id ? INPUT_ID : EXECUTION_ID,
            aggregate_version: commandId === original.command_id ? 1 : 8,
            event_ids: [],
          },
        } satisfies CommandStatusResponse;
      },
    });
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    const { result } = renderHook(() => useNativeIntervention({
      ...hookOptions(),
      workspace: streamingWorkspace,
      nativeContext: streamingNativeContext,
    }), { wrapper: queryProvider() });
    await waitFor(() => expect(result.current.inputAction?.status).toBe(status));
    expect(result.current.storageError).toBeNull();
    expect(localStorage.getItem(nativeInterventionDraftKey(draft.scope))).toBe(raw);
    expect(observations.posted).toHaveLength(0);
    const uuidSpy = vi.spyOn(crypto, "randomUUID");
    await act(async () => { await result.current.reviewCurrentContext(); });
    await act(async () => { await result.current.send(); await result.current.requestStop(); });
    expect(result.current.canSend).toBe(false);
    expect(result.current.canStop).toBe(false);

    await act(async () => { await result.current.lookupOriginalInput(); await result.current.lookupOriginalStop(); });
    expect(result.current.inputAction?.status).toBe(status);
    expect(result.current.stopAction?.status).toBe(status);
    mismatch = false;
    await act(async () => { await result.current.lookupOriginalInput(); await result.current.lookupOriginalStop(); });

    expect(observations.posted).toHaveLength(0);
    expect(uuidSpy).not.toHaveBeenCalled();
    expect(observations.commandGets).toEqual([original.command_id, originalStopA.command_id, original.command_id, originalStopA.command_id]);
    expect(result.current.inputAction).toMatchObject({ status: "applied", original });
    expect(result.current.stopAction).toMatchObject({ status: "applied", original: originalStopA, aggregate_version: 8 });
    const loaded = loadNativeInterventionDraft(draft.scope);
    if (!loaded.ok || loaded.draft === null) throw new Error("query recovery must preserve a valid draft");
    expect(JSON.stringify(loaded.draft.input.original)).toBe(originalJson);
    expect(JSON.stringify(loaded.draft.input.review)).toBe(inputReviewJson);
    expect(JSON.stringify(loaded.draft.stop.review)).toBe(stopReviewJson);
    expect(loaded.draft.review?.context).toEqual({ ...context, session_version: 7, session_last_sequence: 5 });
    expect(loaded.draft.text).toBe("editable draft remains separate");
    expect(Object.keys(loaded.draft.input.original!.payload.context as object)).toHaveLength(proof === undefined ? 10 : 11);
  });

  it("suppresses same-tick duplicate Send calls before either preflight yields", async () => {
    const observations = installSendAndStopHandlers();
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    const { result } = renderHook(() => useNativeIntervention(hookOptions()), { wrapper: queryProvider() });

    await waitFor(() => expect(result.current.sessionAuthorized).toBe(true));
    act(() => result.current.onTextChange("one same-tick input"));
    await act(async () => { await result.current.reviewCurrentContext(); });
    await act(async () => {
      await Promise.all([result.current.send(), result.current.send()]);
    });

    expect(observations.posted).toHaveLength(1);
    expect(observations.draftsAtPost).toHaveLength(1);
    expect(observations.draftsAtPost[0]).toMatchObject({ input: { status: "pending" } });
  });

  it("normalizes only the submitted original and reloads the untouched editable whitespace without replay", async () => {
    const observations = installSendAndStopHandlers();
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    const originalText = "  keep  interior \n";
    const first = renderHook(() => useNativeIntervention(hookOptions()), { wrapper: queryProvider() });

    await waitFor(() => expect(first.result.current.sessionAuthorized).toBe(true));
    act(() => first.result.current.onTextChange(originalText));
    await act(async () => { await first.result.current.reviewCurrentContext(); });
    await act(async () => { await first.result.current.send(); });

    expect(observations.posted).toHaveLength(1);
    expect(observations.posted[0]).toMatchObject({ payload: { text: "keep  interior" } });
    expect(first.result.current.text).toBe(originalText);
    const initialDraft = loadNativeInterventionDraft({
      workspace_id: WORKSPACE_ID,
      principal_id: PRINCIPAL_ID,
      worker_item_id: WORKER_ITEM_ID,
    });
    expect(initialDraft).toMatchObject({
      ok: true,
      draft: {
        text: originalText,
        input: { status: "applied", original: { payload: { text: "keep  interior" } } },
      },
    });
    const original = observations.posted[0];
    first.unmount();

    const reloaded = renderHook(() => useNativeIntervention(hookOptions()), { wrapper: queryProvider() });
    await waitFor(() => expect(reloaded.result.current.inputAction?.original).toEqual(original));
    expect(reloaded.result.current.text).toBe(originalText);
    expect(reloaded.result.current.inputAction).toMatchObject({
      status: "applied",
      original: { payload: { text: "keep  interior" } },
    });
    expect(observations.posted).toHaveLength(1);
    reloaded.unmount();
  });

  it.each(["Send", "Stop"] as const)("rejects an expired current-user session at %s preflight without allocating an original", async (intent) => {
    let currentSession = localSession;
    const observations = installSendAndStopHandlers({ getLocalSession: () => currentSession });
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    const { result } = renderHook(() => useNativeIntervention(hookOptions()), { wrapper: queryProvider() });

    await waitFor(() => {
      expect(result.current.sessionAuthorized).toBe(true);
      expect(result.current.nativeFactsAvailable).toBe(true);
    });
    if (intent === "Send") {
      act(() => result.current.onTextChange("do not submit after expiry"));
      await act(async () => { await result.current.reviewCurrentContext(); });
      expect(result.current.canSend).toBe(true);
    } else {
      expect(result.current.canStop).toBe(true);
    }
    const scope = { workspace_id: WORKSPACE_ID, principal_id: PRINCIPAL_ID, worker_item_id: WORKER_ITEM_ID };
    const persistedBefore = loadNativeInterventionDraft(scope);
    const contextReadsBefore = observations.nativeContextReads();
    currentSession = { ...localSession, expires_at: "2000-01-01T00:00:00Z" };
    expect(document.cookie).toContain("sidera_csrf=csrf-native");
    const uuidSpy = vi.spyOn(globalThis.crypto, "randomUUID");

    if (intent === "Send") {
      await act(async () => { await result.current.send(); });
    } else {
      await act(async () => { await result.current.requestStop(); });
    }

    expect(observations.posted).toHaveLength(0);
    expect(observations.nativeContextReads()).toBe(contextReadsBefore);
    expect(observations.csrfHeaders).toEqual([]);
    expect(uuidSpy).not.toHaveBeenCalled();
    expect(loadNativeInterventionDraft(scope)).toEqual(persistedBefore);
    expect(result.current.inputAction).toMatchObject({ status: "idle", original: null });
    expect(result.current.stopAction).toMatchObject({ status: "idle", original: null });
  });

  it("allows one fresh Send to replacement B while retaining applied original Stop A", async () => {
    const originalDraft = saveStopDraft();
    const observations = installSendAndStopHandlers({
      getNativeContext: () => replacementNativeContext,
      getExecution: (executionId) => executionId === REPLACEMENT_EXECUTION_ID ? replacementExecution : undefined,
      getSession: (sessionId) => sessionId === REPLACEMENT_SESSION_ID ? replacementSession : undefined,
    });
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    const { result } = renderHook(() => useNativeIntervention({
      ...hookOptions(),
      workspace: replacementWorkspace,
      execution: replacementExecution,
      nativeContext: replacementNativeContext,
    }), { wrapper: queryProvider() });

    await waitFor(() => {
      expect(result.current.sessionAuthorized).toBe(true);
      expect(result.current.stopAction?.status).toBe("applied");
      expect(result.current.nativeFactsAvailable).toBe(true);
    });
    expect(result.current.canSend).toBe(false);
    act(() => result.current.onTextChange("Send only after reviewing replacement B"));
    await act(async () => { await result.current.reviewCurrentContext(); });
    expect(result.current.canSend).toBe(true);
    await act(async () => { await result.current.send(); });

    expect(observations.posted).toHaveLength(1);
    expect(observations.posted[0]).toMatchObject({
      command_type: "SendExecutionInput",
      actor: { type: "user", id: PRINCIPAL_ID },
      target_type: "execution_session",
      target_id: REPLACEMENT_SESSION_ID,
      expected_version: 8,
      payload: { context: replacementContext, text: "Send only after reviewing replacement B" },
    });
    expect(observations.posted[0]?.payload.client_idempotency_key).toMatch(/^[0-9a-f-]{36}$/i);
    expect(result.current.stopAction).toEqual(originalDraft.stop);
    expect(loadNativeInterventionDraft(originalDraft.scope)).toMatchObject({
      ok: true,
      draft: { stop: originalDraft.stop, input: { original: observations.posted[0], status: "applied" } },
    });
  });

  it("replaces applied Stop A only after an explicit Stop of coherent replacement B", async () => {
    const originalDraft = saveStopDraft();
    const observations = installSendAndStopHandlers({
      getNativeContext: () => replacementNativeContext,
      getExecution: (executionId) => executionId === REPLACEMENT_EXECUTION_ID ? replacementExecution : undefined,
      getSession: (sessionId) => sessionId === REPLACEMENT_SESSION_ID ? replacementSession : undefined,
    });
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    const { result } = renderHook(() => useNativeIntervention({
      ...hookOptions(),
      workspace: replacementWorkspace,
      execution: replacementExecution,
      nativeContext: replacementNativeContext,
    }), { wrapper: queryProvider() });

    await waitFor(() => {
      expect(result.current.stopAction?.status).toBe("applied");
      expect(result.current.nativeFactsAvailable).toBe(true);
    });
    expect(result.current.canStop).toBe(true);
    expect(result.current.stopAction?.original).toEqual(originalStopA);
    await act(async () => { await result.current.requestStop(); });

    expect(observations.posted).toHaveLength(1);
    expect(observations.posted[0]).toMatchObject({
      command_type: "StopExecution",
      actor: { type: "user", id: PRINCIPAL_ID },
      target_type: "execution",
      target_id: REPLACEMENT_EXECUTION_ID,
      expected_version: 8,
      payload: {},
    });
    expect(result.current.stopAction).toMatchObject({
      status: "applied",
      original: observations.posted[0],
      review: { context: replacementContext, execution_version: 8 },
    });
    expect(result.current.stopAction?.original?.command_id).not.toBe(originalStopA.command_id);
    expect(loadNativeInterventionDraft(originalDraft.scope)).toMatchObject({
      ok: true,
      draft: { stop: { status: "applied", original: observations.posted[0], review: { context: replacementContext } } },
    });
  });

  it.each([
    { label: "same Execution A", status: "applied" as const, useReplacement: false },
    { label: "pending A on replacement B", status: "pending" as const, useReplacement: true },
    { label: "unknown A on replacement B", status: "unknown" as const, useReplacement: true },
  ])("blocks Send and Stop for $label", async ({ status, useReplacement }) => {
    saveStopDraft(status);
    const currentContext = useReplacement ? replacementNativeContext : nativeContext;
    const currentWorkspace = useReplacement ? replacementWorkspace : workspace;
    const currentExecution = useReplacement ? replacementExecution : execution;
    const currentSession = useReplacement ? replacementSession : session;
    const currentExecutionId = useReplacement ? REPLACEMENT_EXECUTION_ID : EXECUTION_ID;
    const currentSessionId = useReplacement ? REPLACEMENT_SESSION_ID : SESSION_ID;
    const observations = installSendAndStopHandlers({
      getNativeContext: () => currentContext,
      getExecution: (executionId) => executionId === currentExecutionId ? currentExecution : undefined,
      getSession: (sessionId) => sessionId === currentSessionId ? currentSession : undefined,
    });
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    const { result } = renderHook(() => useNativeIntervention({
      ...hookOptions(),
      workspace: currentWorkspace,
      execution: currentExecution,
      nativeContext: currentContext,
    }), { wrapper: queryProvider() });

    await waitFor(() => expect(result.current.stopAction?.status).toBe(status));
    act(() => result.current.onTextChange("must remain blocked"));
    await act(async () => { await result.current.reviewCurrentContext(); });
    expect(result.current.canSend).toBe(false);
    expect(result.current.canStop).toBe(false);
    const uuidSpy = vi.spyOn(globalThis.crypto, "randomUUID");
    await act(async () => { await result.current.send(); });
    await act(async () => { await result.current.requestStop(); });

    expect(observations.posted).toHaveLength(0);
    expect(uuidSpy).not.toHaveBeenCalled();
    expect(result.current.stopAction?.original).toEqual(originalStopA);
    uuidSpy.mockRestore();
  });

  it("keeps a valid canonical history selection passive after applied Stop A when current B is coherent", async () => {
    const originalDraft = saveStopDraft();
    const observations = installSendAndStopHandlers({
      getNativeContext: () => replacementNativeContext,
      getExecution: (executionId) => executionId === REPLACEMENT_EXECUTION_ID ? replacementExecution : undefined,
      getSession: (sessionId) => sessionId === REPLACEMENT_SESSION_ID ? replacementSession : undefined,
    });
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    const { result } = renderHook(() => useNativeIntervention({
      ...hookOptions(),
      workspace: replacementWorkspace,
      execution: replacementExecution,
      nativeContext: replacementNativeContext,
    }), { wrapper: queryProvider() });

    await waitFor(() => {
      expect(result.current.stopAction?.status).toBe("applied");
      expect(result.current.nativeFactsAvailable).toBe(true);
    });
    expect(result.current.canLoadHistoricalInput).toBe(true);
    act(() => result.current.explicitResendSuperseded("historical WorkerActivity body"));

    expect(result.current.text).toBe("historical WorkerActivity body");
    expect(result.current.canSend).toBe(false);
    expect(result.current.inputAction).toMatchObject({ status: "idle", original: null });
    expect(result.current.stopAction).toEqual(originalDraft.stop);
    expect(loadNativeInterventionDraft(originalDraft.scope)).toMatchObject({
      ok: true,
      draft: { text: "historical WorkerActivity body", review: null, input: { original: null }, stop: originalDraft.stop },
    });
    expect(observations.posted).toHaveLength(0);
  });

  it("does not allow historical selection when replacement B is incoherent", async () => {
    const draft = saveStopDraft();
    const incoherentContext = { ...replacementNativeContext, context: null } as NativeWorkerContextView;
    const observations = installSendAndStopHandlers({ getNativeContext: () => incoherentContext });
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    const { result } = renderHook(() => useNativeIntervention({
      ...hookOptions(),
      workspace: replacementWorkspace,
      execution: replacementExecution,
      nativeContext: incoherentContext,
    }), { wrapper: queryProvider() });

    await waitFor(() => expect(result.current.stopAction?.status).toBe("applied"));
    expect(result.current.nativeFactsAvailable).toBe(false);
    expect(result.current.canLoadHistoricalInput).toBe(false);
    act(() => result.current.explicitResendSuperseded("must remain passive without coherent B"));
    expect(result.current.text).toBe("");
    expect(result.current.stopAction?.original).toEqual(draft.stop.original);
    expect(observations.posted).toHaveLength(0);
  });

  it("does not bypass hydration validation for a Stop target mismatched with its saved review", async () => {
    const draft = saveStopDraft();
    const raw = localStorage.getItem(nativeInterventionDraftKey(draft.scope));
    if (raw === null) throw new Error("the original Stop fixture must be persisted");
    const invalidDraft: NativeInterventionDraft = JSON.parse(raw);
    if (invalidDraft.stop.original === null) throw new Error("the original Stop fixture must be present");
    invalidDraft.stop.original.target_id = REPLACEMENT_EXECUTION_ID;
    localStorage.setItem(nativeInterventionDraftKey(draft.scope), JSON.stringify(invalidDraft));

    const observations = installSendAndStopHandlers({
      getNativeContext: () => replacementNativeContext,
      getExecution: (executionId) => executionId === REPLACEMENT_EXECUTION_ID ? replacementExecution : undefined,
      getSession: (sessionId) => sessionId === REPLACEMENT_SESSION_ID ? replacementSession : undefined,
    });
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    const { result } = renderHook(() => useNativeIntervention({
      ...hookOptions(),
      workspace: replacementWorkspace,
      execution: replacementExecution,
      nativeContext: replacementNativeContext,
    }), { wrapper: queryProvider() });

    await waitFor(() => expect(result.current.storageError).not.toBeNull());
    expect(result.current.canLoadHistoricalInput).toBe(false);
    expect(result.current.inputAction).toBeNull();
    expect(result.current.stopAction).toBeNull();
    act(() => result.current.explicitResendSuperseded("must not bypass a corrupt saved link"));
    expect(result.current.text).toBe("");
    expect(observations.posted).toHaveLength(0);
    expect(loadNativeInterventionDraft(draft.scope).ok).toBe(false);
  });

  it("stops from fresh canonical facts with blank text and no editable review", async () => {
    const observations = installSendAndStopHandlers();
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    const { result } = renderHook(() => useNativeIntervention(hookOptions()), { wrapper: queryProvider() });

    await waitFor(() => {
      expect(result.current.sessionAuthorized).toBe(true);
      expect(result.current.inputAction?.status).toBe("idle");
    });
    expect(result.current.text).toBe("");
    expect(result.current.canStop).toBe(true);
    const readsBeforeStop = observations.nativeContextReads();

    await act(async () => { await result.current.requestStop(); });

    expect(observations.nativeContextReads()).toBeGreaterThan(readsBeforeStop);
    expect(observations.posted).toHaveLength(1);
    expect(observations.posted[0]).toMatchObject({
      command_type: "StopExecution",
      actor: { type: "user", id: PRINCIPAL_ID },
      target_type: "execution",
      target_id: EXECUTION_ID,
      expected_version: 7,
      payload: {},
    });
    expect(observations.draftsAtPost[0]).toMatchObject({
      input: { status: "idle", original: null },
      stop: { status: "pending", original: observations.posted[0], review: { context, execution_version: 7 } },
    });
  });

  it("stops against the clicked canonical source when the editable input review is stale", async () => {
    const replacementRunId = "80000000-0000-0000-0000-000000000002";
    const replacementContext: NativeWorkerContextView = {
      ...nativeContext,
      context: { ...context, run_id: replacementRunId },
      execution: { ...executionPin, run_id: replacementRunId },
    };
    let currentNativeContext = nativeContext;
    const observations = installSendAndStopHandlers({ getNativeContext: () => currentNativeContext });
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    const { result, rerender } = renderHook(
      ({ currentContext }: { currentContext: NativeWorkerContextView }) => useNativeIntervention({
        ...hookOptions(),
        nativeContext: currentContext,
      }),
      { initialProps: { currentContext: nativeContext }, wrapper: queryProvider() },
    );

    await waitFor(() => {
      expect(result.current.sessionAuthorized).toBe(true);
      expect(result.current.inputAction?.status).toBe("idle");
    });
    await act(async () => { await result.current.reviewCurrentContext(); });
    act(() => result.current.onTextChange("editable input stays review-gated"));
    currentNativeContext = replacementContext;
    rerender({ currentContext: replacementContext });

    expect(result.current.canSend).toBe(false);
    expect(result.current.canStop).toBe(true);
    await act(async () => { await result.current.requestStop(); });

    expect(observations.posted).toHaveLength(1);
    expect(observations.posted[0]).toMatchObject({ command_type: "StopExecution", expected_version: 7 });
    expect(observations.draftsAtPost[0]).toMatchObject({
      text: "editable input stays review-gated",
      review: { context: { run_id: RUN_ID } },
      stop: { status: "pending", review: { context: { run_id: replacementRunId } } },
    });
  });

  it("does not stop a replacement source discovered during Stop preflight", async () => {
    const releaseContext = deferred();
    const replacementRunId = "80000000-0000-0000-0000-000000000002";
    let currentNativeContext = nativeContext;
    let holdContext = true;
    const observations = installSendAndStopHandlers({
      getNativeContext: () => currentNativeContext,
      beforeNativeContextResponse: async () => {
        if (!holdContext) return;
        holdContext = false;
        await releaseContext.promise;
      },
    });
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    const { result } = renderHook(() => useNativeIntervention(hookOptions()), { wrapper: queryProvider() });

    await waitFor(() => {
      expect(result.current.sessionAuthorized).toBe(true);
      expect(result.current.inputAction?.status).toBe("idle");
    });
    const uuidSpy = vi.spyOn(globalThis.crypto, "randomUUID");
    let stopPromise!: Promise<void>;
    await act(async () => { stopPromise = result.current.requestStop(); });
    await waitFor(() => expect(observations.nativeContextReads()).toBeGreaterThan(0));
    currentNativeContext = {
      ...nativeContext,
      context: { ...context, run_id: replacementRunId },
      execution: { ...executionPin, run_id: replacementRunId },
    };
    releaseContext.resolve();
    await stopPromise;

    expect(observations.nativeContextReads()).toBeGreaterThan(0);
    expect(observations.posted).toHaveLength(0);
    expect(result.current.stopAction).toMatchObject({ status: "idle", original: null });
    expect(uuidSpy).not.toHaveBeenCalled();
    uuidSpy.mockRestore();
  });

  it("loads a selected historical body from a fresh idle draft without creating an original", async () => {
    const observations = installSendAndStopHandlers();
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    const { result } = renderHook(() => useNativeIntervention(hookOptions()), { wrapper: queryProvider() });

    await waitFor(() => {
      expect(result.current.sessionAuthorized).toBe(true);
      expect(result.current.inputAction?.status).toBe("idle");
    });
    const uuidSpy = vi.spyOn(globalThis.crypto, "randomUUID");

    act(() => result.current.explicitResendSuperseded("historical WorkerActivity body"));

    expect(result.current.text).toBe("historical WorkerActivity body");
    expect(result.current.canSend).toBe(false);
    expect(result.current.inputAction).toMatchObject({ status: "idle", original: null });
    expect(result.current.inputObservation).toBeNull();
    expect(observations.posted).toHaveLength(0);
    expect(uuidSpy).not.toHaveBeenCalled();
    expect(loadNativeInterventionDraft({
      workspace_id: WORKSPACE_ID,
      principal_id: PRINCIPAL_ID,
      worker_item_id: WORKER_ITEM_ID,
    })).toMatchObject({
      ok: true,
      draft: { text: "historical WorkerActivity body", client_idempotency_key: null, review: null },
    });

    await act(async () => { await result.current.reviewCurrentContext(); });
    await act(async () => { await result.current.send(); });

    expect(observations.posted).toHaveLength(1);
    expect(observations.posted[0]).toMatchObject({
      command_type: "SendExecutionInput",
      target_id: SESSION_ID,
      expected_version: 5,
      payload: { context, text: "historical WorkerActivity body" },
    });
    expect(uuidSpy.mock.calls.length).toBeGreaterThan(0);
    uuidSpy.mockRestore();
  });

  it("preserves terminal input review and editable text while independently stopping", async () => {
    const observations = installSendAndStopHandlers({
      inputPage: (_page, posted) => ({
        items: posted[0] === undefined ? [] : [observedInput(posted[0], "delivered")],
        next_cursor: null,
      }),
    });
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    const { result } = renderHook(() => useNativeIntervention(hookOptions()), { wrapper: queryProvider() });

    await waitFor(() => expect(result.current.sessionAuthorized).toBe(true));
    await act(async () => { await result.current.reviewCurrentContext(); });
    act(() => result.current.onTextChange("terminal input text"));
    await act(async () => { await result.current.send(); });
    const inputOriginal = result.current.inputAction?.original;
    const inputReview = result.current.inputAction?.review;
    await act(async () => { await result.current.refreshInputStatus(); });

    expect(result.current.inputObservation?.status).toBe("delivered");
    expect(result.current.canStop).toBe(true);
    expect(loadNativeInterventionDraft({
      workspace_id: WORKSPACE_ID,
      principal_id: PRINCIPAL_ID,
      worker_item_id: WORKER_ITEM_ID,
    })).toMatchObject({
      ok: true,
      draft: {
        text: "terminal input text",
        review: null,
        input: { status: "applied", original: inputOriginal, review: inputReview },
      },
    });
    const readsBeforeStop = observations.nativeContextReads();

    await act(async () => { await result.current.requestStop(); });

    expect(observations.nativeContextReads()).toBeGreaterThan(readsBeforeStop);
    expect(observations.posted).toHaveLength(2);
    expect(observations.posted[1]).toMatchObject({ command_type: "StopExecution", expected_version: 7 });
    expect(observations.draftsAtPost[1]).toMatchObject({
      text: "terminal input text",
      review: null,
      input: { status: "applied", original: inputOriginal, review: inputReview },
      stop: { status: "pending", review: { context, execution_version: 7 } },
    });
  });

  it("does not replace a pending input original during historical selection", async () => {
    const postStarted = deferred();
    const releasePost = deferred();
    const observations = installSendAndStopHandlers({
      beforeCommandResponse: async (command) => {
        if (command.command_type !== "SendExecutionInput") return;
        postStarted.resolve();
        await releasePost.promise;
      },
    });
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    const { result } = renderHook(() => useNativeIntervention(hookOptions()), { wrapper: queryProvider() });

    await waitFor(() => expect(result.current.sessionAuthorized).toBe(true));
    await act(async () => { await result.current.reviewCurrentContext(); });
    act(() => result.current.onTextChange("pending input text"));
    let sendPromise!: Promise<void>;
    await act(async () => {
      sendPromise = result.current.send();
      await postStarted.promise;
    });
    const inputAction = result.current.inputAction;
    expect(inputAction?.status).toBe("pending");

    act(() => result.current.explicitResendSuperseded("historical body"));

    expect(result.current.text).toBe("pending input text");
    expect(result.current.inputAction).toEqual(inputAction);
    expect(result.current.inputAction?.original).toEqual(observations.posted[0]);
    expect(observations.posted).toHaveLength(1);
    releasePost.resolve();
    await act(async () => { await sendPromise; });
  });

  it("does not replace historical text while Stop is pending or applied", async () => {
    const stopPostStarted = deferred();
    const releaseStopPost = deferred();
    const observations = installSendAndStopHandlers({
      beforeCommandResponse: async (command) => {
        if (command.command_type !== "StopExecution") return;
        stopPostStarted.resolve();
        await releaseStopPost.promise;
      },
    });
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    const { result } = renderHook(() => useNativeIntervention(hookOptions()), { wrapper: queryProvider() });

    await waitFor(() => {
      expect(result.current.sessionAuthorized).toBe(true);
      expect(result.current.inputAction?.status).toBe("idle");
    });
    let stopPromise!: Promise<void>;
    await act(async () => {
      stopPromise = result.current.requestStop();
      await stopPostStarted.promise;
    });
    const pendingStop = result.current.stopAction;
    expect(pendingStop?.status).toBe("pending");

    act(() => result.current.explicitResendSuperseded("historical body"));

    expect(result.current.text).toBe("");
    expect(result.current.stopAction).toEqual(pendingStop);
    expect(observations.posted).toHaveLength(1);
    releaseStopPost.resolve();
    await act(async () => { await stopPromise; });
    expect(result.current.stopAction?.status).toBe("applied");

    act(() => result.current.explicitResendSuperseded("historical body"));

    expect(result.current.text).toBe("");
    expect(result.current.stopAction?.status).toBe("applied");
    expect(observations.posted).toHaveLength(1);
  });

  it.each([
    { name: "ongoing", reportedStatus: "queued", status: "queued", unavailable: false },
    { name: "unmapped", reportedStatus: "not_a_native_status", status: null, unavailable: true },
  ])("keeps historical selection disabled for $name input observation", async ({ reportedStatus, status, unavailable }) => {
    const observations = installSendAndStopHandlers({
      inputPage: (_page, posted) => ({
        items: posted[0] === undefined ? [] : [{ ...observedInput(posted[0], "queued"), status: reportedStatus }],
        next_cursor: null,
      }),
    });
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    const { result } = renderHook(() => useNativeIntervention(hookOptions()), { wrapper: queryProvider() });

    await waitFor(() => expect(result.current.sessionAuthorized).toBe(true));
    await act(async () => { await result.current.reviewCurrentContext(); });
    act(() => result.current.onTextChange("original input"));
    await act(async () => { await result.current.send(); });
    await act(async () => { await result.current.refreshInputStatus(); });
    const original = result.current.inputAction?.original;
    const inputAction = result.current.inputAction;
    expect(result.current.inputAction?.status).toBe("applied");
    expect(result.current.inputObservation).toMatchObject({ status, unavailable });

    act(() => result.current.explicitResendSuperseded("historical body"));

    expect(result.current.text).toBe("original input");
    expect(result.current.inputAction).toEqual(inputAction);
    expect(result.current.inputAction?.original).toEqual(original);
    expect(result.current.inputObservation).toMatchObject({ status, unavailable });
    expect(observations.posted).toHaveLength(1);
  });

  it("freezes exact native input and Stop commands before using the shared writer", async () => {
    const observations = installSendAndStopHandlers();
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    const { result } = renderHook(() => useNativeIntervention(hookOptions()), { wrapper: queryProvider() });

    await waitFor(() => expect(result.current.sessionAuthorized).toBe(true));
    await act(async () => {
      await result.current.reviewCurrentContext();
    });
    act(() => result.current.onTextChange("inspect the current state"));
    await act(async () => {
      await result.current.send();
    });
    await act(async () => {
      await result.current.requestStop();
    });

    expect(observations.posted).toHaveLength(2);
    expect(observations.posted[0]).toMatchObject({
      command_type: "SendExecutionInput",
      actor: { type: "user", id: PRINCIPAL_ID },
      target_type: "execution_session",
      target_id: SESSION_ID,
      expected_version: 5,
      payload: {
        context,
        text: "inspect the current state",
        attachment_refs: [],
      },
    });
    expect(observations.posted[0]?.payload).toHaveProperty("client_idempotency_key");
    expect(observations.posted[1]).toMatchObject({
      command_type: "StopExecution",
      actor: { type: "user", id: PRINCIPAL_ID },
      target_type: "execution",
      target_id: EXECUTION_ID,
      expected_version: 7,
      payload: {},
    });
    expect(Object.keys(observations.posted[0]?.payload.context ?? {}).sort()).toEqual([
      "binding_version",
      "context_version",
      "execution_id",
      "execution_mode",
      "plan_generation",
      "run_id",
      "session_id",
      "session_version",
      "worker_id",
      "worker_item_id",
    ]);
    expect(observations.draftsAtPost).toHaveLength(2);
    expect(observations.draftsAtPost[0]).toMatchObject({
      input: { status: "pending", original: observations.posted[0] },
    });
    expect(observations.draftsAtPost[1]).toMatchObject({
      input: { status: "applied" },
      stop: { status: "pending", original: observations.posted[1] },
    });
    expect(observations.csrfHeaders).toEqual(["csrf-native", "csrf-native"]);
    expect(loadNativeInterventionDraft({
      workspace_id: WORKSPACE_ID,
      principal_id: PRINCIPAL_ID,
      worker_item_id: WORKER_ITEM_ID,
    }).ok).toBe(true);
  });

  it("reads one actual cursor page per click and refreshes the same original from page one", async () => {
    const observations = installSendAndStopHandlers({
      inputPage: (page, posted) => {
        const original = posted[0];
        if (page === 1) return { items: [], next_cursor: "cursor:opaque:2" };
        if (page === 2 && original !== undefined) {
          return { items: [observedInput(original, "queued")], next_cursor: null };
        }
        if (page === 3 && original !== undefined) {
          return { items: [observedInput(original, "delivered")], next_cursor: null };
        }
        return { items: [], next_cursor: null };
      },
    });
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    const { result } = renderHook(() => useNativeIntervention(hookOptions()), { wrapper: queryProvider() });

    await waitFor(() => expect(result.current.sessionAuthorized).toBe(true));
    await act(async () => { await result.current.reviewCurrentContext(); });
    act(() => result.current.onTextChange("inspect the current state"));
    await act(async () => { await result.current.send(); });
    await act(async () => { await result.current.refreshInputStatus(); });
    expect(result.current.inputObservation).toMatchObject({ status: null, inputId: null, canReadMore: true });
    expect(observations.inputCursors).toEqual([null]);

    await act(async () => { await result.current.readMoreInputStatus(); });
    expect(result.current.inputObservation).toMatchObject({ status: "queued", inputId: INPUT_ID });
    expect(observations.inputCursors).toEqual([null, "cursor:opaque:2"]);

    await act(async () => { await result.current.refreshInputStatus(); });
    expect(result.current.inputObservation).toMatchObject({ status: "delivered", inputId: INPUT_ID });
    expect(observations.inputCursors).toEqual([null, "cursor:opaque:2", null]);
    expect(observations.posted).toHaveLength(1);
  });

  it("keeps an uncorrelated input UNKNOWN, allows independent Stop, and looks up the same original", async () => {
    const observations = installSendAndStopHandlers({
      commandResponse: (command) => ({
        command_id: command.command_type === "SendExecutionInput" ? RUN_ID : command.command_id,
        aggregate_id: command.command_type === "SendExecutionInput" ? INPUT_ID : EXECUTION_ID,
        aggregate_version: command.command_type === "SendExecutionInput" ? 1 : 8,
        event_ids: [],
      }),
      commandStatus: (commandId, posted) => {
        const original = posted.find((command) => command.command_id === commandId);
        return {
          command_id: commandId,
          command_type: original?.command_type ?? null,
          target_type: original?.target_type ?? null,
          target_id: original?.target_id ?? null,
          expected_version: original?.expected_version ?? null,
          status: "applied",
          result: {
            command_id: commandId,
            aggregate_id: INPUT_ID,
            aggregate_version: 1,
            event_ids: [],
          },
        } satisfies CommandStatusResponse;
      },
      inputPage: (_page, posted) => ({
        items: posted[0] === undefined ? [] : [observedInput(posted[0], "delivered")],
        next_cursor: null,
      }),
    });
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    const { result } = renderHook(() => useNativeIntervention(hookOptions()), { wrapper: queryProvider() });

    await waitFor(() => expect(result.current.sessionAuthorized).toBe(true));
    await act(async () => { await result.current.reviewCurrentContext(); });
    act(() => result.current.onTextChange("inspect the current state"));
    await act(async () => { await result.current.send(); });
    expect(result.current.inputAction?.status).toBe("unknown");

    await act(async () => { await result.current.refreshInputStatus(); });
    expect(result.current.inputObservation?.status).toBe("delivered");
    expect(result.current.inputAction?.status).toBe("unknown");

    const unknownOriginal = result.current.inputAction?.original;
    const unknownObservation = result.current.inputObservation;
    act(() => result.current.explicitResendSuperseded("must not replace unknown original"));

    expect(result.current.text).toBe("inspect the current state");
    expect(result.current.inputAction?.status).toBe("unknown");
    expect(result.current.inputAction?.original).toEqual(unknownOriginal);
    expect(result.current.inputObservation).toEqual(unknownObservation);
    expect(observations.posted).toHaveLength(1);

    await act(async () => { await result.current.requestStop(); });
    const originalInputId = observations.posted[0]?.command_id;
    await act(async () => { await result.current.lookupOriginalInput(); });

    expect(result.current.inputAction?.status).toBe("applied");
    expect(observations.posted).toHaveLength(2);
    expect(observations.posted[1]).toMatchObject({ command_type: "StopExecution", expected_version: 7 });
    expect(observations.commandGets).toEqual([originalInputId]);
  });

  it("does not post when the pending original cannot be persisted", async () => {
    const observations = installSendAndStopHandlers();
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    const setItem = Storage.prototype.setItem;
    const setItemSpy = vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (this: Storage, key: string, value: string) {
      if (key.startsWith("sidera.native-intervention.v1:")
        && JSON.parse(value).input.status === "pending") {
        throw new Error("storage blocked");
      }
      setItem.call(this, key, value);
    });
    const { result } = renderHook(() => useNativeIntervention(hookOptions()), { wrapper: queryProvider() });

    await waitFor(() => expect(result.current.sessionAuthorized).toBe(true));
    await act(async () => { await result.current.reviewCurrentContext(); });
    act(() => result.current.onTextChange("inspect the current state"));
    await act(async () => { await result.current.send(); });

    expect(observations.posted).toHaveLength(0);
    expect(result.current.storageError).not.toBeNull();
    expect(loadNativeInterventionDraft({
      workspace_id: WORKSPACE_ID,
      principal_id: PRINCIPAL_ID,
      worker_item_id: WORKER_ITEM_ID,
    })).toMatchObject({ ok: true, draft: { input: { status: "idle", original: null } } });
    setItemSpy.mockRestore();
  });

  it("keeps an unsaved Send as editable state and rechecks changed source after storage recovery", async () => {
    let currentContext = nativeContext;
    const observations = installSendAndStopHandlers({ getNativeContext: () => currentContext });
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    const { result } = renderHook(() => useNativeIntervention(hookOptions()), { wrapper: queryProvider() });

    await waitFor(() => expect(result.current.sessionAuthorized).toBe(true));
    await act(async () => { await result.current.reviewCurrentContext(); });
    act(() => result.current.onTextChange("inspect the current state"));
    const setItem = Storage.prototype.setItem;
    let failInitialWrite = true;
    const setItemSpy = vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (this: Storage, key: string, value: string) {
      if (failInitialWrite && key.startsWith("sidera.native-intervention.v1:")
        && JSON.parse(value).input.status === "pending") {
        failInitialWrite = false;
        throw new Error("storage blocked");
      }
      setItem.call(this, key, value);
    });

    await act(async () => { await result.current.send(); });
    expect(observations.posted).toHaveLength(0);
    setItemSpy.mockRestore();
    act(() => result.current.retryDraftStorage());
    expect(result.current.text).toBe("inspect the current state");

    const readsBeforeChangedSource = observations.nativeContextReads();
    currentContext = {
      ...nativeContext,
      context: null,
      execution: null,
      execution_version: null,
      input_enabled: false,
      read_only_reason: "execution_not_started",
      run: null,
    };
    await act(async () => { await result.current.send(); });

    expect(observations.posted).toHaveLength(0);
    expect(observations.nativeContextReads()).toBeGreaterThan(readsBeforeChangedSource);
    expect(result.current.inputAction).toMatchObject({ status: "idle", original: null });
  });

  it("rechecks stable source and creates a Send only after explicit storage recovery", async () => {
    const observations = installSendAndStopHandlers();
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    const { result } = renderHook(() => useNativeIntervention(hookOptions()), { wrapper: queryProvider() });

    await waitFor(() => expect(result.current.sessionAuthorized).toBe(true));
    await act(async () => { await result.current.reviewCurrentContext(); });
    act(() => result.current.onTextChange("inspect the current state"));
    const setItem = Storage.prototype.setItem;
    let failInitialWrite = true;
    const setItemSpy = vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (this: Storage, key: string, value: string) {
      if (failInitialWrite && key.startsWith("sidera.native-intervention.v1:")
        && JSON.parse(value).input.status === "pending") {
        failInitialWrite = false;
        throw new Error("storage blocked");
      }
      setItem.call(this, key, value);
    });

    await act(async () => { await result.current.send(); });
    expect(observations.posted).toHaveLength(0);
    setItemSpy.mockRestore();
    act(() => result.current.retryDraftStorage());
    expect(result.current.inputAction).toMatchObject({ status: "idle", original: null });
    const readsBeforeExplicitSend = observations.nativeContextReads();

    await act(async () => { await result.current.send(); });

    expect(observations.nativeContextReads()).toBeGreaterThan(readsBeforeExplicitSend);
    expect(observations.posted).toHaveLength(1);
    expect(observations.posted[0]).toMatchObject({ command_type: "SendExecutionInput", expected_version: 5 });
  });

  it("restores only the safe draft after an unsaved Stop and preserves a prior input original", async () => {
    const observations = installSendAndStopHandlers();
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    const { result } = renderHook(() => useNativeIntervention(hookOptions()), { wrapper: queryProvider() });

    await waitFor(() => expect(result.current.sessionAuthorized).toBe(true));
    await act(async () => { await result.current.reviewCurrentContext(); });
    act(() => result.current.onTextChange("keep this input"));
    await act(async () => { await result.current.send(); });
    const inputOriginal = result.current.inputAction?.original;
    expect(inputOriginal?.command_type).toBe("SendExecutionInput");

    const setItem = Storage.prototype.setItem;
    let failStopWrite = true;
    const setItemSpy = vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (this: Storage, key: string, value: string) {
      const stored = JSON.parse(value);
      if (failStopWrite && key.startsWith("sidera.native-intervention.v1:")
        && stored.stop.status === "pending") {
        failStopWrite = false;
        throw new Error("storage blocked");
      }
      setItem.call(this, key, value);
    });

    await act(async () => { await result.current.requestStop(); });
    expect(observations.posted).toHaveLength(1);
    setItemSpy.mockRestore();
    act(() => result.current.retryDraftStorage());
    expect(result.current.stopAction).toMatchObject({ status: "idle", original: null });
    expect(result.current.inputAction?.original).toEqual(inputOriginal);

    const readsBeforeExplicitStop = observations.nativeContextReads();
    await act(async () => { await result.current.requestStop(); });

    expect(observations.nativeContextReads()).toBeGreaterThan(readsBeforeExplicitStop);
    expect(observations.posted).toHaveLength(2);
    expect(observations.posted[1]).toMatchObject({ command_type: "StopExecution", expected_version: 7 });
    expect(result.current.inputAction?.original).toEqual(inputOriginal);
  });

  it("does not send an unsaved Stop after authority changes during storage recovery", async () => {
    let currentSession = localSession;
    const observations = installSendAndStopHandlers({ getLocalSession: () => currentSession });
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    const { result } = renderHook(() => useNativeIntervention(hookOptions()), { wrapper: queryProvider() });

    await waitFor(() => expect(result.current.sessionAuthorized).toBe(true));
    await act(async () => { await result.current.reviewCurrentContext(); });
    const setItem = Storage.prototype.setItem;
    let failStopWrite = true;
    const setItemSpy = vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (this: Storage, key: string, value: string) {
      if (failStopWrite && key.startsWith("sidera.native-intervention.v1:")
        && JSON.parse(value).stop.status === "pending") {
        failStopWrite = false;
        throw new Error("storage blocked");
      }
      setItem.call(this, key, value);
    });

    await act(async () => { await result.current.requestStop(); });
    expect(observations.posted).toHaveLength(0);
    setItemSpy.mockRestore();
    act(() => result.current.retryDraftStorage());
    expect(result.current.stopAction).toMatchObject({ status: "idle", original: null });
    currentSession = { ...localSession, principal_id: WORKER_ID };

    await act(async () => { await result.current.requestStop(); });

    expect(observations.posted).toHaveLength(0);
    expect(result.current.stopAction).toMatchObject({ status: "idle", original: null });
  });

  it("preserves a submitted original when outcome persistence fails and recovers it by GET only", async () => {
    const observations = installSendAndStopHandlers({
      commandStatus: (commandId, posted) => {
        const original = posted.find((command) => command.command_id === commandId);
        return {
          command_id: commandId,
          command_type: original?.command_type ?? null,
          target_type: original?.target_type ?? null,
          target_id: original?.target_id ?? null,
          expected_version: original?.expected_version ?? null,
          status: "applied",
          result: { command_id: commandId, aggregate_id: INPUT_ID, aggregate_version: 1, event_ids: [] },
        } satisfies CommandStatusResponse;
      },
    });
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    const { result } = renderHook(() => useNativeIntervention(hookOptions()), { wrapper: queryProvider() });

    await waitFor(() => expect(result.current.sessionAuthorized).toBe(true));
    await act(async () => { await result.current.reviewCurrentContext(); });
    act(() => result.current.onTextChange("submitted input"));
    const setItem = Storage.prototype.setItem;
    let failOutcomeWrite = true;
    const setItemSpy = vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (this: Storage, key: string, value: string) {
      const stored = JSON.parse(value);
      if (failOutcomeWrite && key.startsWith("sidera.native-intervention.v1:")
        && stored.input.status === "applied") {
        failOutcomeWrite = false;
        throw new Error("storage blocked");
      }
      setItem.call(this, key, value);
    });

    await act(async () => { await result.current.send(); });
    setItemSpy.mockRestore();
    const originalId = observations.posted[0]?.command_id;
    expect(observations.posted).toHaveLength(1);
    expect(result.current.inputAction).toMatchObject({ status: "pending", original: observations.posted[0] });

    act(() => result.current.retryDraftStorage());
    expect(result.current.inputAction).toMatchObject({ status: "applied", original: observations.posted[0] });
    await act(async () => { await result.current.lookupOriginalInput(); });

    expect(observations.posted).toHaveLength(1);
    expect(observations.commandGets).toEqual([originalId]);
    expect(result.current.inputAction?.original?.command_id).toBe(originalId);
  });

  it("does not allocate, persist, cache, or post when fresh preflight resolves after unmount", async () => {
    const contextStarted = deferred();
    const releaseContext = deferred();
    let holdNextContext = false;
    let currentContext = nativeContext;
    const observations = installSendAndStopHandlers({
      getNativeContext: () => currentContext,
      beforeNativeContextResponse: async () => {
        if (!holdNextContext) return;
        holdNextContext = false;
        contextStarted.resolve();
        await releaseContext.promise;
      },
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 60_000 } } });
    const nativeContextKey = [...nativeWorkerContextQueryKey(WORKSPACE_ID, WORKER_ITEM_ID), PRINCIPAL_ID];
    client.setQueryData(nativeContextKey, nativeContext);
    let refreshCount = 0;
    const { result, unmount } = renderHook(
      () => useNativeIntervention(hookOptions(async () => { refreshCount += 1; })),
      { wrapper: queryProvider(client) },
    );

    await waitFor(() => expect(result.current.sessionAuthorized).toBe(true));
    await act(async () => { await result.current.reviewCurrentContext(); });
    client.setQueryData(nativeContextKey, nativeContext);
    act(() => result.current.onTextChange("wait for current facts"));
    const draftKey = nativeInterventionDraftKey({
      workspace_id: WORKSPACE_ID,
      principal_id: PRINCIPAL_ID,
      worker_item_id: WORKER_ITEM_ID,
    });
    const storedBeforePreflight = localStorage.getItem(draftKey);
    const uuidSpy = vi.spyOn(globalThis.crypto, "randomUUID");
    holdNextContext = true;
    let sendPromise!: Promise<void>;
    await act(async () => {
      sendPromise = result.current.send();
      await contextStarted.promise;
    });
    const refreshCountAtRead = refreshCount;
    unmount();
    currentContext = { ...nativeContext, context: { ...context, context_version: 5 } };
    releaseContext.resolve();
    await sendPromise;

    expect(observations.posted).toHaveLength(0);
    expect(uuidSpy).not.toHaveBeenCalled();
    expect(localStorage.getItem(draftKey)).toBe(storedBeforePreflight);
    expect(client.getQueryData(nativeContextKey)).toEqual(nativeContext);
    expect(refreshCount).toBe(refreshCountAtRead);
    uuidSpy.mockRestore();
  });

  it("keeps the persisted original and does no post-unmount result write after a held POST", async () => {
    const postStarted = deferred();
    const releasePost = deferred();
    const observations = installSendAndStopHandlers({
      beforeCommandResponse: async () => {
        postStarted.resolve();
        await releasePost.promise;
      },
    });
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    let refreshCount = 0;
    const { result, unmount } = renderHook(
      () => useNativeIntervention(hookOptions(async () => { refreshCount += 1; })),
      { wrapper: queryProvider() },
    );

    await waitFor(() => expect(result.current.sessionAuthorized).toBe(true));
    await act(async () => { await result.current.reviewCurrentContext(); });
    act(() => result.current.onTextChange("persist before posting"));
    let sendPromise!: Promise<void>;
    await act(async () => {
      sendPromise = result.current.send();
      await postStarted.promise;
    });
    const draftKey = nativeInterventionDraftKey({
      workspace_id: WORKSPACE_ID,
      principal_id: PRINCIPAL_ID,
      worker_item_id: WORKER_ITEM_ID,
    });
    const storedAtPost = localStorage.getItem(draftKey);
    const refreshCountAtPost = refreshCount;
    const uuidSpy = vi.spyOn(globalThis.crypto, "randomUUID");
    const uuidCountAtPost = uuidSpy.mock.calls.length;
    unmount();
    releasePost.resolve();
    await sendPromise;

    const persisted = loadNativeInterventionDraft({
      workspace_id: WORKSPACE_ID,
      principal_id: PRINCIPAL_ID,
      worker_item_id: WORKER_ITEM_ID,
    });
    expect(observations.posted).toHaveLength(1);
    expect(localStorage.getItem(draftKey)).toBe(storedAtPost);
    expect(persisted).toMatchObject({
      ok: true,
      draft: { input: { status: "pending", original: observations.posted[0] } },
    });
    expect(refreshCount).toBe(refreshCountAtPost);
    expect(uuidSpy).toHaveBeenCalledTimes(uuidCountAtPost);
    uuidSpy.mockRestore();
  });

  it("requires fresh review after delivered input before sending selected historical text", async () => {
    const observations = installSendAndStopHandlers({
      inputPage: (_page, posted) => ({
        items: posted[0] === undefined ? [] : [observedInput(posted[0], "delivered")],
        next_cursor: null,
      }),
    });
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    const { result } = renderHook(() => useNativeIntervention(hookOptions()), { wrapper: queryProvider() });

    await waitFor(() => expect(result.current.sessionAuthorized).toBe(true));
    await act(async () => { await result.current.reviewCurrentContext(); });
    act(() => result.current.onTextChange("first input"));
    await act(async () => { await result.current.send(); });
    await act(async () => { await result.current.refreshInputStatus(); });
    expect(result.current.inputObservation?.status).toBe("delivered");
    expect(result.current.canSend).toBe(false);
    const priorInput = result.current.inputAction;
    const priorObservation = result.current.inputObservation;
    const priorClientKey = observations.posted[0]?.payload.client_idempotency_key;
    const uuidSpy = vi.spyOn(globalThis.crypto, "randomUUID");
    const uuidCountBeforeSelection = uuidSpy.mock.calls.length;

    act(() => result.current.explicitResendSuperseded("older canonical history body"));

    expect(result.current.text).toBe("older canonical history body");
    expect(result.current.inputAction).toEqual(priorInput);
    expect(result.current.inputObservation).toEqual(priorObservation);
    expect(result.current.canSend).toBe(false);
    expect(observations.posted).toHaveLength(1);
    expect(loadNativeInterventionDraft({
      workspace_id: WORKSPACE_ID,
      principal_id: PRINCIPAL_ID,
      worker_item_id: WORKER_ITEM_ID,
    })).toMatchObject({ ok: true, draft: { client_idempotency_key: priorClientKey, review: null } });
    expect(uuidSpy).toHaveBeenCalledTimes(uuidCountBeforeSelection);

    await act(async () => { await result.current.reviewCurrentContext(); });
    expect(result.current.canSend).toBe(true);
    await act(async () => { await result.current.send(); });

    expect(observations.posted).toHaveLength(2);
    expect(observations.posted[1]?.payload.text).toBe("older canonical history body");
    expect(observations.posted[1]?.payload.client_idempotency_key)
      .not.toBe(priorClientKey);
    expect(result.current.inputAction?.original).toEqual(observations.posted[1]);
    expect(uuidSpy.mock.calls.length).toBeGreaterThan(uuidCountBeforeSelection);
    uuidSpy.mockRestore();
  });

  it("uses the selected superseded activity body only as a reviewed new intent", async () => {
    const observations = installSendAndStopHandlers({
      inputPage: (_page, posted) => ({
        items: posted[0] === undefined ? [] : [observedInput(posted[0], "superseded")],
        next_cursor: null,
      }),
    });
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    const { result } = renderHook(() => useNativeIntervention(hookOptions()), { wrapper: queryProvider() });

    await waitFor(() => expect(result.current.sessionAuthorized).toBe(true));
    await act(async () => { await result.current.reviewCurrentContext(); });
    act(() => result.current.onTextChange("original body"));
    await act(async () => { await result.current.send(); });
    await act(async () => { await result.current.refreshInputStatus(); });
    const oldKey = observations.posted[0]?.payload.client_idempotency_key;
    const priorInput = result.current.inputAction;
    const priorObservation = result.current.inputObservation;
    const uuidSpy = vi.spyOn(globalThis.crypto, "randomUUID");
    const uuidCountBeforeSelection = uuidSpy.mock.calls.length;

    act(() => result.current.explicitResendSuperseded("selected activity body"));
    expect(result.current.text).toBe("selected activity body");
    expect(result.current.inputAction).toEqual(priorInput);
    expect(result.current.inputAction?.original).toEqual(observations.posted[0]);
    expect(result.current.inputObservation).toEqual(priorObservation);
    expect(result.current.canSend).toBe(false);
    expect(observations.posted).toHaveLength(1);
    expect(uuidSpy).toHaveBeenCalledTimes(uuidCountBeforeSelection);
    expect(loadNativeInterventionDraft({
      workspace_id: WORKSPACE_ID,
      principal_id: PRINCIPAL_ID,
      worker_item_id: WORKER_ITEM_ID,
    })).toMatchObject({ ok: true, draft: { client_idempotency_key: oldKey, review: null } });
    await act(async () => { await result.current.reviewCurrentContext(); });
    await act(async () => { await result.current.send(); });

    expect(observations.posted).toHaveLength(2);
    expect(observations.posted[1]?.payload.text).toBe("selected activity body");
    expect(observations.posted[1]?.payload.client_idempotency_key).not.toBe(oldKey);
    expect(result.current.inputAction?.original).toEqual(observations.posted[1]);
    expect(uuidSpy.mock.calls.length).toBeGreaterThan(uuidCountBeforeSelection);
    uuidSpy.mockRestore();
  });

  it("does not replace input text or clear its original while Stop is unresolved", async () => {
    const observations = installSendAndStopHandlers({
      commandResponse: (command) => ({
        command_id: command.command_type === "StopExecution" ? RUN_ID : command.command_id,
        aggregate_id: command.command_type === "SendExecutionInput" ? INPUT_ID : EXECUTION_ID,
        aggregate_version: command.command_type === "SendExecutionInput" ? 1 : 8,
        event_ids: [],
      }),
      inputPage: (_page, posted) => ({
        items: posted[0] === undefined ? [] : [observedInput(posted[0], "superseded")],
        next_cursor: null,
      }),
    });
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    const { result } = renderHook(() => useNativeIntervention(hookOptions()), { wrapper: queryProvider() });

    await waitFor(() => expect(result.current.sessionAuthorized).toBe(true));
    await act(async () => { await result.current.reviewCurrentContext(); });
    act(() => result.current.onTextChange("original body"));
    await act(async () => { await result.current.send(); });
    const inputOriginal = result.current.inputAction?.original;
    await act(async () => { await result.current.requestStop(); });
    expect(result.current.stopAction?.status).toBe("unknown");
    await act(async () => { await result.current.refreshInputStatus(); });

    act(() => result.current.explicitResendSuperseded("selected activity body"));

    expect(result.current.text).toBe("original body");
    expect(result.current.inputAction?.original).toEqual(inputOriginal);
    expect(result.current.inputAction?.status).toBe("applied");
    expect(result.current.stopAction?.status).toBe("unknown");
    expect(observations.posted).toHaveLength(2);
  });

  it("requires fresh review after the journal reports rejection", async () => {
    const observations = installSendAndStopHandlers({
      commandResponse: (command) => ({
        command_id: command.command_type === "SendExecutionInput" ? RUN_ID : command.command_id,
        aggregate_id: INPUT_ID,
        aggregate_version: 1,
        event_ids: [],
      }),
      commandStatus: (commandId, posted) => {
        const original = posted.find((command) => command.command_id === commandId);
        return {
          command_id: commandId,
          command_type: original?.command_type ?? null,
          target_type: original?.target_type ?? null,
          target_id: original?.target_id ?? null,
          expected_version: original?.expected_version ?? null,
          status: "rejected",
          error_code: "input_closed",
          result: null,
        } satisfies CommandStatusResponse;
      },
    });
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    const { result } = renderHook(() => useNativeIntervention(hookOptions()), { wrapper: queryProvider() });

    await waitFor(() => expect(result.current.sessionAuthorized).toBe(true));
    await act(async () => { await result.current.reviewCurrentContext(); });
    act(() => result.current.onTextChange("rejected input"));
    await act(async () => { await result.current.send(); });
    const commandId = observations.posted[0]?.command_id;
    await act(async () => { await result.current.lookupOriginalInput(); });
    expect(result.current.inputAction?.status).toBe("rejected");
    expect(result.current.canSend).toBe(false);

    act(() => result.current.onTextChange("new intent"));
    await act(async () => { await result.current.send(); });
    expect(observations.posted).toHaveLength(1);
    await act(async () => { await result.current.reviewCurrentContext(); });
    await act(async () => { await result.current.send(); });

    expect(observations.posted).toHaveLength(2);
    expect(observations.commandGets).toEqual([commandId]);
  });

  it("retains handled action state when refresh fails after POST and journal GET", async () => {
    const observations = installSendAndStopHandlers({
      commandResponse: (command) => ({
        command_id: command.command_type === "SendExecutionInput" ? RUN_ID : command.command_id,
        aggregate_id: INPUT_ID,
        aggregate_version: 1,
        event_ids: [],
      }),
      commandStatus: (commandId, posted) => {
        const original = posted.find((command) => command.command_id === commandId);
        return {
          command_id: commandId,
          command_type: original?.command_type ?? null,
          target_type: original?.target_type ?? null,
          target_id: original?.target_id ?? null,
          expected_version: original?.expected_version ?? null,
          status: "applied",
          result: { command_id: commandId, aggregate_id: INPUT_ID, aggregate_version: 1, event_ids: [] },
        } satisfies CommandStatusResponse;
      },
    });
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    let refreshCount = 0;
    const { result } = renderHook(
      () => useNativeIntervention(hookOptions(async () => {
        refreshCount += 1;
        if (refreshCount >= 3) throw new Error("refresh unavailable");
      })),
      { wrapper: queryProvider() },
    );

    await waitFor(() => expect(result.current.sessionAuthorized).toBe(true));
    await act(async () => { await result.current.reviewCurrentContext(); });
    act(() => result.current.onTextChange("handled input"));
    await act(async () => { await result.current.send(); });
    const original = observations.posted[0];
    expect(result.current.inputAction).toMatchObject({ status: "unknown", original });

    await act(async () => { await result.current.lookupOriginalInput(); });

    expect(result.current.inputAction).toMatchObject({ status: "applied", original });
    expect(observations.posted).toHaveLength(1);
    expect(observations.commandGets).toEqual([original?.command_id]);
    expect(refreshCount).toBe(4);
  });
});
