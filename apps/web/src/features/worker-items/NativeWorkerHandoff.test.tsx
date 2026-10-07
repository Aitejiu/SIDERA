import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { createElement, type PropsWithChildren } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";

import type { ProfileRef, ProfileView } from "@/features/agents/types";
import { PROFILE } from "@/features/agents/testFixtures";
import type { LocalSession } from "@/features/local-session/api";
import { workerResultInputsFixture } from "@/test/resultInputFixtures";
import { server } from "@/test/server";
import { nativeWorkerContextQueryKey } from "../assignments/api";
import { resultView } from "../results/testFixtures";
import type {
  AgentBindingView,
  CommandRequest,
  CommandResponse,
  NativeWorkerContextView,
} from "../assignments/types";
import { handoffDraftKey, type HandoffScope } from "../assignments/profileHandoff";
import { handoffCandidatesQueryKey, workerWorkspaceQueryKey } from "./api";
import type {
  ActiveHandoff,
  HandoffCandidateList,
  NativeHandoffProposal,
  NativeHandoffTransition,
  WorkerWorkspace,
} from "./types";
import { WorkerItemPage } from "./WorkerItemPage";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const WORKER_ITEM_ID = "40000000-0000-0000-0000-000000000001";
const ROOT_TASK_ID = "30000000-0000-0000-0000-000000000001";
const SUBTASK_ID = "30000000-0000-0000-0000-000000000002";
const PRINCIPAL_ID = "principal-current";
const EXECUTION_ID = "60000000-0000-0000-0000-000000000001";
const SESSION_ID = "70000000-0000-0000-0000-000000000001";
const WORKER_ID = "80000000-0000-0000-0000-000000000001";
const RUN_ID = "90000000-0000-0000-0000-000000000001";
const PROPOSAL_ID = "a0000000-0000-0000-0000-000000000001";
const TRANSITION_ID = "b0000000-0000-0000-0000-000000000001";
const BASE = `/api/v1/workspaces/${WORKSPACE_ID}`;
const handoffScope: HandoffScope = {
  workspace_id: WORKSPACE_ID,
  principal_id: PRINCIPAL_ID,
  worker_item_id: WORKER_ITEM_ID,
};
const SESSION: LocalSession = {
  session_id: "50000000-0000-0000-0000-000000000001",
  workspace_id: WORKSPACE_ID,
  principal_id: PRINCIPAL_ID,
  expires_at: "2099-01-01T00:00:00Z",
};
const sourceProfileRef: ProfileRef = {
  workspace_id: WORKSPACE_ID,
  profile_id: "50000000-0000-0000-0000-000000000002",
  revision: 1,
};
const sourceProfileHeadRef = { ...sourceProfileRef, revision: 2 };

function profileAt(ref: ProfileRef, name: string): ProfileView {
  return {
    profile: {
      ref,
      draft: { ...PROFILE.profile.draft, name },
      config_hash: "a".repeat(64),
      created_at: "2026-10-07T00:00:00Z",
    },
    status: { profile_id: ref.profile_id, enabled: true, version: ref.revision },
  };
}

