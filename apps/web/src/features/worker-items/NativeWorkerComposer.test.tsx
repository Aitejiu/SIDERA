import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";

import { PROFILE_REF } from "@/features/agents/testFixtures";
import type {
  AgentBindingView,
  AgentExecutionView,
  CommandRequest,
  CommandResponse,
  CommandStatusResponse,
  NativeWorkerContextView,
  ProfileRef,
} from "@/features/assignments/types";
import type { LocalSession } from "@/features/local-session/api";
import { server } from "@/test/server";

import type { ExecutionInput } from "../trace/types";
import type { Execution, ExecutionSession } from "../trace/types";
import {
  saveNativeInterventionDraft,
  type NativeInterventionDraft,
  type NativeInterventionReview,
} from "../trace/nativeIntervention";
import type { WorkerActivity, WorkerWorkspace } from "./types";
import { NativeWorkerComposer } from "./NativeWorkerComposer";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const WORKER_ITEM_ID = "40000000-0000-0000-0000-000000000001";
const ROOT_TASK_ID = "30000000-0000-0000-0000-000000000001";
const SUBTASK_ID = "30000000-0000-0000-0000-000000000002";
const EXECUTION_ID = "60000000-0000-0000-0000-000000000001";
const SESSION_ID = "70000000-0000-0000-0000-000000000001";
const WORKER_ID = "90000000-0000-0000-0000-000000000001";
const RUN_ID = "80000000-0000-0000-0000-000000000001";
const INPUT_ID = "50000000-0000-0000-0000-000000000001";
const PRINCIPAL_ID = "10000000-0000-0000-0000-000000000001";
const REPLACEMENT_EXECUTION_ID = "60000000-0000-0000-0000-000000000002";
const REPLACEMENT_SESSION_ID = "70000000-0000-0000-0000-000000000002";
const REPLACEMENT_RUN_ID = "80000000-0000-0000-0000-000000000002";
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
    title: "Callback worker",
    status: "active",
    version: 9,
    context_version: 4,
    acknowledged_context_version: 4,
  },
  current_placement: { plan_generation: 2, subtask_task_id: SUBTASK_ID, subtask_key: "callback-idempotency" },
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

const replacementExecution = {
  ...execution,
  id: REPLACEMENT_EXECUTION_ID,
  version: 8,
} as Execution;

const replacementSession = {
  ...session,
  id: REPLACEMENT_SESSION_ID,
  execution_id: REPLACEMENT_EXECUTION_ID,
  version: 8,
} as ExecutionSession;

const replacementContext: NonNullable<NativeWorkerContextView["context"]> = {
  ...context,
  execution_id: REPLACEMENT_EXECUTION_ID,
  session_id: REPLACEMENT_SESSION_ID,
  session_version: 8,
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
    ...workspace.current_runtime,
    execution_id: REPLACEMENT_EXECUTION_ID,
    session_id: REPLACEMENT_SESSION_ID,
    session_version: 8,
  },
} as unknown as WorkerWorkspace;

const SAVED_EXECUTION_PROFILE_R1 = { ...PROFILE_REF, revision: 1 };
const CURRENT_OWNER_PROFILE_R2 = {
  ...PROFILE_REF,
  profile_id: "a1000000-0000-0000-0000-000000000002",
  revision: 2,
};
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
const originalInputA: CommandRequest = {
  actor: { type: "user", id: PRINCIPAL_ID },
  command_id: "f0000000-0000-0000-0000-000000000004",
  workspace_id: WORKSPACE_ID,
  command_type: "SendExecutionInput",
  target_type: "execution_session",
  target_id: SESSION_ID,
  expected_version: 5,
  idempotency_key: "f0000000-0000-0000-0000-000000000005",
  correlation_id: "f0000000-0000-0000-0000-000000000006",
  payload: {
    context,
    client_idempotency_key: "f0000000-0000-0000-0000-000000000007",
    text: "original A input",
    attachment_refs: [],
  },
};
const originalReviewA: NativeInterventionReview = {
  context,
  execution_version: 7,
  profile_ref: PROFILE_REF,
  binding_version: context.binding_version,
};

