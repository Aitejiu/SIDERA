import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { createElement, type PropsWithChildren } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PROFILE, PROFILE_REF } from "@/features/agents/testFixtures";
import type { LocalSession } from "@/features/local-session/api";
import { server } from "@/test/server";
import type {
  AgentBindingView,
  CommandRequest,
  CommandResponse,
  NativeWorkerContextView,
} from "./types";
import { handoffDraftKey, type HandoffScope } from "./profileHandoff";
import type {
  NativeHandoffProposal,
  NativeHandoffTransition,
  WorkerWorkspace,
} from "@/features/worker-items/types";
import { handoffTransitionQueryKey } from "@/features/worker-items/api";
import { ProfileHandoffDialog } from "./ProfileHandoffDialog";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const WORKER_ITEM_ID = "40000000-0000-0000-0000-000000000001";
const ROOT_TASK_ID = "30000000-0000-0000-0000-000000000001";
const SUBTASK_ID = "30000000-0000-0000-0000-000000000002";
const EXECUTION_ID = "60000000-0000-0000-0000-000000000001";
const SESSION_ID = "70000000-0000-0000-0000-000000000001";
const WORKER_ID = "80000000-0000-0000-0000-000000000001";
const RUN_ID = "90000000-0000-0000-0000-000000000001";
const PROPOSAL_ID = "a0000000-0000-0000-0000-000000000001";
const TRANSITION_ID = "b0000000-0000-0000-0000-000000000001";
const PRINCIPAL_ID = "principal-current";
const BASE = `/api/v1/workspaces/${WORKSPACE_ID}`;
const SCOPE: HandoffScope = {
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
const sourceProfileRef = {
  workspace_id: WORKSPACE_ID,
  profile_id: "50000000-0000-0000-0000-000000000002",
  revision: 1,
};
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
  current_step: "Validate proposal",
  next_action: "Continue validation",
  latest_checkpoint: null,
  usage: { input_tokens: 0, output_tokens: 0, cached_tokens: 0, cost_microunits: 0 },
  artifacts: [],
  evidence: [],
  can_intervene: false,
  read_only_reason: null,
  intervention_context: null,
  trace_descriptor: null,
  last_change_cursor: 0,
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
const currentHuman = { kind: "human" as const, id: PRINCIPAL_ID, profileRef: null };
const proposalContext = {
  source_package_id: "b0000000-0000-0000-0000-000000000011",
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
const proposal: NativeHandoffProposal = {
  id: PROPOSAL_ID,
  proposal_schema_version: 2,
  workspace_id: WORKSPACE_ID,
  root_task_id: ROOT_TASK_ID,
  subtask_id: SUBTASK_ID,
  worker_item_id: WORKER_ITEM_ID,
  source_worker_item_version: 7,
  version: 1,
  proposal_hash: "a".repeat(64),
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
  target_assignee: { kind: currentHuman.kind, id: currentHuman.id },
  target_profile_ref: null,
  target_worker_id: null,
  handoff_reason: "Transfer to current human",
  handoff_instruction: "Continue the validation step",
  context: proposalContext,
  plan_base: planBase,
};
const confirmedProposal: NativeHandoffProposal = {
  ...proposal,
  version: 2,
  status: "confirmed",
  confirmed_at: "2026-10-07T00:02:00Z",
};
const transition: NativeHandoffTransition = {
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
  target_assignee: { kind: currentHuman.kind, id: currentHuman.id },
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

function installHandlers() {
  const posts: CommandRequest[] = [];
  let candidateGets = 0;
  let canonicalTransition = transition;
  let sessionMismatch = false;
  server.use(
    http.get("/product/v1/session", () => HttpResponse.json(sessionMismatch
      ? { ...SESSION, principal_id: "principal-other" }
      : SESSION)),
    http.get(`${BASE}/worker-items/${WORKER_ITEM_ID}/workspace`, () => HttpResponse.json(workspace)),
    http.get(`${BASE}/assignments/worker_item/${WORKER_ITEM_ID}`, () => HttpResponse.json(sourceBinding)),
    http.get(`${BASE}/worker-items/${WORKER_ITEM_ID}/native-context`, () => HttpResponse.json(nativeContext)),
    http.get(`${BASE}/worker-items/${WORKER_ITEM_ID}/handoff-candidates`, () => {
      candidateGets += 1;
      return HttpResponse.json({ candidates: [], as_of: "2026-10-07T00:00:00Z" });
    }),
    http.get(`/agents/v1/workspaces/${WORKSPACE_ID}/profiles`, () => HttpResponse.json({ items: [PROFILE] })),
    http.get(`/agents/v1/workspaces/${WORKSPACE_ID}/profiles/${PROFILE_REF.profile_id}`, ({ request }) => {
      const revision = new URL(request.url).searchParams.get("revision");
      return revision === String(PROFILE_REF.revision)
        ? HttpResponse.json(PROFILE)
        : HttpResponse.json({}, { status: 404 });
    }),
    http.get(`${BASE}/handoff-proposals/${PROPOSAL_ID}`, () => HttpResponse.json(proposal)),
    http.get(`${BASE}/handoff-transitions/${TRANSITION_ID}`, () => HttpResponse.json(canonicalTransition)),
    http.get(/\/api\/v1\/workspaces\/[^/]+\/commands\/[^/]+$/, ({ request }) => {
      const commandId = new URL(request.url).pathname.split("/").at(-1) ?? "";
      return HttpResponse.json({ command_id: commandId, status: "pending", result: null });
    }),
    http.post("/api/v1/commands", async ({ request }) => {
      const command = await request.json() as CommandRequest;
      posts.push(command);
      if (command.command_type === "PrepareProfileHandoff") {
        return HttpResponse.json({
          command_id: command.command_id,
          aggregate_id: proposal.id,
          aggregate_version: proposal.version,
          event_ids: [],
          data: { proposal },
        } satisfies CommandResponse);
      }
      return HttpResponse.json({
        command_id: command.command_id,
        aggregate_id: transition.id,
        aggregate_version: transition.version,
        event_ids: [],
        data: { proposal: confirmedProposal, transition },
      } satisfies CommandResponse);
    }),
  );
  return {
    posts,
    getCandidateGets: () => candidateGets,
    setCanonicalTransition: (next: NativeHandoffTransition) => { canonicalTransition = next; },
    setSessionMismatch: (next: boolean) => { sessionMismatch = next; },
  };
}

function renderDialog(onClose = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  function Provider({ children }: PropsWithChildren) {
    return createElement(QueryClientProvider, { client }, children);
  }
  const view = render(
    <ProfileHandoffDialog
      workspace={workspace}
      taskTitle="Payment refactor"
      subtaskTitle="Callback idempotency"
      initialTarget={{ kind: "profile", id: PROFILE_REF.profile_id, profileRef: PROFILE_REF }}
      onRefresh={async () => undefined}
      onClose={onClose}
    />,
    { wrapper: Provider },
  );
  return { ...view, onClose, client };
}

afterEach(() => {
  server.resetHandlers();
  localStorage.clear();
  document.cookie = "sidera_csrf=; Max-Age=0; Path=/";
});

describe("ProfileHandoffDialog", () => {
  it("prepares a Profile-to-human handoff and shows separate native progress facts", async () => {
    const handlers = installHandlers();
    document.cookie = "sidera_csrf=owned-csrf; Path=/";
    const user = userEvent.setup();
    renderDialog();

    const target = await screen.findByRole("combobox", { name: "交接目标" });
    await waitFor(() => expect(target).toBeEnabled());
    await user.selectOptions(target, screen.getByRole("option", { name: "当前用户" }));
    await waitFor(() => expect(target).toHaveValue("human"));
    await user.type(screen.getByRole("textbox", { name: "交接原因" }), "Transfer to current human");
    await user.type(screen.getByRole("textbox", { name: "交接指令" }), "Continue the validation step");
    await user.click(screen.getByRole("button", { name: "准备原生交接" }));

    expect(await screen.findByRole("heading", { name: "复核原生交接方案" })).toBeInTheDocument();
    expect(within(screen.getByLabelText("固定来源事实")).getByText(
      `${sourceProfileRef.profile_id} · R${sourceProfileRef.revision}`,
    )).toBeInTheDocument();
    expect(screen.getByText("当前用户", { selector: "strong" })).toBeInTheDocument();
    expect(screen.getByText("Transfer to current human")).toBeInTheDocument();
    expect(handlers.posts).toHaveLength(1);
    expect(handlers.posts[0]).toMatchObject({
      command_type: "PrepareProfileHandoff",
      target_type: "worker_item",
      target_id: WORKER_ITEM_ID,
      expected_version: 7,
      payload: {
        context: {
          worker_item_id: WORKER_ITEM_ID,
          execution_id: EXECUTION_ID,
          session_id: SESSION_ID,
          run_id: RUN_ID,
          binding_version: 2,
          context_version: 4,
        },
        target_assignee: { kind: "human", id: PRINCIPAL_ID },
      },
    });
    expect(JSON.parse(localStorage.getItem(handoffDraftKey(SCOPE)) ?? "null")).toMatchObject({
      reason: "Transfer to current human",
      instruction: "Continue the validation step",
      actions: { prepare: { status: "applied", original: handlers.posts[0] } },
    });

    await user.click(screen.getByRole("button", { name: "确认交接" }));

    expect(await screen.findByRole("heading", { name: "原生交接进度" })).toBeInTheDocument();
    expect(screen.getByText("source_stop_requested")).toBeInTheDocument();
    expect(within(screen.getByLabelText("原生交接参与者")).getByText(
      `${sourceProfileRef.profile_id} · R${sourceProfileRef.revision}`,
    )).toBeInTheDocument();
    expect(screen.getByText(/目标 Runtime 尚未接入；这不表示目标已启动或启动失败/)).toBeInTheDocument();
    expect(handlers.posts).toHaveLength(2);
    expect(handlers.posts[1]).toMatchObject({
      command_type: "ConfirmProfileHandoff",
      target_type: "handoff_proposal",
      target_id: PROPOSAL_ID,
      expected_version: 1,
    });
    expect(handlers.getCandidateGets()).toBe(0);
    expect(screen.queryByRole("button", { name: /Recover|恢复/ })).not.toBeInTheDocument();
  });

  it("closes without implicitly cancelling an already prepared proposal", async () => {
    const handlers = installHandlers();
    document.cookie = "sidera_csrf=owned-csrf; Path=/";
    const user = userEvent.setup();
    const dialog = renderDialog();
    const target = await screen.findByRole("combobox", { name: "交接目标" });
    await waitFor(() => expect(target).toBeEnabled());
    await user.selectOptions(target, screen.getByRole("option", { name: "当前用户" }));
    await waitFor(() => expect(target).toHaveValue("human"));
    await user.type(screen.getByRole("textbox", { name: "交接原因" }), "Transfer to current human");
    await user.type(screen.getByRole("textbox", { name: "交接指令" }), "Continue the validation step");
    await user.click(screen.getByRole("button", { name: "准备原生交接" }));
    await screen.findByRole("heading", { name: "复核原生交接方案" });

    await user.keyboard("{Escape}");

    expect(dialog.onClose).toHaveBeenCalledTimes(1);
    expect(handlers.posts.map((command) => command.command_type)).toEqual(["PrepareProfileHandoff"]);
  });

  it("offers a new handoff only after convergence and preserves progress on session mismatch", async () => {
    const handlers = installHandlers();
    document.cookie = "sidera_csrf=owned-csrf; Path=/";
    const user = userEvent.setup();
    const dialog = renderDialog();
    const target = await screen.findByRole("combobox", { name: "交接目标" });
    await waitFor(() => expect(target).toBeEnabled());
    await user.selectOptions(target, screen.getByRole("option", { name: "当前用户" }));
    await user.type(screen.getByRole("textbox", { name: "交接原因" }), "Transfer to current human");
    await user.type(screen.getByRole("textbox", { name: "交接指令" }), "Continue the validation step");
    await user.click(screen.getByRole("button", { name: "准备原生交接" }));
    await screen.findByRole("heading", { name: "复核原生交接方案" });
    await user.click(screen.getByRole("button", { name: "确认交接" }));

    const progress = await screen.findByRole("region", { name: "原生交接进度" });
    expect(handlers.posts.map((command) => command.command_type)).toEqual([
      "PrepareProfileHandoff",
      "ConfirmProfileHandoff",
    ]);
    expect(screen.queryByRole("button", { name: "开始新的交接" })).not.toBeInTheDocument();

    const running: NativeHandoffTransition = {
      ...transition,
      version: 2,
      status: "running",
      phase: "freeze_source_workspace",
      writer_state: "writer_none",
      started_at: "2026-10-07T00:03:00Z",
    };
    handlers.setCanonicalTransition(running);
    await Promise.all([
      dialog.client.invalidateQueries({
        queryKey: ["native-profile-handoff-transition", WORKSPACE_ID, PRINCIPAL_ID, WORKER_ITEM_ID, TRANSITION_ID],
        exact: true,
      }),
      dialog.client.invalidateQueries({ queryKey: handoffTransitionQueryKey(WORKSPACE_ID, TRANSITION_ID), exact: true }),
    ]);
    await waitFor(() => expect(progress).toHaveTextContent("running"));
    expect(screen.queryByRole("button", { name: "开始新的交接" })).not.toBeInTheDocument();

    const unknown: NativeHandoffTransition = {
      ...running,
      version: 3,
      status: "blocked_unknown",
      phase: "freeze_source_workspace",
      writer_state: "unknown",
      failure_code: "workspace_freeze_unknown",
      blocked_operation_id: "c0000000-0000-0000-0000-000000000001",
    };
    handlers.setCanonicalTransition(unknown);
    await Promise.all([
      dialog.client.invalidateQueries({
        queryKey: ["native-profile-handoff-transition", WORKSPACE_ID, PRINCIPAL_ID, WORKER_ITEM_ID, TRANSITION_ID],
        exact: true,
      }),
      dialog.client.invalidateQueries({ queryKey: handoffTransitionQueryKey(WORKSPACE_ID, TRANSITION_ID), exact: true }),
    ]);
    await waitFor(() => expect(progress).toHaveTextContent("blocked_unknown"));
    expect(screen.queryByRole("button", { name: "开始新的交接" })).not.toBeInTheDocument();

    const converged: NativeHandoffTransition = {
      ...running,
      version: 4,
      status: "converged",
      phase: "complete",
      writer_state: "writer_none",
      target_binding_version: 3,
      failure_code: null,
      blocked_operation_id: null,
      completed_at: "2026-10-07T00:04:00Z",
    };
    handlers.setCanonicalTransition(converged);
    await Promise.all([
      dialog.client.invalidateQueries({
        queryKey: ["native-profile-handoff-transition", WORKSPACE_ID, PRINCIPAL_ID, WORKER_ITEM_ID, TRANSITION_ID],
        exact: true,
      }),
      dialog.client.invalidateQueries({ queryKey: handoffTransitionQueryKey(WORKSPACE_ID, TRANSITION_ID), exact: true }),
    ]);

    const startNew = await screen.findByRole("button", { name: "开始新的交接" });
    handlers.setSessionMismatch(true);
    await user.click(startNew);
    expect(await within(screen.getByRole("dialog", { name: "原生交接进度" })).findByRole("alert")).toHaveTextContent(
      "需要重新确认当前用户会话",
    );
    expect(screen.getByRole("button", { name: "本地会话" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "原生交接进度" })).toBeInTheDocument();
    expect(JSON.parse(localStorage.getItem(handoffDraftKey(SCOPE)) ?? "null")).toMatchObject({
      transition_id: TRANSITION_ID,
      prepared: { proposal: { id: PROPOSAL_ID, version: 1, status: "prepared" } },
      actions: { confirm: { status: "applied" } },
    });
    expect(handlers.posts).toHaveLength(2);

    await user.click(screen.getByRole("button", { name: "本地会话" }));
    expect(await screen.findByRole("dialog", { name: "本地会话" })).toBeInTheDocument();
    expect(screen.getByText("principal-other")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "关闭本地会话" }));
    handlers.setSessionMismatch(false);
    await user.click(screen.getByRole("button", { name: "开始新的交接" }));

    expect(await screen.findByRole("heading", { name: "原生 Profile 交接" })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "交接原因" })).toHaveValue("");
    expect(screen.getByRole("textbox", { name: "交接指令" })).toHaveValue("");
    expect(screen.getByRole("button", { name: "准备原生交接" })).toBeDisabled();
    expect(handlers.posts).toHaveLength(2);
    expect(dialog.onClose).not.toHaveBeenCalled();
  });
});