const sourceProfileR1 = profileAt(sourceProfileRef, "Source Profile R1");
const sourceProfileR2 = profileAt(sourceProfileHeadRef, "Source Profile head R2");
const sourceBinding: AgentBindingView = {
  workspace_id: WORKSPACE_ID,
  target_type: "worker_item",
  target_id: WORKER_ITEM_ID,
  assignee: { kind: "profile", id: sourceProfileRef.profile_id },
  profile_ref: sourceProfileRef,
  version: 2,
  runnable: true,
  not_runnable_reasons: [],
  created_at: "2026-10-07T00:00:00Z",
  changed_at: "2026-10-07T00:00:00Z",
};
const workspace = {
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
    version: 7,
    updated_at: "2026-10-07T00:00:00Z",
  },
  current_placement: {
    worker_item_key: "native-worker",
    subtask_key: "callback-idempotency",
    subtask_task_id: SUBTASK_ID,
    role: "implementer",
    capability_requirements: [],
    plan_generation: 2,
    plan_snapshot_id: "10000000-0000-0000-0000-000000000001",
  },
  placement_history: [],
  current_runtime: {
    adapter_type: "agent-run",
    attempt: 1,
    execution_id: EXECUTION_ID,
    execution_status: "running",
    fencing_token: 2,
    input_mode: "queued",
    lease_id: "lease-1",
    session_id: SESSION_ID,
    session_status: "active",
    session_version: 3,
    task_id: SUBTASK_ID,
    worker_display_name: "Source Profile R1",
    worker_id: WORKER_ID,
  },
  execution_history: [{
    execution_id: EXECUTION_ID,
    task_id: SUBTASK_ID,
    status: "running",
    attempt: 1,
    worker_id: WORKER_ID,
    session_id: SESSION_ID,
    failure_code: null,
    started_at: "2026-10-07T00:00:00Z",
    finished_at: null,
    created_at: "2026-10-07T00:00:00Z",
    updated_at: "2026-10-07T00:00:00Z",
    version: 5,
    capability_requirements: [],
    adapter_preferences: ["agent-run"],
    adapter_forbidden: [],
    scheduling_status: "assigned",
    waiting_reason: null,
    waiting_details_summary: null,
    requires_operator: false,
    blocked_since: null,
    pinned_worker_id: null,
    assigned_worker_id: WORKER_ID,
    pinned_worker_summary: null,
    assigned_worker_summary: null,
    binding_summary: null,
  }],
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
    session_version: 3,
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
} satisfies WorkerWorkspace;
const nativeContext: NativeWorkerContextView = {
  workspace_id: WORKSPACE_ID,
  worker_item_id: WORKER_ITEM_ID,
  execution_mode: "native_profile",
  worker_item_version: 7,
  binding: sourceBinding,
  execution: {
    workspace_id: WORKSPACE_ID,
    execution_id: EXECUTION_ID,
    worker_item_id: WORKER_ITEM_ID,
    root_task_id: ROOT_TASK_ID,
    subtask_id: SUBTASK_ID,
    profile_ref: sourceProfileRef,
    binding_version: 2,
    run_id: RUN_ID,
    event_cursor: 5,
    status: "running",
    link_status: "accepted",
    updated_at: "2026-10-07T00:00:00Z",
  },
  execution_version: 5,
  context: {
    worker_item_id: WORKER_ITEM_ID,
    execution_mode: "native_profile",
    execution_id: EXECUTION_ID,
    session_id: SESSION_ID,
    session_version: 3,
    worker_id: WORKER_ID,
    binding_version: 2,
    run_id: RUN_ID,
    plan_generation: 2,
    context_version: 4,
  },
  input_enabled: false,
  read_only_reason: "input_not_available",
  run: {
    state: "running",
    input_open: false,
    profile_ref: sourceProfileRef,
    runtime_ref: { workspace_id: WORKSPACE_ID, runtime_id: "b0000000-0000-0000-0000-000000000003" },
    actual_runtime_version: "1",
    applied_fields: [],
    config_hash: "c".repeat(64),
    observed_model: null,
    reason_code: null,
    selected_model: null,
  },
  as_of: "2026-10-07T00:00:00Z",
};
const legacyNativeContext: NativeWorkerContextView = {
  ...nativeContext,
  execution_mode: "legacy",
  execution: null,
  execution_version: null,
  context: null,
  input_enabled: false,
  read_only_reason: "execution_mode_legacy",
  run: null,
};
const legacyWorkspace = {
  ...workspace,
  current_runtime: {
    ...workspace.current_runtime,
    adapter_type: "opencode",
    worker_display_name: "Legacy source",
  },
} satisfies WorkerWorkspace;
const proposalContext = {
  source_package_id: "c0000000-0000-0000-0000-000000000001",
  source_package_hash: "b".repeat(64),
  source_context_version: 3,
  target_package_id: null,
  target_package_hash: null,
  target_context_version: 4,
};
const planBase = {
  snapshot_id: workspace.current_placement.plan_snapshot_id,
  generation: 2,
  head_version: 6,
};
const sourceStopUnconfirmedTransition: NativeHandoffTransition = {
  id: TRANSITION_ID,
  handoff_schema_version: 2,
  workspace_id: WORKSPACE_ID,
  worker_item_id: WORKER_ITEM_ID,
  proposal_id: PROPOSAL_ID,
  version: 2,
  status: "running",
  phase: "stop_source",
  writer_state: "source_stop_requested",
  confirmed_at: "2026-10-07T00:02:00Z",
  started_at: "2026-10-07T00:02:30Z",
  completed_at: null,
  failure_code: "source_stop_unconfirmed",
  blocked_operation_id: null,
  logical_attempt: 1,
  next_attempt_at: "2026-10-07T00:05:00Z",
  source_execution_id: EXECUTION_ID,
  source_session_id: SESSION_ID,
  source_run_id: RUN_ID,
  source_binding_version: 2,
  source_profile_ref: sourceProfileRef,
  source_input_cutoff_sequence: 5,
  target_assignee: { kind: "human", id: PRINCIPAL_ID },
  target_profile_ref: null,
  target_worker_id: null,
  target_execution_id: null,
  target_session_id: null,
  target_run_id: null,
  target_binding_version: null,
  target_context_version: 4,
  context: proposalContext,
  plan_base: planBase,
  available_recovery_strategies: [],
};
const activeStopUnconfirmed: ActiveHandoff = {
  transition_id: TRANSITION_ID,
  proposal_id: PROPOSAL_ID,
  status: sourceStopUnconfirmedTransition.status,
  phase: sourceStopUnconfirmedTransition.phase,
  writer_state: sourceStopUnconfirmedTransition.writer_state,
  source_execution_id: EXECUTION_ID,
  source_session_id: SESSION_ID,
  source_worker_id: WORKER_ID,
  target_worker_id: null,
  target_execution_id: null,
  target_session_id: null,
  attention_required: true,
  failure_code: sourceStopUnconfirmedTransition.failure_code,
  blocked_operation_id: null,
  available_recovery_strategies: [],
  source_trace_descriptor: workspace.trace_descriptor,
  target_trace_descriptor: null,
  started_at: null,
  updated_at: "2026-10-07T00:02:00Z",
};
const cachedEligibleLegacyCandidates: HandoffCandidateList = {
  workspace_id: WORKSPACE_ID,
  worker_item_id: WORKER_ITEM_ID,
  source_execution_id: EXECUTION_ID,
  source_session_id: SESSION_ID,
  source_worker_id: WORKER_ID,
  source_worker_item_version: 7,
  candidates: [{
    worker_id: "80000000-0000-0000-0000-000000000009",
    display_name: "Legacy candidate",
    adapter_type: "opencode",
    eligible: true,
    recommended: true,
    ineligibility_code: null,
    runtime_snapshot: {},
    capability_snapshot_hash: "e".repeat(64),
    policy_reason: { selected: "recommended" },
    workspace_compatibility: { compatible: true },
    runtime_profile_label: "OpenCode · v1",
    capabilities_satisfied: [],
    health_summary: "idle; load 0/2",
    policy_reasons: [{ code: "recommended" }],
    inspected_at: "2026-10-07T00:00:00Z",
  }],
  recommended_worker_id: "80000000-0000-0000-0000-000000000009",
  blocking_codes: [],
  inspected_at: "2026-10-07T00:00:00Z",
};