function saveOriginalActionsA(stopStatus: "applied" | "unknown" = "applied", includeInput = false): NativeInterventionDraft {
  const draft: NativeInterventionDraft = {
    schema_version: 1,
    scope: { workspace_id: WORKSPACE_ID, principal_id: PRINCIPAL_ID, worker_item_id: WORKER_ITEM_ID },
    text: "",
    client_idempotency_key: null,
    review: null,
    input: includeInput ? {
      original: originalInputA,
      review: originalReviewA,
      status: "applied",
      aggregate_id: INPUT_ID,
      aggregate_version: 1,
      rejection_code: null,
    } : {
      original: null,
      review: null,
      status: "idle",
      aggregate_id: null,
      aggregate_version: null,
      rejection_code: null,
    },
    stop: {
      original: originalStopA,
      review: originalReviewA,
      status: stopStatus,
      aggregate_id: null,
      aggregate_version: null,
      rejection_code: null,
    },
  };
  const saved = saveNativeInterventionDraft(draft.scope, draft);
  if (!saved.ok) throw new Error("could not save original A action fixtures");
  return draft;
}

function replacementNativeContextWithProfiles(
  executionProfileRef: ProfileRef,
  ownerProfileRef: ProfileRef,
): NativeWorkerContextView {
  return {
    ...replacementNativeContext,
    execution: replacementNativeContext.execution === null ? null : {
      ...replacementNativeContext.execution,
      profile_ref: executionProfileRef,
    },
    run: replacementNativeContext.run === null ? null : {
      ...replacementNativeContext.run,
      profile_ref: executionProfileRef,
    },
    binding: replacementNativeContext.binding === null ? null : {
      ...replacementNativeContext.binding,
      assignee: { kind: "profile", id: ownerProfileRef.profile_id },
      profile_ref: ownerProfileRef,
    },
  };
}