function renderPage(client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })) {
  function Provider({ children }: PropsWithChildren) {
    return createElement(QueryClientProvider, { client }, children);
  }
  const view = render(
    <WorkerItemPage />,
    {
      wrapper: ({ children }) => (
        <Provider>
          <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/worker-items/${WORKER_ITEM_ID}`]}>
            <Routes>
              <Route path="/workspaces/:workspaceId/worker-items/:workerItemId" element={children} />
            </Routes>
          </MemoryRouter>
        </Provider>
      ),
    },
  );
  return { ...view, client };
}

function installHandlers(options: {
  activeTransition?: NativeHandoffTransition;
  initialLegacy?: boolean;
  nativeContextUnavailable?: boolean;
  prepareReplyLost?: boolean;
  prepareLookupDenied?: boolean;
  confirmReplyLost?: boolean;
} = {}) {
  const posts: CommandRequest[] = [];
  let candidateGets = 0;
  const profileRevisionReads: string[] = [];
  let currentProposal: NativeHandoffProposal | null = null;
  let currentTransition: NativeHandoffTransition | null = null;
  let pageWorkspace: WorkerWorkspace = options.initialLegacy
    ? legacyWorkspace
    : options.activeTransition === undefined
      ? workspace
      : { ...workspace, active_handoff: activeStopUnconfirmed };
  let currentNativeContext = options.initialLegacy ? legacyNativeContext : nativeContext;
  let nativeContextUnavailable = options.nativeContextUnavailable ?? false;
  let prepareLookupDenied = options.prepareLookupDenied ?? false;
  let prepareOriginal: CommandRequest | null = null;
  let confirmOriginal: CommandRequest | null = null;
  let confirmResult: CommandResponse | null = null;
  const commandLookups: string[] = [];
  server.use(
    http.get("/product/v1/session", () => HttpResponse.json(SESSION)),
    http.get(`${BASE}/worker-items/${WORKER_ITEM_ID}/workspace`, () => HttpResponse.json(pageWorkspace)),
    http.get(`${BASE}/assignments/worker_item/${WORKER_ITEM_ID}`, () => HttpResponse.json(sourceBinding)),
    http.get(`${BASE}/worker-items/${WORKER_ITEM_ID}/native-context`, () => nativeContextUnavailable
      ? HttpResponse.json({ detail: "native context unavailable" }, { status: 503 })
      : HttpResponse.json(currentNativeContext)),
    http.get(`/api/v1/tasks/${ROOT_TASK_ID}/graph`, () => HttpResponse.json({
      overview: { title: "Payment refactor" },
      nodes: [{ subtask_key: "callback-idempotency", title: "Callback idempotency" }],
    })),
    http.get(`${BASE}/tasks/${ROOT_TASK_ID}/results`, () => HttpResponse.json(resultView({
      workspace_id: WORKSPACE_ID,
      root_task_id: ROOT_TASK_ID,
      task_title: "Payment refactor",
    }))),
    http.get(`${BASE}/worker-items/${WORKER_ITEM_ID}/result-inputs`, () => HttpResponse.json(workerResultInputsFixture())),
    http.get(`${BASE}/worker-items/${WORKER_ITEM_ID}/activity`, () => HttpResponse.json({
      items: [],
      last_change_cursor: 5,
      next_cursor: null,
    })),
    http.get(`${BASE}/worker-items/${WORKER_ITEM_ID}/handoff-candidates`, () => {
      candidateGets += 1;
      return HttpResponse.json(options.initialLegacy
        ? cachedEligibleLegacyCandidates
        : { candidates: [], as_of: "2026-10-07T00:00:00Z" });
    }),
    http.get(`/agents/v1/workspaces/${WORKSPACE_ID}/profiles`, () => HttpResponse.json({ items: [sourceProfileR2] })),
    http.get(`/agents/v1/workspaces/${WORKSPACE_ID}/profiles/${sourceProfileRef.profile_id}`, ({ request }) => {
      const revision = new URL(request.url).searchParams.get("revision");
      profileRevisionReads.push(revision ?? "missing");
      if (revision === "1") return HttpResponse.json(sourceProfileR1);
      if (revision === "2") return HttpResponse.json(sourceProfileR2);
      return HttpResponse.json({}, { status: 404 });
    }),
    http.get(`/api/v1/executions/${EXECUTION_ID}`, () => HttpResponse.json({ status: "running" })),
    http.get(`${BASE}/handoff-proposals/${PROPOSAL_ID}`, () => currentProposal === null
      ? HttpResponse.json({}, { status: 404 })
      : HttpResponse.json(currentProposal)),
    http.get(`${BASE}/handoff-transitions/${TRANSITION_ID}`, () => currentTransition === null
      ? options.activeTransition === undefined
        ? HttpResponse.json({}, { status: 404 })
        : HttpResponse.json(options.activeTransition)
      : HttpResponse.json(currentTransition)),
    http.get(/\/api\/v1\/workspaces\/[^/]+\/commands\/[^/]+$/, ({ request }) => {
      const commandId = new URL(request.url).pathname.split("/").at(-1) ?? "";
      commandLookups.push(commandId);
      if (options.confirmReplyLost && confirmOriginal?.command_id === commandId && confirmResult !== null) {
        return HttpResponse.json({
          command_id: commandId,
          command_type: confirmOriginal.command_type,
          target_type: confirmOriginal.target_type,
          target_id: confirmOriginal.target_id,
          expected_version: confirmOriginal.expected_version,
          status: "applied",
          result: confirmResult,
        });
      }
      if (prepareLookupDenied && prepareOriginal?.command_id === commandId) {
        return HttpResponse.json({ detail: "original command lookup denied" }, { status: 403 });
      }
      if (options.prepareReplyLost && prepareOriginal?.command_id === commandId && currentProposal !== null) {
        return HttpResponse.json({
          command_id: commandId,
          command_type: prepareOriginal.command_type,
          target_type: prepareOriginal.target_type,
          target_id: prepareOriginal.target_id,
          expected_version: prepareOriginal.expected_version,
          status: "applied",
          result: {
            command_id: commandId,
            aggregate_id: currentProposal.id,
            aggregate_version: currentProposal.version,
            event_ids: [],
            data: { proposal: currentProposal },
          },
        });
      }
      return HttpResponse.json({ command_id: commandId, status: "pending", result: null });
    }),
    http.post("/api/v1/commands", async ({ request }) => {
      const command = await request.json() as CommandRequest;
      posts.push(command);
      if (command.command_type === "PrepareProfileHandoff") {
        prepareOriginal = command;
        const payload = command.payload as Record<string, unknown>;
        currentProposal = {
          id: PROPOSAL_ID,
          proposal_schema_version: 2,
          workspace_id: WORKSPACE_ID,
          root_task_id: ROOT_TASK_ID,
          subtask_id: SUBTASK_ID,
          worker_item_id: WORKER_ITEM_ID,
          source_worker_item_version: 7,
          version: 1,
          proposal_hash: "d".repeat(64),
          status: "prepared",
          stale_reason: null,
          created_at: "2026-10-07T00:00:00Z",
          expires_at: "2099-01-01T00:15:00Z",
          confirmed_at: null,
          cancelled_at: null,
          source_execution_id: EXECUTION_ID,
          source_session_id: SESSION_ID,
          source_run_id: RUN_ID,
          source_binding_version: 2,
          source_profile_ref: sourceProfileRef,
          target_assignee: payload.target_assignee as NativeHandoffProposal["target_assignee"],
          target_profile_ref: payload.target_profile_ref as NativeHandoffProposal["target_profile_ref"],
          target_worker_id: null,
          handoff_reason: payload.handoff_reason as string,
          handoff_instruction: payload.handoff_instruction as string,
          context: proposalContext,
          plan_base: planBase,
        };
        const response = {
          command_id: command.command_id,
          aggregate_id: currentProposal.id,
          aggregate_version: currentProposal.version,
          event_ids: [],
          data: { proposal: currentProposal },
        } satisfies CommandResponse;
        return options.prepareReplyLost ? HttpResponse.error() : HttpResponse.json(response);
      }

      if (command.command_type === "ConfirmProfileHandoff" && currentProposal !== null) {
        confirmOriginal = command;
        const confirmedProposal: NativeHandoffProposal = {
          ...currentProposal,
          version: 2,
          status: "confirmed",
          confirmed_at: "2026-10-07T00:02:00Z",
        };
        const resultTransition: NativeHandoffTransition = {
          id: TRANSITION_ID,
          handoff_schema_version: 2,
          workspace_id: WORKSPACE_ID,
          worker_item_id: WORKER_ITEM_ID,
          proposal_id: PROPOSAL_ID,
          version: 1,
          status: "pending",
          phase: "stop_source",
          writer_state: "source_stop_requested",
          confirmed_at: confirmedProposal.confirmed_at!,
          started_at: null,
          completed_at: null,
          failure_code: null,
          blocked_operation_id: null,
          logical_attempt: 1,
          next_attempt_at: null,
          source_execution_id: EXECUTION_ID,
          source_session_id: SESSION_ID,
          source_run_id: RUN_ID,
          source_binding_version: 2,
          source_profile_ref: sourceProfileRef,
          source_input_cutoff_sequence: 5,
          target_assignee: currentProposal.target_assignee,
          target_profile_ref: currentProposal.target_profile_ref,
          target_worker_id: null,
          target_execution_id: null,
          target_session_id: null,
          target_run_id: null,
          target_binding_version: null,
          target_context_version: 4,
          context: currentProposal.context,
          plan_base: currentProposal.plan_base,
          available_recovery_strategies: [],
        };
        currentProposal = confirmedProposal;
        currentTransition = options.confirmReplyLost
          ? { ...resultTransition, version: 2 }
          : resultTransition;
        confirmResult = {
          command_id: command.command_id,
          aggregate_id: resultTransition.id,
          aggregate_version: resultTransition.version,
          event_ids: [],
          data: { proposal: confirmedProposal, transition: resultTransition },
        } satisfies CommandResponse;
        return options.confirmReplyLost ? HttpResponse.error() : HttpResponse.json(confirmResult);
      }

      return HttpResponse.json({
        command_id: command.command_id,
        aggregate_id: WORKER_ITEM_ID,
        aggregate_version: 7,
        event_ids: [],
        data: {},
      } satisfies CommandResponse);
    }),
  );
  return {
    posts,
    getCandidateGets: () => candidateGets,
    getProfileRevisionReads: () => profileRevisionReads,
    getCommandLookups: () => commandLookups,
    getPrepareOriginal: () => prepareOriginal,
    getConfirmOriginal: () => confirmOriginal,
    closeSource: () => {
      const runtime = pageWorkspace.current_runtime;
      pageWorkspace = {
        ...pageWorkspace,
        current_runtime: runtime === null ? null : {
          ...runtime,
          execution_status: "stopped",
          session_status: "closed",
        },
      };
      nativeContextUnavailable = true;
    },
    switchToNativeWithActiveTransition: () => {
      pageWorkspace = { ...workspace, active_handoff: activeStopUnconfirmed };
      currentNativeContext = nativeContext;
      nativeContextUnavailable = false;
    },
    allowPrepareLookup: () => {
      prepareLookupDenied = false;
    },
  };
}

afterEach(() => {
  server.resetHandlers();
  localStorage.clear();
  document.cookie = "sidera_csrf=; Max-Age=0; Path=/";
});

describe("NativeWorkerHandoff", () => {
  it("prepares and confirms the native Profile-to-human path from an explicit WorkerItem choice", async () => {
    const handlers = installHandlers();
    document.cookie = "sidera_csrf=owned-csrf; Path=/";
    const user = userEvent.setup();
    renderPage();

    const picker = await screen.findByRole("combobox", { name: "设置归属" });
    await waitFor(() => expect(picker).toBeEnabled());
    expect(screen.queryByRole("button", { name: "开始原生交接" })).not.toBeInTheDocument();
    await user.selectOptions(picker, "human");
    await waitFor(() => expect(picker).toHaveValue("human"));

    await user.click(await screen.findByRole("button", { name: "开始原生交接" }));
    expect(await screen.findByRole("heading", { name: "原生 Profile 交接" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "交接目标" })).toHaveValue("human");
    expect(within(screen.getByLabelText("固定来源事实")).getByText(
      `${sourceProfileRef.profile_id} · R${sourceProfileRef.revision}`,
    )).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "当前进展" })).toHaveTextContent("Validate callback");
    expect(handlers.getCandidateGets()).toBe(0);
    expect(handlers.posts).toHaveLength(0);

    await user.type(screen.getByRole("textbox", { name: "交接原因" }), "Move work to current human");
    await user.type(screen.getByRole("textbox", { name: "交接指令" }), "Continue the callback validation");
    await user.click(screen.getByRole("button", { name: "准备原生交接" }));

    expect(await screen.findByRole("heading", { name: "复核原生交接方案" })).toBeInTheDocument();
    expect(handlers.posts).toHaveLength(1);
    expect(handlers.posts[0]).toMatchObject({
      command_type: "PrepareProfileHandoff",
      target_type: "worker_item",
      target_id: WORKER_ITEM_ID,
      expected_version: 7,
      payload: {
        target_assignee: { kind: "human", id: PRINCIPAL_ID },
        context: {
          worker_item_id: WORKER_ITEM_ID,
          execution_mode: "native_profile",
          execution_id: EXECUTION_ID,
          session_id: SESSION_ID,
          session_version: 3,
          worker_id: WORKER_ID,
          binding_version: 2,
          run_id: RUN_ID,
          plan_generation: 2,
          context_version: 4,
        },
      },
    });

    await user.click(screen.getByRole("button", { name: "确认交接" }));

    expect(await screen.findByRole("heading", { name: "原生交接进度" })).toBeInTheDocument();
    expect(screen.getByText("等待源 Agent 停止")).toBeInTheDocument();
    expect(screen.getByText("source_stop_requested")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "查看源 Trace" })).toHaveAttribute(
      "href",
      `/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}/executions/${EXECUTION_ID}/sessions/${SESSION_ID}/trace`,
    );
    expect(handlers.posts.map((command) => command.command_type)).toEqual([
      "PrepareProfileHandoff",
      "ConfirmProfileHandoff",
    ]);
    expect(handlers.posts[1]).toMatchObject({
      target_type: "handoff_proposal",
      target_id: PROPOSAL_ID,
      expected_version: 1,
    });
    expect(handlers.getCandidateGets()).toBe(0);
  });

  it("keeps source Profile R1 pinned while handing off to the current Profile R2", async () => {
    const handlers = installHandlers();
    document.cookie = "sidera_csrf=owned-csrf; Path=/";
    const user = userEvent.setup();
    renderPage();

    const picker = await screen.findByRole("combobox", { name: "设置归属" });
    await waitFor(() => expect(picker).toBeEnabled());
    await user.selectOptions(
      picker,
      screen.getByRole("option", { name: "Source Profile head R2 · R2" }),
    );
    await waitFor(() => expect(picker).toHaveDisplayValue("Source Profile head R2 · R2"));
    await user.click(await screen.findByRole("button", { name: "开始原生交接" }));

    const target = await screen.findByRole("combobox", { name: "交接目标" });
    expect(target).toHaveDisplayValue("Source Profile head R2 · R2");
    expect(within(screen.getByLabelText("固定来源事实")).getByText(
      `${sourceProfileRef.profile_id} · R${sourceProfileRef.revision}`,
    )).toBeInTheDocument();
    await user.type(screen.getByRole("textbox", { name: "交接原因" }), "Move to the current profile revision");
    await user.type(screen.getByRole("textbox", { name: "交接指令" }), "Continue the callback validation");
    await user.click(screen.getByRole("button", { name: "准备原生交接" }));

    expect(await screen.findByRole("heading", { name: "复核原生交接方案" })).toBeInTheDocument();
    expect(screen.getByText(`${sourceProfileRef.profile_id} · R1`, { selector: "strong" })).toBeInTheDocument();
    expect(screen.getByText("Source Profile head R2 · R2", { selector: "strong" })).toBeInTheDocument();
    expect(handlers.posts[0]).toMatchObject({
      command_type: "PrepareProfileHandoff",
      payload: {
        target_assignee: { kind: "profile", id: sourceProfileHeadRef.profile_id },
        target_profile_ref: sourceProfileHeadRef,
      },
    });
    expect(handlers.getProfileRevisionReads()).toContain("2");

    await user.click(screen.getByRole("button", { name: "确认交接" }));

    expect(await screen.findByRole("heading", { name: "原生交接进度" })).toBeInTheDocument();
    expect(within(screen.getByLabelText("原生交接参与者")).getByText(
      `${sourceProfileRef.profile_id} · R1`,
    )).toBeInTheDocument();
    expect(within(screen.getByLabelText("原生交接参与者")).getByText(
      `${sourceProfileHeadRef.profile_id} · R2`,
    )).toBeInTheDocument();
    expect(handlers.posts.map((command) => command.command_type)).toEqual([
      "PrepareProfileHandoff",
      "ConfirmProfileHandoff",
    ]);
    expect(handlers.getCandidateGets()).toBe(0);
  });

  it("keeps source-stop-unconfirmed progress readable with null target and unavailable native context", async () => {
    const handlers = installHandlers({
      activeTransition: sourceStopUnconfirmedTransition,
      nativeContextUnavailable: true,
    });
    renderPage();

    const progress = await screen.findByRole("region", { name: "原生交接进度" });
    expect(progress).toHaveTextContent("等待源 Agent 停止证据");
    expect(progress).toHaveTextContent("source_stop_requested");
    expect(progress).toHaveTextContent("source_stop_unconfirmed");
    expect(progress).toHaveTextContent("目标 Runtime 尚未接入");
    expect(screen.getByRole("link", { name: "查看源 Trace" })).toBeInTheDocument();
    expect(screen.queryByText("目标 Agent 启动结果未知")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /恢复|Recover/ })).not.toBeInTheDocument();
    expect(handlers.getCandidateGets()).toBe(0);
    expect(handlers.posts).toHaveLength(0);
  });

  it("does not expose the legacy handoff entry from cached candidates on a native source", async () => {
    const handlers = installHandlers();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
    client.setQueryData(
      handoffCandidatesQueryKey(WORKSPACE_ID, WORKER_ITEM_ID),
      cachedEligibleLegacyCandidates,
    );
    const user = userEvent.setup();
    renderPage(client);

    const picker = await screen.findByRole("combobox", { name: "设置归属" });
    await waitFor(() => expect(picker).toBeEnabled());
    expect(screen.queryByRole("button", { name: "调整执行 Agent" })).not.toBeInTheDocument();
    await user.selectOptions(picker, "human");
    await waitFor(() => expect(picker).toHaveValue("human"));
    expect(await screen.findByRole("button", { name: "开始原生交接" })).toBeInTheDocument();
    expect(handlers.getCandidateGets()).toBe(0);
    expect(handlers.posts).toHaveLength(0);
  });

  it("hides an open legacy writer and reveals native progress after canonical mode changes", async () => {
    const handlers = installHandlers({
      activeTransition: sourceStopUnconfirmedTransition,
      initialLegacy: true,
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
    const user = userEvent.setup();
    renderPage(client);

    const legacyWriter = await screen.findByRole("button", { name: "调整执行 Agent" });
    await user.click(legacyWriter);
    expect(await screen.findByRole("dialog", { name: "调整执行 Agent" })).toBeInTheDocument();
    expect(handlers.getCandidateGets()).toBe(1);

    handlers.switchToNativeWithActiveTransition();
    await Promise.all([
      client.invalidateQueries({
        queryKey: workerWorkspaceQueryKey(WORKSPACE_ID, WORKER_ITEM_ID),
        exact: true,
      }),
      client.invalidateQueries({
        queryKey: [...nativeWorkerContextQueryKey(WORKSPACE_ID, WORKER_ITEM_ID), PRINCIPAL_ID],
        exact: true,
      }),
    ]);

    const progress = await screen.findByRole("region", { name: "原生交接进度" });
    expect(progress).toHaveTextContent("等待源 Agent 停止证据");
    expect(progress).toHaveTextContent("source_stop_requested");
    expect(progress).toHaveTextContent("source_stop_unconfirmed");
    expect(screen.queryByRole("dialog", { name: "调整执行 Agent" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "调整执行 Agent" })).not.toBeInTheDocument();
    expect(handlers.getCandidateGets()).toBe(1);
    expect(handlers.posts).toHaveLength(0);
  });

  it("reopens a persisted lost Prepare from the record entry after the source closes", async () => {
    const handlers = installHandlers({ prepareReplyLost: true, prepareLookupDenied: true });
    document.cookie = "sidera_csrf=owned-csrf; Path=/";
    const user = userEvent.setup();
    const firstPage = renderPage();
    const picker = await screen.findByRole("combobox", { name: "设置归属" });
    await waitFor(() => expect(picker).toBeEnabled());
    await user.selectOptions(picker, "human");
    await waitFor(() => expect(picker).toHaveValue("human"));
    await user.click(await screen.findByRole("button", { name: "开始原生交接" }));
    const reason = await screen.findByRole("textbox", { name: "交接原因" });
    const instruction = screen.getByRole("textbox", { name: "交接指令" });
    await waitFor(() => expect(reason).toBeEnabled());
    await user.type(reason, "Move work to current human");
    await user.type(instruction, "Continue the callback validation");
    const prepare = screen.getByRole("button", { name: "准备原生交接" });
    await waitFor(() => expect(prepare).toBeEnabled());
    await user.click(prepare);

    await waitFor(() => expect(handlers.posts).toHaveLength(1));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(JSON.parse(localStorage.getItem(handoffDraftKey(handoffScope)) ?? "null")).toMatchObject({
      actions: { prepare: { status: "unknown" } },
      reason: "Move work to current human",
      instruction: "Continue the callback validation",
    });
    await screen.findByRole("button", { name: "查询原始操作" });
    const original = handlers.getPrepareOriginal();
    expect(original).not.toBeNull();
    expect(handlers.posts).toHaveLength(1);
    expect(JSON.parse(localStorage.getItem(handoffDraftKey(handoffScope)) ?? "null")).toMatchObject({
      actions: { prepare: { original, status: "unknown" } },
      reason: "Move work to current human",
      instruction: "Continue the callback validation",
    });

    await user.click(screen.getByRole("button", { name: "关闭原生交接" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    handlers.closeSource();
    firstPage.unmount();

    renderPage();
    const recordEntry = await screen.findByRole("button", { name: "查看交接记录" });
    await user.click(recordEntry);
    expect(await screen.findByRole("heading", { name: "原生 Profile 交接" })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "查询原始操作" })).toBeEnabled();
    expect(screen.getByRole("textbox", { name: "交接原因" })).toHaveValue("Move work to current human");
    expect(screen.getByRole("textbox", { name: "交接指令" })).toHaveValue("Continue the callback validation");
    await user.click(screen.getByRole("button", { name: "查询原始操作" }));
    expect(await within(screen.getByRole("dialog")).findByRole("alert")).toHaveTextContent(
      "原始操作状态尚未确认，请重试读取",
    );
    expect(JSON.parse(localStorage.getItem(handoffDraftKey(handoffScope)) ?? "null")).toMatchObject({
      actions: { prepare: { original, status: "unknown" } },
    });
    expect(handlers.posts).toHaveLength(1);

    handlers.allowPrepareLookup();
    await waitFor(() => expect(screen.getByRole("button", { name: "查询原始操作" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "查询原始操作" }));

    await screen.findByRole("heading", { name: "复核原生交接方案" });
    expect(handlers.getCommandLookups()).toEqual([original?.command_id, original?.command_id]);
    expect(handlers.posts).toHaveLength(1);
    expect(JSON.parse(localStorage.getItem(handoffDraftKey(handoffScope)) ?? "null")).toMatchObject({
      actions: { prepare: { original, status: "applied" } },
      prepared: { proposal: { id: PROPOSAL_ID, version: 1, status: "prepared" } },
    });
    expect(screen.queryByRole("button", { name: "确认交接" })).not.toBeInTheDocument();
  });

  it("recovers a lost Confirm from the same record entry without replacing prepared V1", async () => {
    const handlers = installHandlers({ confirmReplyLost: true });
    document.cookie = "sidera_csrf=owned-csrf; Path=/";
    const user = userEvent.setup();
    const firstPage = renderPage();
    const picker = await screen.findByRole("combobox", { name: "设置归属" });
    await waitFor(() => expect(picker).toBeEnabled());
    await user.selectOptions(picker, "human");
    await waitFor(() => expect(picker).toHaveValue("human"));
    await user.click(await screen.findByRole("button", { name: "开始原生交接" }));
    const reason = await screen.findByRole("textbox", { name: "交接原因" });
    const instruction = screen.getByRole("textbox", { name: "交接指令" });
    await waitFor(() => expect(reason).toBeEnabled());
    await user.type(reason, "Move work to current human");
    await user.type(instruction, "Continue the callback validation");
    const prepare = screen.getByRole("button", { name: "准备原生交接" });
    await waitFor(() => expect(prepare).toBeEnabled());
    await user.click(prepare);
    await screen.findByRole("heading", { name: "复核原生交接方案" });

    const preparedBeforeConfirm = JSON.parse(localStorage.getItem(handoffDraftKey(handoffScope)) ?? "null");
    expect(preparedBeforeConfirm).toMatchObject({
      prepared: { proposal: { id: PROPOSAL_ID, version: 1, status: "prepared" } },
      actions: { prepare: { status: "applied" } },
    });
    const confirm = screen.getByRole("button", { name: "确认交接" });
    await waitFor(() => expect(confirm).toBeEnabled());
    await user.click(confirm);
    await waitFor(() => expect(JSON.parse(localStorage.getItem(handoffDraftKey(handoffScope)) ?? "null"))
      .toMatchObject({ actions: { confirm: { status: "unknown" } } }));

    const original = handlers.getConfirmOriginal();
    expect(original).not.toBeNull();
    expect(original).toMatchObject({
      command_type: "ConfirmProfileHandoff",
      target_type: "handoff_proposal",
      target_id: PROPOSAL_ID,
      expected_version: 1,
    });
    const storedBeforeReload = JSON.parse(localStorage.getItem(handoffDraftKey(handoffScope)) ?? "null");
    expect(storedBeforeReload.prepared).toEqual(preparedBeforeConfirm.prepared);
    expect(storedBeforeReload.actions.confirm).toMatchObject({ original, status: "unknown" });
    expect(handlers.posts.map((command) => command.command_type)).toEqual([
      "PrepareProfileHandoff",
      "ConfirmProfileHandoff",
    ]);

    await user.click(screen.getByRole("button", { name: "关闭原生交接" }));
    handlers.closeSource();
    firstPage.unmount();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "查看交接记录" }));
    await user.click(await screen.findByRole("button", { name: "查询原始操作" }));

    const progress = await screen.findByRole("region", { name: "原生交接进度" });
    expect(progress).toHaveTextContent("Transition V2");
    expect(screen.getByLabelText("原始命令结果")).toHaveTextContent("Confirm：命令已处理");
    expect(handlers.getCommandLookups()).toEqual([original?.command_id]);
    expect(handlers.posts.map((command) => command.command_type)).toEqual([
      "PrepareProfileHandoff",
      "ConfirmProfileHandoff",
    ]);
    const recovered = JSON.parse(localStorage.getItem(handoffDraftKey(handoffScope)) ?? "null");
    expect(recovered.prepared).toEqual(preparedBeforeConfirm.prepared);
    expect(recovered.actions.confirm).toMatchObject({ original, status: "applied" });
    expect(recovered.transition_id).toBe(TRANSITION_ID);
  });
});