function supersededActivity(body: string, activityExecutionId = EXECUTION_ID): WorkerActivity {
  return {
    activity_id: "b0000000-0000-0000-0000-000000000001",
    source_type: "execution_input",
    source_id: INPUT_ID,
    execution_id: activityExecutionId,
    session_id: SESSION_ID,
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

function observedInput(command: CommandRequest, status: ExecutionInput["status"]): ExecutionInput {
  const payload = sendInputPayload(command);
  return {
    id: INPUT_ID,
    workspace_id: WORKSPACE_ID,
    execution_id: payload.context.execution_id,
    session_id: payload.context.session_id,
    client_idempotency_key: payload.client_idempotency_key,
    status,
    operation_id: null,
    failure_code: null,
    version: 1,
    text_length: payload.text.length,
    attachment_count: 0,
    created_at: "2026-10-07T00:00:00Z",
    updated_at: "2026-10-07T00:00:00Z",
  };
}

function sendInputPayload(command: CommandRequest): {
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
  const context = rawContext as Record<string, unknown>;
  if (typeof context.execution_id !== "string" || typeof context.session_id !== "string") {
    throw new Error("SendExecutionInput fixture context is incomplete");
  }
  return {
    context: { execution_id: context.execution_id, session_id: context.session_id },
    client_idempotency_key: payload.client_idempotency_key,
    text: payload.text,
  };
}

function unmappedCommandResponse(command: CommandRequest): CommandResponse {
  if (typeof command.target_id !== "string" || typeof command.expected_version !== "number") {
    throw new Error("command fixture requires a concrete target and expected version");
  }
  return {
    command_id: command.command_id,
    aggregate_id: command.command_type === "SendExecutionInput" ? command.target_id : RUN_ID,
    aggregate_version: command.expected_version + 1,
    event_ids: [],
  };
}

function appliedCommandWithoutResult(command: CommandRequest | undefined): CommandStatusResponse {
  if (command === undefined) throw new Error("command GET must follow its original POST");
  return {
    command_id: command.command_id,
    command_type: command.command_type,
    target_type: command.target_type,
    target_id: command.target_id,
    expected_version: command.expected_version,
    status: "applied",
    result: null,
  };
}

type ComponentHandlerOptions = {
  getNativeContext?: () => NativeWorkerContextView;
  getExecution?: () => Execution;
  getSession?: () => ExecutionSession;
  getInputStatus?: () => ExecutionInput["status"];
  commandResponse?: (command: CommandRequest) => CommandResponse;
  commandStatus?: (command: CommandRequest | undefined) => CommandStatusResponse;
};

function installHandlers(
  status: ExecutionInput["status"] = "delivered",
  options: ComponentHandlerOptions = {},
) {
  const posted: CommandRequest[] = [];
  const commandGets: string[] = [];
  server.use(
    http.get("/product/v1/session", () => HttpResponse.json(localSession)),
    http.get(`${BASE}/worker-items/${WORKER_ITEM_ID}/native-context`, () => HttpResponse.json(options.getNativeContext?.() ?? nativeContext)),
    http.get(/\/api\/v1\/executions\/[^/]+$/, () => HttpResponse.json(options.getExecution?.() ?? execution)),
    http.get("/api/v1/gateway/sessions", () => HttpResponse.json({ items: [options.getSession?.() ?? session], next_cursor: null })),
    http.get("/api/v1/gateway/inputs", () => HttpResponse.json({
      items: posted[0] === undefined
        ? []
        : [observedInput(posted[0], options.getInputStatus?.() ?? status)],
      next_cursor: null,
    })),
    http.get(`${BASE}/commands/:commandId`, ({ params }) => {
      const commandId = String(params.commandId);
      commandGets.push(commandId);
      const original = posted.find((command) => command.command_id === commandId);
      return HttpResponse.json(options.commandStatus?.(original) ?? {
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
      return HttpResponse.json(options.commandResponse?.(command) ?? {
        command_id: command.command_id,
        aggregate_id: command.command_type === "SendExecutionInput" ? INPUT_ID : EXECUTION_ID,
        aggregate_version: command.command_type === "SendExecutionInput" ? 1 : 8,
        event_ids: [],
      } satisfies CommandResponse);
    }),
  );
  return { posted, commandGets };
}

type ComposerProps = {
  workspace: WorkerWorkspace;
  execution: Execution | undefined;
  nativeContext: NativeWorkerContextView | undefined;
  activities: readonly WorkerActivity[];
  onRefresh: () => Promise<void>;
};

function renderComposer(activities: readonly WorkerActivity[] = [], overrides: Partial<ComposerProps> = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  let props: ComposerProps = { workspace, execution, nativeContext, activities, onRefresh: async () => {}, ...overrides };
  const page = () => (
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <NativeWorkerComposer
          workspaceId={WORKSPACE_ID}
          workerItemId={WORKER_ITEM_ID}
          {...props}
        />
      </MemoryRouter>
    </QueryClientProvider>
  );
  const view = render(page());
  return {
    ...view,
    rerenderComposer(next: Partial<ComposerProps>) {
      props = { ...props, ...next };
      view.rerender(page());
    },
  };
}

afterEach(() => {
  server.resetHandlers();
  localStorage.clear();
  document.cookie = "sidera_csrf=; Max-Age=0; Path=/";
});

describe("NativeWorkerComposer", () => {
  it("enables reviewed Send in rendered UI during streaming and posts the latest observation once", async () => {
    const streamingContext: NativeWorkerContextView = {
      ...nativeContext,
      context: { ...context, session_last_sequence: 3 },
      execution: { ...executionPin, event_cursor: 99 },
    };
    let currentNativeContext = streamingContext;
    let currentSession = { ...session, version: 7, last_sequence: 5 };
    const { posted } = installHandlers("delivered", {
      getNativeContext: () => currentNativeContext,
      getSession: () => currentSession,
    });
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    const user = userEvent.setup();
    renderComposer([], {
      nativeContext: streamingContext,
      workspace: { ...workspace, current_runtime: { ...workspace.current_runtime!, session_version: 4 } },
    });

    const input = screen.getByRole("textbox", { name: "介入这个 Worker" });
    const send = screen.getByRole("button", { name: "发送到当前 Session" });
    await waitFor(() => expect(input).toBeEnabled());
    expect(send).toBeDisabled();
    await user.type(input, "send while observations stream");
    await user.click(screen.getByRole("button", { name: "复核当前 context" }));
    await waitFor(() => expect(send).toBeEnabled());

    currentNativeContext = {
      ...streamingContext,
      context: { ...context, session_version: 10, session_last_sequence: 8 },
    };
    currentSession = { ...session, version: 10, last_sequence: 8 };
    await user.click(send);

    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]).toMatchObject({
      command_type: "SendExecutionInput",
      target_type: "execution_session",
      target_id: SESSION_ID,
      expected_version: 10,
      payload: {
        context: { ...context, session_version: 10, session_last_sequence: 8 },
        text: "send while observations stream",
        client_idempotency_key: expect.any(String),
      },
    });
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("命令已处理"));
    expect(send).toBeDisabled();
  });

  it("keeps Send behind explicit review and shows command and delivery states separately", async () => {
    const { posted } = installHandlers("delivered");
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    const user = userEvent.setup();
    renderComposer();

    const input = screen.getByRole("textbox", { name: "介入这个 Worker" });
    const send = screen.getByRole("button", { name: "发送到当前 Session" });
    await waitFor(() => expect(input).toBeEnabled());
    expect(send).toBeDisabled();
    expect(screen.getByRole("link", { name: "与主 Agent 讨论调整" })).toHaveAttribute("href", expect.stringContaining("workerItemId="));

    await user.type(input, "inspect current state");
    await user.click(screen.getByRole("button", { name: "复核当前 context" }));
    await waitFor(() => expect(send).toBeEnabled());
    await user.click(send);

    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]).toMatchObject({
      command_type: "SendExecutionInput",
      target_type: "execution_session",
      target_id: SESSION_ID,
      expected_version: 5,
      payload: { context, text: "inspect current state", attachment_refs: [] },
    });
    expect(screen.getByRole("status")).toHaveTextContent("命令已处理");
    expect(screen.getByLabelText("原生操作状态")).toHaveTextContent("实际投递：投递状态未读取");

    await user.click(screen.getByRole("button", { name: "读取原生投递状态" }));
    await waitFor(() => expect(screen.getByLabelText("原生操作状态")).toHaveTextContent("实际投递：已投递"));
  });

  it("labels Stop as requested without claiming the running Execution stopped", async () => {
    const { posted } = installHandlers();
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    const user = userEvent.setup();
    renderComposer();

    await user.click(await screen.findByRole("button", { name: "复核当前 context" }));
    const stop = screen.getByRole("button", { name: "停止 Execution" });
    await waitFor(() => expect(stop).toBeEnabled());
    await user.click(stop);

    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]).toMatchObject({
      command_type: "StopExecution",
      target_type: "execution",
      target_id: EXECUTION_ID,
      expected_version: 7,
      payload: {},
    });
    expect(screen.getByText(/停止请求已记录/)).toBeInTheDocument();
    expect(screen.queryByText("Execution 已停止")).not.toBeInTheDocument();
  });

  it("stops a reviewed UNKNOWN Run once and recovers only its original command", async () => {
    const unknownContext: NativeWorkerContextView = {
      ...nativeContext,
      input_enabled: false,
      read_only_reason: "native_run_unknown",
      run: { ...nativeContext.run!, state: "unknown", input_open: false },
    };
    const { posted, commandGets } = installHandlers("delivered", {
      getNativeContext: () => unknownContext,
      commandResponse: unmappedCommandResponse,
      commandStatus: appliedCommandWithoutResult,
    });
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    const user = userEvent.setup();
    renderComposer([], { nativeContext: unknownContext });

    const input = screen.getByRole("textbox", { name: "介入这个 Worker" });
    const send = screen.getByRole("button", { name: "发送到当前 Session" });
    const stop = screen.getByRole("button", { name: "停止 Execution" });
    await waitFor(() => expect(input).toBeEnabled());
    await user.type(input, "must remain unsent while the Run is UNKNOWN");
    await user.click(screen.getByRole("button", { name: "复核当前 context" }));
    await waitFor(() => expect(stop).toBeEnabled());
    expect(send).toBeDisabled();
    await user.click(send);
    expect(posted).toHaveLength(0);
    await user.click(stop);

    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]).toEqual({
      actor: { type: "user", id: PRINCIPAL_ID },
      command_id: expect.any(String),
      workspace_id: WORKSPACE_ID,
      command_type: "StopExecution",
      target_type: "execution",
      target_id: EXECUTION_ID,
      expected_version: 7,
      idempotency_key: expect.any(String),
      correlation_id: expect.any(String),
      payload: {},
    });
    const commandId = posted[0]?.command_id;
    expect(commandId).toBeDefined();
    const status = screen.getByLabelText("原生操作状态");
    await waitFor(() => expect(status).toHaveTextContent("停止请求已记录"));
    expect(status).not.toHaveTextContent("Execution 已停止");
    expect(send).toBeDisabled();
    expect(stop).toBeDisabled();
    await user.click(stop);

    await user.click(screen.getByRole("button", { name: "查询原始 Stop 命令" }));
    await waitFor(() => expect(commandGets).toEqual([commandId]));
    await user.click(screen.getByRole("button", { name: "查询原始 Stop 命令" }));
    await waitFor(() => expect(commandGets).toEqual([commandId, commandId]));
    expect(posted).toHaveLength(1);
    expect(status).toHaveTextContent("停止请求已记录");
    expect(status).not.toHaveTextContent("Execution 已停止");
    expect(send).toBeDisabled();
    expect(stop).toBeDisabled();
  });

  it("keeps applied-unmapped input recovery reachable without another POST", async () => {
    const { posted, commandGets } = installHandlers("delivered", {
      commandResponse: unmappedCommandResponse,
      commandStatus: appliedCommandWithoutResult,
    });
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    const user = userEvent.setup();
    renderComposer();

    const input = screen.getByRole("textbox", { name: "介入这个 Worker" });
    await waitFor(() => expect(input).toBeEnabled());
    await user.type(input, "recover original input");
    await user.click(screen.getByRole("button", { name: "复核当前 context" }));
    await user.click(screen.getByRole("button", { name: "发送到当前 Session" }));
    await waitFor(() => expect(posted).toHaveLength(1));

    const commandId = posted[0]?.command_id;
    expect(commandId).toBeDefined();
    await user.click(await screen.findByRole("button", { name: "查询原始输入命令" }));
    await waitFor(() => expect(commandGets).toEqual([commandId]));
    expect(screen.getByLabelText("原生操作状态")).toHaveTextContent("输入命令：命令已处理");

    await user.click(screen.getByRole("button", { name: "查询原始输入命令" }));
    await waitFor(() => expect(commandGets).toEqual([commandId, commandId]));
    expect(posted).toHaveLength(1);
  });

  it("recovers an applied-unmapped Stop and only confirms its exact Execution", async () => {
    const { posted, commandGets } = installHandlers("delivered", {
      commandResponse: unmappedCommandResponse,
      commandStatus: appliedCommandWithoutResult,
    });
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    const user = userEvent.setup();
    const view = renderComposer();

    await user.click(await screen.findByRole("button", { name: "复核当前 context" }));
    await user.click(screen.getByRole("button", { name: "停止 Execution" }));
    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]).toMatchObject({ command_type: "StopExecution", target_id: EXECUTION_ID });
    const commandId = posted[0]?.command_id;

    await user.click(await screen.findByRole("button", { name: "查询原始 Stop 命令" }));
    await waitFor(() => expect(commandGets).toEqual([commandId]));
    expect(screen.getByLabelText("原生操作状态")).toHaveTextContent("停止请求已记录");
    await user.click(screen.getByRole("button", { name: "查询原始 Stop 命令" }));
    await waitFor(() => expect(commandGets).toEqual([commandId, commandId]));
    expect(posted).toHaveLength(1);

    view.rerenderComposer({ execution: { ...replacementExecution, status: "stopped" } as Execution });
    expect(screen.getByLabelText("原生操作状态")).not.toHaveTextContent("Execution 已停止");
    expect(screen.getByLabelText("原生操作状态")).toHaveTextContent("停止请求已记录");

    view.rerenderComposer({ execution: { ...execution, status: "stopped" } as Execution });
    expect(screen.getByLabelText("原生操作状态")).toHaveTextContent("Execution 已停止");

    const nonterminalOrForeignExecutions: Array<Execution | undefined> = [
      { ...execution, status: "running", stop_requested: true } as Execution,
      { ...execution, status: "failed" } as Execution,
      { ...execution, status: "succeeded" } as Execution,
      { ...execution, status: "canceled" } as Execution,
      { ...execution, workspace_id: "20000000-0000-0000-0000-000000000099", status: "stopped" } as Execution,
      { ...execution, worker_item_id: "40000000-0000-0000-0000-000000000099", status: "stopped" } as Execution,
      undefined,
    ];
    for (const currentExecution of nonterminalOrForeignExecutions) {
      view.rerenderComposer({ execution: currentExecution });
      expect(screen.getByLabelText("原生操作状态")).not.toHaveTextContent("Execution 已停止");
      expect(screen.getByLabelText("原生操作状态")).toHaveTextContent("停止请求已记录");
    }
  });

  it("lets current B use its reviewed context while preserving and reading original Stop A", async () => {
    saveOriginalActionsA();
    const { posted, commandGets } = installHandlers("delivered", {
      getNativeContext: () => replacementNativeContext,
      getExecution: () => replacementExecution,
      getSession: () => replacementSession,
      commandStatus: (command) => command === undefined ? {
        command_id: originalStopA.command_id,
        command_type: originalStopA.command_type,
        target_type: originalStopA.target_type,
        target_id: originalStopA.target_id,
        expected_version: originalStopA.expected_version,
        status: "applied",
        result: null,
      } : appliedCommandWithoutResult(command),
    });
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    const user = userEvent.setup();
    const body = "canonical history for replacement B";
    const view = renderComposer([supersededActivity(body, EXECUTION_ID)], {
      workspace: replacementWorkspace,
      execution: replacementExecution,
      nativeContext: replacementNativeContext,
    });

    const input = screen.getByRole("textbox", { name: "介入这个 Worker" });
    await waitFor(() => expect(input).toBeEnabled());
    const load = screen.getByRole("button", { name: "载入为新介入草稿" });
    expect(load).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "选择此失效输入" }));
    await waitFor(() => expect(load).toBeEnabled());
    await user.click(load);
    expect(input).toHaveValue(body);
    expect(screen.getByLabelText("原生操作状态")).not.toHaveTextContent("Execution 已停止");

    await user.click(screen.getByRole("button", { name: "复核当前 context" }));
    const send = screen.getByRole("button", { name: "发送到当前 Session" });
    await waitFor(() => expect(send).toBeEnabled());
    await user.click(send);
    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]).toMatchObject({
      command_type: "SendExecutionInput",
      actor: { type: "user", id: PRINCIPAL_ID },
      target_id: REPLACEMENT_SESSION_ID,
      expected_version: 8,
      payload: { context: replacementContext, text: body },
    });
    expect(screen.getByLabelText("原生操作状态")).toHaveTextContent("实际投递：投递状态未读取");
    await user.click(screen.getByRole("button", { name: "查询原始 Stop 命令" }));
    await waitFor(() => expect(commandGets).toEqual([originalStopA.command_id]));
    expect(posted).toHaveLength(1);

    view.rerenderComposer({ execution: { ...replacementExecution, status: "stopped" } as Execution });
    expect(screen.getByLabelText("原生操作状态")).not.toHaveTextContent("Execution 已停止");
    expect(screen.getByLabelText("原生操作状态")).toHaveTextContent("停止请求已记录");
  });

  it("keeps the canonical historical loader disabled for an unknown original Stop", async () => {
    saveOriginalActionsA("unknown");
    installHandlers("delivered", {
      getNativeContext: () => replacementNativeContext,
      getExecution: () => replacementExecution,
      getSession: () => replacementSession,
    });
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    const body = "must not bypass unknown Stop";
    const user = userEvent.setup();
    renderComposer([supersededActivity(body, EXECUTION_ID)], {
      workspace: replacementWorkspace,
      execution: replacementExecution,
      nativeContext: replacementNativeContext,
    });

    await waitFor(() => expect(screen.getByRole("textbox", { name: "介入这个 Worker" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "选择此失效输入" }));
    expect(screen.getByRole("button", { name: "载入为新介入草稿" })).toBeDisabled();
  });

  it("shows saved Execution and original action pins separately from the current owner", async () => {
    saveOriginalActionsA("applied", true);
    const currentContext = replacementNativeContextWithProfiles(
      SAVED_EXECUTION_PROFILE_R1,
      CURRENT_OWNER_PROFILE_R2,
    );
    installHandlers("delivered", {
      getNativeContext: () => currentContext,
      getExecution: () => replacementExecution,
      getSession: () => replacementSession,
    });
    const user = userEvent.setup();
    renderComposer([], {
      workspace: replacementWorkspace,
      execution: replacementExecution,
      nativeContext: currentContext,
    });
    await user.click(screen.getByText("查看精确 native context"));

    expect(screen.getByText("Execution Profile")).toBeVisible();
    expect(screen.getAllByText(`${SAVED_EXECUTION_PROFILE_R1.profile_id} · r1`)).toHaveLength(3);
    expect(screen.getByText("Current owner Profile")).toBeVisible();
    expect(screen.getByText(`${CURRENT_OWNER_PROFILE_R2.profile_id} · r2`)).toBeVisible();
    expect(screen.getByRole("heading", { name: "原始输入固定快照" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "原始 Stop 固定快照" })).toBeVisible();

    const inputPins = screen.getByRole("heading", { name: "原始输入固定快照" }).nextElementSibling;
    const stopPins = screen.getByRole("heading", { name: "原始 Stop 固定快照" }).nextElementSibling;
    expect(inputPins?.tagName).toBe("DL");
    expect(stopPins?.tagName).toBe("DL");
    if (!(inputPins instanceof HTMLElement) || !(stopPins instanceof HTMLElement)) {
      throw new Error("original action pin lists must follow their headings");
    }
    expect(within(inputPins).getByText(originalInputA.command_id)).toBeVisible();
    expect(within(inputPins).getByText(EXECUTION_ID)).toBeVisible();
    expect(within(inputPins).getByText(SESSION_ID)).toBeVisible();
    expect(within(inputPins).getByText(RUN_ID)).toBeVisible();
    expect(within(inputPins).getByText(`${PROFILE_REF.profile_id} · r${PROFILE_REF.revision}`)).toBeVisible();
    expect(within(stopPins).getByText(originalStopA.command_id)).toBeVisible();
    expect(within(stopPins).getByText(EXECUTION_ID)).toBeVisible();
    expect(within(stopPins).getByText(SESSION_ID)).toBeVisible();
    expect(within(stopPins).getByText(RUN_ID)).toBeVisible();
    expect(within(stopPins).getByText(`${PROFILE_REF.profile_id} · r${PROFILE_REF.revision}`)).toBeVisible();
    expect(screen.getByText(REPLACEMENT_EXECUTION_ID)).toBeVisible();
    expect(screen.getByText(REPLACEMENT_RUN_ID)).toBeVisible();
  });

  it.each([
    ["missing", null],
    ["nonmatching", { ...replacementNativeContext.execution!, execution_id: EXECUTION_ID }],
  ])("shows saved Execution Profile unavailable when its link is %s", async (_label, savedExecution) => {
    const currentContext = {
      ...replacementNativeContextWithProfiles(SAVED_EXECUTION_PROFILE_R1, CURRENT_OWNER_PROFILE_R2),
      execution: savedExecution,
    } as NativeWorkerContextView;
    installHandlers("delivered", {
      getNativeContext: () => currentContext,
      getExecution: () => replacementExecution,
      getSession: () => replacementSession,
    });
    const user = userEvent.setup();
    renderComposer([], {
      workspace: replacementWorkspace,
      execution: replacementExecution,
      nativeContext: currentContext,
    });
    await user.click(screen.getByText("查看精确 native context"));

    expect(screen.getByText("Execution Profile").parentElement).toHaveTextContent("不可用");
    expect(screen.getByText(`${CURRENT_OWNER_PROFILE_R2.profile_id} · r2`)).toBeVisible();
    expect(screen.queryByText(`${SAVED_EXECUTION_PROFILE_R1.profile_id} · r1`)).not.toBeInTheDocument();
  });

  it("keeps one read-only notice and one native status row when context is unavailable", async () => {
    installHandlers();
    const { container } = renderComposer([], { nativeContext: undefined });

    await screen.findByRole("button", { name: "复核当前 context" });
    expect(await screen.findAllByText(/当前 native 执行上下文不可用/)).toHaveLength(1);
    expect(container.querySelectorAll(".worker-composer__native-status")).toHaveLength(1);
  });

  it("selects a canonical superseded body from the prior Execution, then reviews current context", async () => {
    let currentNativeContext = nativeContext;
    let currentExecution = execution;
    let currentSession = session;
    const { posted } = installHandlers("superseded", {
      getNativeContext: () => currentNativeContext,
      getExecution: () => currentExecution,
      getSession: () => currentSession,
    });
    document.cookie = "sidera_csrf=csrf-native; Path=/";
    const user = userEvent.setup();
    const historicalBody = "body from the canonical activity";
    const view = renderComposer([supersededActivity(historicalBody)]);

    const input = screen.getByRole("textbox", { name: "介入这个 Worker" });
    await waitFor(() => expect(input).toBeEnabled());
    await user.type(input, "old local body");
    await user.click(screen.getByRole("button", { name: "复核当前 context" }));
    await user.click(screen.getByRole("button", { name: "发送到当前 Session" }));
    await waitFor(() => expect(posted).toHaveLength(1));
    await user.click(screen.getByRole("button", { name: "读取原生投递状态" }));
    await screen.findByText(historicalBody);

    currentNativeContext = replacementNativeContext;
    currentExecution = replacementExecution;
    currentSession = replacementSession;
    view.rerenderComposer({
      workspace: replacementWorkspace,
      execution: replacementExecution,
      nativeContext: replacementNativeContext,
    });

    await user.click(screen.getByRole("button", { name: "选择此失效输入" }));
    expect(input).toHaveValue("old local body");
    expect(posted).toHaveLength(1);

    await user.click(screen.getByRole("button", { name: "载入为新介入草稿" }));
    expect(input).toHaveValue(historicalBody);
    expect(posted).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "复核当前 context" }));
    await user.click(screen.getByRole("button", { name: "发送到当前 Session" }));

    await waitFor(() => expect(posted).toHaveLength(2));
    expect(posted[1]).toMatchObject({
      target_id: REPLACEMENT_SESSION_ID,
      expected_version: 8,
      payload: { context: replacementContext, text: historicalBody },
    });
    expect(posted[1]?.payload.client_idempotency_key).not.toBe(posted[0]?.payload.client_idempotency_key);
  });

  it("does not load a stale selected activity after it becomes ineligible", async () => {
    installHandlers();
    const first = supersededActivity("stale selected body");
    const second = { ...supersededActivity("remaining eligible body"), activity_id: "b0000000-0000-0000-0000-000000000003" };
    const view = renderComposer([first, second]);
    const user = userEvent.setup();
    const input = screen.getByRole("textbox", { name: "介入这个 Worker" });
    await waitFor(() => expect(input).toBeEnabled());
    const choices = screen.getAllByRole("button", { name: "选择此失效输入" });
    const firstChoice = choices[0];
    if (firstChoice === undefined) throw new Error("first superseded activity should be selectable");
    await user.click(firstChoice);

    const load = screen.getByRole("button", { name: "载入为新介入草稿" });
    for (const updatedActivities of [
      [{ ...first, delivery_status: "delivered" }, second],
      [second],
      [{ ...first, body: "  " }, second],
    ] satisfies WorkerActivity[][]) {
      view.rerenderComposer({ activities: updatedActivities });
      expect(load).toBeDisabled();
      expect(input).not.toHaveValue("stale selected body");
    }
  });
});
