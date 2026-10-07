import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { createElement, type PropsWithChildren } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PROFILE, PROFILE_REF } from "@/features/agents/testFixtures";
import type { LocalSession } from "@/features/local-session/api";
import { server } from "@/test/server";
import type {
  AgentBindingView,
  AssigneeValue,
  CommandRequest,
  CommandResponse,
  NativeWorkerContextView,
} from "./types";
import type {
  NativeHandoffProposal,
  NativeHandoffTransition,
  WorkerWorkspace,
} from "@/features/worker-items/types";
import {
  handoffDraftKey,
  type HandoffScope,
} from "./profileHandoff";
import { nativeWorkerContextQueryKey } from "./api";
import { useProfileHandoff } from "./useProfileHandoff";

const workspaceId = "20000000-0000-0000-0000-000000000001";
const workerItemId = "40000000-0000-0000-0000-000000000001";
const rootTaskId = "30000000-0000-0000-0000-000000000001";
const subtaskId = "30000000-0000-0000-0000-000000000002";
const executionId = "60000000-0000-0000-0000-000000000001";
const sessionId = "70000000-0000-0000-0000-000000000001";
const workerId = "80000000-0000-0000-0000-000000000001";
const runId = "90000000-0000-0000-0000-000000000001";
const proposalId = "a0000000-0000-0000-0000-000000000001";
const transitionId = "b0000000-0000-0000-0000-000000000001";
const principalId = "principal-current";
const workerItemBId = "40000000-0000-0000-0000-000000000002";
const base = `/api/v1/workspaces/${workspaceId}`;
const scope: HandoffScope = { workspace_id: workspaceId, principal_id: principalId, worker_item_id: workerItemId };
const scopeB: HandoffScope = { ...scope, worker_item_id: workerItemBId };
const session: LocalSession = {
  session_id: "50000000-0000-0000-0000-000000000001",
  workspace_id: workspaceId,
  principal_id: principalId,
  expires_at: "2099-01-01T00:00:00Z",
};
const sourceProfileRef = {
  workspace_id: workspaceId,
  profile_id: "50000000-0000-0000-0000-000000000002",
  revision: 1,
};
const sourceBinding: AgentBindingView = {
  workspace_id: workspaceId,
  target_type: "worker_item",
  target_id: workerItemId,
  assignee: { kind: "profile", id: sourceProfileRef.profile_id },
  profile_ref: sourceProfileRef,
  version: 2,
  runnable: true,
  not_runnable_reasons: [],
  created_at: "2026-10-07T00:00:00Z",
  changed_at: "2026-10-07T00:00:00Z",
};
const sourceBindingB: AgentBindingView = { ...sourceBinding, target_id: workerItemBId };
const workspace: WorkerWorkspace = {
  workspace_id: workspaceId,
  worker_item: {
    id: workerItemId,
    stable_key: "native-worker",
    title: "Native worker",
    responsibility: "Implement the callback contract.",
    root_task_id: rootTaskId,
    status: "active",
    context_version: 4,
    acknowledged_context_version: 4,
    version: 7,
    updated_at: "2026-10-07T00:00:00Z",
  },
  current_placement: {
    worker_item_key: "native-worker",
    subtask_key: "callback-idempotency",
    subtask_task_id: subtaskId,
    role: "implementer",
    capability_requirements: [],
    plan_generation: 2,
    plan_snapshot_id: "10000000-0000-0000-0000-000000000001",
  },
  placement_history: [],
  current_runtime: {
    adapter_type: "agent-run",
    attempt: 1,
    execution_id: executionId,
    execution_status: "running",
    fencing_token: 2,
    input_mode: "queued",
    lease_id: "lease-1",
    session_id: sessionId,
    session_status: "active",
    session_version: 3,
    task_id: subtaskId,
    worker_display_name: "Source Profile R1",
    worker_id: workerId,
  },
  execution_history: [{
    execution_id: executionId,
    task_id: subtaskId,
    status: "running",
    attempt: 1,
    worker_id: workerId,
    session_id: sessionId,
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
    assigned_worker_id: workerId,
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
};
const nativeContext: NativeWorkerContextView = {
  workspace_id: workspaceId,
  worker_item_id: workerItemId,
  execution_mode: "native_profile",
  worker_item_version: 7,
  binding: sourceBinding,
  execution: {
    workspace_id: workspaceId,
    execution_id: executionId,
    worker_item_id: workerItemId,
    root_task_id: rootTaskId,
    subtask_id: subtaskId,
    profile_ref: sourceProfileRef,
    binding_version: 2,
    run_id: runId,
    event_cursor: 5,
    status: "running",
    link_status: "accepted",
    updated_at: "2026-10-07T00:00:00Z",
  },
  execution_version: 5,
  context: {
    worker_item_id: workerItemId,
    execution_mode: "native_profile",
    execution_id: executionId,
    session_id: sessionId,
    session_version: 3,
    worker_id: workerId,
    binding_version: 2,
    run_id: runId,
    plan_generation: 2,
    context_version: 4,
  },
  input_enabled: false,
  read_only_reason: "input_not_available",
  run: {
    state: "running",
    input_open: false,
    profile_ref: sourceProfileRef,
    runtime_ref: { workspace_id: workspaceId, runtime_id: "b0000000-0000-0000-0000-000000000003" },
    actual_runtime_version: "1",
    applied_fields: [],
    config_hash: "c".repeat(64),
    observed_model: null,
    reason_code: null,
    selected_model: null,
  },
  as_of: "2026-10-07T00:00:00Z",
};
const workspaceB: WorkerWorkspace = {
  ...workspace,
  worker_item: { ...workspace.worker_item, id: workerItemBId },
};
const nativeContextB: NativeWorkerContextView = {
  ...nativeContext,
  worker_item_id: workerItemBId,
  binding: sourceBindingB,
  execution: { ...nativeContext.execution!, worker_item_id: workerItemBId },
  context: { ...nativeContext.context!, worker_item_id: workerItemBId },
};
const contextAddress = {
  worker_item_id: workerItemId,
  execution_mode: "native_profile",
  execution_id: executionId,
  session_id: sessionId,
  session_version: 3,
  worker_id: workerId,
  binding_version: 2,
  run_id: runId,
  plan_generation: 2,
  context_version: 4,
};
const planBase = {
  snapshot_id: workspace.current_placement.plan_snapshot_id,
  generation: 2,
  head_version: 6,
};
const context = {
  source_package_id: "b0000000-0000-0000-0000-000000000001",
  source_package_hash: "b".repeat(64),
  source_context_version: 3,
  target_package_id: null,
  target_package_hash: null,
  target_context_version: 4,
};
const profileTarget = {
  kind: "profile" as const,
  id: PROFILE_REF.profile_id,
  profileRef: PROFILE_REF,
};
const humanTarget = { kind: "human" as const, id: principalId, profileRef: null };

function proposalFor(target = profileTarget): NativeHandoffProposal {
  return {
    id: proposalId,
    proposal_schema_version: 2,
    workspace_id: workspaceId,
    root_task_id: rootTaskId,
    subtask_id: subtaskId,
    worker_item_id: workerItemId,
    source_worker_item_version: 7,
    version: 1,
    proposal_hash: "a".repeat(64),
    status: "prepared",
    stale_reason: null,
    created_at: "2026-10-07T00:00:00Z",
    expires_at: "2099-01-01T00:15:00Z",
    confirmed_at: null,
    cancelled_at: null,
    source_execution_id: executionId,
    source_session_id: sessionId,
    source_run_id: runId,
    source_binding_version: 2,
    source_profile_ref: sourceProfileRef,
    target_assignee: { kind: target.kind, id: target.id },
    target_profile_ref: target.kind === "profile" ? target.profileRef : null,
    target_worker_id: null,
    handoff_reason: "Change the implementation owner",
    handoff_instruction: "Continue the validation step",
    context,
    plan_base: planBase,
  };
}

const proposal = proposalFor();
const confirmedProposal = {
  ...proposal,
  version: 2,
  status: "confirmed",
  confirmed_at: "2026-10-07T00:02:00Z",
} satisfies NativeHandoffProposal;
const transition: NativeHandoffTransition = {
  id: transitionId,
  handoff_schema_version: 2,
  workspace_id: workspaceId,
  worker_item_id: workerItemId,
  proposal_id: proposalId,
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
  source_execution_id: executionId,
  source_session_id: sessionId,
  source_run_id: runId,
  source_binding_version: 2,
  source_profile_ref: sourceProfileRef,
  source_input_cutoff_sequence: 5,
  target_assignee: proposal.target_assignee,
  target_profile_ref: proposal.target_profile_ref,
  target_worker_id: null,
  target_execution_id: null,
  target_session_id: null,
  target_run_id: null,
  target_binding_version: null,
  target_context_version: 4,
  context,
  plan_base: planBase,
  available_recovery_strategies: [],
};

function wrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  function Provider({ children }: PropsWithChildren) {
    return createElement(QueryClientProvider, { client }, children);
  }
  return { client, Provider };
}

function installReads() {
  let proposalReads = 0;
  const commandReads: string[] = [];
  server.use(
    http.get("/product/v1/session", () => HttpResponse.json(session)),
    http.get(`${base}/worker-items/${workerItemId}/workspace`, () => HttpResponse.json(workspace)),
    http.get(`${base}/assignments/worker_item/${workerItemId}`, () => HttpResponse.json(sourceBinding)),
    http.get(`${base}/worker-items/${workerItemId}/native-context`, () => HttpResponse.json(nativeContext)),
    http.get(`${base}/worker-items/${workerItemBId}/workspace`, () => HttpResponse.json(workspaceB)),
    http.get(`${base}/assignments/worker_item/${workerItemBId}`, () => HttpResponse.json(sourceBindingB)),
    http.get(`${base}/worker-items/${workerItemBId}/native-context`, () => HttpResponse.json(nativeContextB)),
    http.get(`/agents/v1/workspaces/${workspaceId}/profiles`, () => HttpResponse.json({ items: [PROFILE] })),
    http.get(`/agents/v1/workspaces/${workspaceId}/profiles/${PROFILE_REF.profile_id}`, ({ request }) => {
      const revision = new URL(request.url).searchParams.get("revision");
      return revision === String(PROFILE_REF.revision)
        ? HttpResponse.json(PROFILE)
        : HttpResponse.json({}, { status: 404 });
    }),
    http.get(`${base}/handoff-proposals/${proposalId}`, () => {
      proposalReads += 1;
      return HttpResponse.json(proposal);
    }),
    http.get(`${base}/handoff-transitions/${transitionId}`, () => HttpResponse.json(transition)),
    http.get(/\/api\/v1\/workspaces\/[^/]+\/commands\/[^/]+$/, ({ request }) => {
      const commandId = new URL(request.url).pathname.split("/").at(-1) ?? "";
      commandReads.push(commandId);
      return HttpResponse.json({ command_id: commandId, status: "pending", result: null });
    }),
  );
  return { getProposalReads: () => proposalReads, getCommandReads: () => commandReads };
}

function mountHook(
  initialTarget: AssigneeValue | null = profileTarget,
  onRefresh: () => Promise<void> = vi.fn(async () => undefined),
  initialWorkspace: WorkerWorkspace = workspace,
) {
  const query = wrapper();
  const hook = renderHook((props: {
    currentWorkspace: WorkerWorkspace;
    target: AssigneeValue | null;
  }) => useProfileHandoff({
    workspace: props.currentWorkspace,
    initialTarget: props.target,
    onRefresh,
  }), {
    initialProps: { currentWorkspace: initialWorkspace, target: initialTarget },
    wrapper: query.Provider,
  });
  return { ...query, ...hook, onRefresh };
}

afterEach(() => {
  server.resetHandlers();
  localStorage.clear();
  document.cookie = "sidera_csrf=; Max-Age=0; Path=/";
  vi.restoreAllMocks();
});

describe("useProfileHandoff", () => {
  it("allows target editing after source review before Prepare fields are complete", async () => {
    installReads();
    const view = mountHook(null);

    await waitFor(() => expect(view.result.current.sourceReview).not.toBeNull());

    expect(view.result.current.canEditTarget).toBe(true);
    expect(view.result.current.canPrepare).toBe(false);
  });

  it("updates the scoped session cache after local authorization", async () => {
    installReads();
    const view = mountHook();
    await waitFor(() => expect(view.result.current.sessionAuthorized).toBe(true));
    const renewed = { ...session, session_id: "renewed-session" };

    act(() => view.result.current.onSessionAuthorized(renewed));

    await waitFor(() => expect(view.result.current.session?.session_id).toBe("renewed-session"));
    expect(view.result.current.sessionAuthorized).toBe(true);
  });

  it("requires explicit revisit after native Run and context facts drift", async () => {
    installReads();
    document.cookie = "sidera_csrf=owned-csrf; Path=/";
    const changedRunId = "90000000-0000-0000-0000-000000000002";
    const changedWorkspace: WorkerWorkspace = {
      ...workspace,
      worker_item: { ...workspace.worker_item, context_version: 5, acknowledged_context_version: 5 },
    };
    const changedContext: NativeWorkerContextView = {
      ...nativeContext,
      context: { ...nativeContext.context!, context_version: 5, run_id: changedRunId },
      execution: { ...nativeContext.execution!, run_id: changedRunId },
    };
    let currentContext = nativeContext;
    const changedProposal: NativeHandoffProposal = {
      ...proposal,
      source_run_id: changedRunId,
      context: { ...proposal.context, target_context_version: 5 },
    };
    const posted: CommandRequest[] = [];
    server.use(
      http.get(`${base}/worker-items/${workerItemId}/native-context`, () => HttpResponse.json(currentContext)),
      http.post("/api/v1/commands", async ({ request }) => {
        const command = await request.json() as CommandRequest;
        posted.push(command);
        return HttpResponse.json({
          command_id: command.command_id,
          aggregate_id: changedProposal.id,
          aggregate_version: changedProposal.version,
          event_ids: [],
          data: { proposal: changedProposal },
        } satisfies CommandResponse);
      }),
    );
    const view = mountHook();
    await waitFor(() => expect(view.result.current.sourceReview).not.toBeNull());
    act(() => {
      view.result.current.setReason("Change the implementation owner");
      view.result.current.setInstruction("Continue the validation step");
    });
    await waitFor(() => expect(view.result.current.canPrepare).toBe(true));
    const reviewedSource = view.result.current.sourceReview;
    if (reviewedSource === null) throw new Error("Expected the initial source review");

    currentContext = changedContext;
    view.rerender({ currentWorkspace: changedWorkspace, target: profileTarget });
    await act(async () => {
      await view.client.invalidateQueries({
        queryKey: [...nativeWorkerContextQueryKey(workspaceId, workerItemId), principalId],
        exact: true,
      });
    });

    await waitFor(() => expect(view.result.current.isStale).toBe(true));
    expect(view.result.current.canPrepare).toBe(false);
    expect(view.result.current.sourceReview).toEqual(reviewedSource);
    expect(view.result.current.reason).toBe("Change the implementation owner");
    expect(view.result.current.instruction).toBe("Continue the validation step");
    expect(posted).toHaveLength(0);

    await act(async () => view.result.current.revisit());

    await waitFor(() => expect(view.result.current.canPrepare).toBe(true));
    expect(view.result.current.sourceReview).toMatchObject({ run_id: changedRunId, context_version: 5 });
    expect(view.result.current.reason).toBe("Change the implementation owner");
    expect(view.result.current.instruction).toBe("Continue the validation step");
    await act(async () => view.result.current.prepare());

    await waitFor(() => expect(view.result.current.prepared?.proposal.id).toBe(proposalId));
    expect(posted).toHaveLength(1);
    const preparedCommand = posted[0];
    if (preparedCommand === undefined) throw new Error("Expected the posted Prepare command");
    expect(preparedCommand).toMatchObject({
      expected_version: 7,
      payload: { context: { run_id: changedRunId, context_version: 5 } },
    });
    expect(changedContext.context).toMatchObject({ run_id: changedRunId, context_version: 5 });
  });

  it("keeps an expired action-entry session write-free and permits retry after authorization", async () => {
    installReads();
    document.cookie = "sidera_csrf=owned-csrf; Path=/";
    let expired = false;
    const posted: CommandRequest[] = [];
    server.use(
      http.get("/product/v1/session", () => HttpResponse.json(expired
        ? { ...session, expires_at: "2000-01-01T00:00:00Z" }
        : session)),
      http.post("/api/v1/commands", async ({ request }) => {
        const command = await request.json() as CommandRequest;
        posted.push(command);
        return HttpResponse.json({
          command_id: command.command_id,
          aggregate_id: proposal.id,
          aggregate_version: proposal.version,
          event_ids: [],
          data: { proposal },
        } satisfies CommandResponse);
      }),
    );
    const view = mountHook();
    await waitFor(() => expect(view.result.current.sourceReview).not.toBeNull());
    act(() => {
      view.result.current.setReason("Change the implementation owner");
      view.result.current.setInstruction("Continue the validation step");
    });
    await waitFor(() => expect(view.result.current.canPrepare).toBe(true));
    const reviewedSource = view.result.current.sourceReview;
    expired = true;

    await act(async () => view.result.current.prepare());

    await waitFor(() => expect(view.result.current.error).toBe("需要重新确认当前用户会话"));
    expect(posted).toHaveLength(0);
    expect(view.result.current.sourceReview).toEqual(reviewedSource);
    expect(view.result.current.target).toEqual(profileTarget);
    expect(view.result.current.reason).toBe("Change the implementation owner");
    expect(view.result.current.instruction).toBe("Continue the validation step");

    expired = false;
    await act(async () => view.result.current.prepare());

    await waitFor(() => expect(view.result.current.prepared?.proposal.id).toBe(proposalId));
    expect(posted).toHaveLength(1);
    const preparedCommand = posted[0];
    if (preparedCommand === undefined) throw new Error("Expected the posted Prepare command");
    expect(preparedCommand.command_type).toBe("PrepareProfileHandoff");
  });

  it("keeps a denied exact Profile revision write-free and permits retry when it is available", async () => {
    installReads();
    document.cookie = "sidera_csrf=owned-csrf; Path=/";
    let denyExactRevision = false;
    let exactRevisionReads = 0;
    const posted: CommandRequest[] = [];
    server.use(
      http.get(`/agents/v1/workspaces/${workspaceId}/profiles/${PROFILE_REF.profile_id}`, ({ request }) => {
        exactRevisionReads += 1;
        const revision = new URL(request.url).searchParams.get("revision");
        if (revision !== String(PROFILE_REF.revision)) return HttpResponse.json({}, { status: 404 });
        return denyExactRevision ? HttpResponse.json({}, { status: 404 }) : HttpResponse.json(PROFILE);
      }),
      http.post("/api/v1/commands", async ({ request }) => {
        const command = await request.json() as CommandRequest;
        posted.push(command);
        return HttpResponse.json({
          command_id: command.command_id,
          aggregate_id: proposal.id,
          aggregate_version: proposal.version,
          event_ids: [],
          data: { proposal },
        } satisfies CommandResponse);
      }),
    );
    const view = mountHook();
    await waitFor(() => expect(view.result.current.sourceReview).not.toBeNull());
    act(() => {
      view.result.current.setReason("Change the implementation owner");
      view.result.current.setInstruction("Continue the validation step");
    });
    await waitFor(() => expect(view.result.current.canPrepare).toBe(true));
    const readsBeforeAction = exactRevisionReads;
    const reviewedSource = view.result.current.sourceReview;
    denyExactRevision = true;

    await act(async () => view.result.current.prepare());

    await waitFor(() => expect(view.result.current.error).toBe("所选目标 Profile 版本不可用"));
    expect(posted).toHaveLength(0);
    expect(exactRevisionReads).toBe(readsBeforeAction + 1);
    expect(view.result.current.sourceReview).toEqual(reviewedSource);
    expect(view.result.current.target).toEqual(profileTarget);
    expect(view.result.current.reason).toBe("Change the implementation owner");
    expect(view.result.current.instruction).toBe("Continue the validation step");

    denyExactRevision = false;
    act(() => {
      view.result.current.setReason("Change the implementation owner");
      view.result.current.setInstruction("Continue the validation step");
    });
    await waitFor(() => expect(view.result.current.canPrepare).toBe(true));
    await act(async () => view.result.current.prepare());

    await waitFor(() => expect(view.result.current.prepared?.proposal.id).toBe(proposalId));
    expect(posted).toHaveLength(1);
    expect(exactRevisionReads).toBe(readsBeforeAction + 2);
  });

  it("updates expiry eligibility at the canonical proposal deadline without refreshing or mutating intent", async () => {
    vi.useFakeTimers();
    let unmount: (() => void) | null = null;
    try {
      vi.setSystemTime(new Date("2026-10-07T00:00:00.000Z"));
      const expiresAt = new Date(Date.now() + 30_000).toISOString();
      const expiringProposal: NativeHandoffProposal = { ...proposal, expires_at: expiresAt };
      const posted: CommandRequest[] = [];
      let exactProfileReads = 0;
      let proposalReads = 0;
      installReads();
      document.cookie = "sidera_csrf=owned-csrf; Path=/";
      server.use(
        http.get(`/agents/v1/workspaces/${workspaceId}/profiles/${PROFILE_REF.profile_id}`, () => {
          exactProfileReads += 1;
          return HttpResponse.json(PROFILE);
        }),
        http.get(`${base}/handoff-proposals/${proposalId}`, () => {
          proposalReads += 1;
          return HttpResponse.json(expiringProposal);
        }),
        http.post("/api/v1/commands", async ({ request }) => {
          const command = await request.json() as CommandRequest;
          posted.push(command);
          return HttpResponse.json({
            command_id: command.command_id,
            aggregate_id: expiringProposal.id,
            aggregate_version: expiringProposal.version,
            event_ids: [],
            data: { proposal: expiringProposal },
          } satisfies CommandResponse);
        }),
      );
      const fetchSpy = vi.spyOn(globalThis, "fetch");
      const view = mountHook(profileTarget);
      unmount = view.unmount;
      await vi.waitFor(() => expect(view.result.current.sourceReview).not.toBeNull());
      act(() => {
        view.result.current.setReason("Change the implementation owner");
        view.result.current.setInstruction("Continue the validation step");
      });
      await vi.waitFor(() => expect(view.result.current.canPrepare).toBe(true));
      await act(async () => view.result.current.prepare());
      await vi.waitFor(() => expect(view.result.current.canConfirm).toBe(true));
      expect(view.result.current.canCancel).toBe(true);

      const preparedV1 = view.result.current.prepared;
      if (preparedV1 === null) throw new Error("Expected the canonical prepared V1 anchor");
      const prepareAction = view.result.current.actions.prepare;
      const reason = view.result.current.reason;
      const instruction = view.result.current.instruction;
      const target = view.result.current.target;
      const fetchCountBeforeDeadline = fetchSpy.mock.calls.length;
      const exactProfileReadsBeforeDeadline = exactProfileReads;
      const proposalReadsBeforeDeadline = proposalReads;
      const postsBeforeDeadline = posted.length;
      const elapsedToDeadline = Date.parse(expiresAt) - Date.now() + 1;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(elapsedToDeadline);
      });
      await vi.waitFor(() => {
        expect(view.result.current.canConfirm).toBe(false);
        expect(view.result.current.canCancel).toBe(false);
      });

      expect(view.result.current.prepared?.proposal).toEqual(preparedV1.proposal);
      expect(view.result.current.reason).toBe(reason);
      expect(view.result.current.instruction).toBe(instruction);
      expect(view.result.current.target).toEqual(target);
      expect(view.result.current.actions.prepare).toEqual(prepareAction);
      expect(fetchSpy).toHaveBeenCalledTimes(fetchCountBeforeDeadline);
      expect(exactProfileReads).toBe(exactProfileReadsBeforeDeadline);
      expect(proposalReads).toBe(proposalReadsBeforeDeadline);
      expect(posted).toHaveLength(postsBeforeDeadline);
    } finally {
      unmount?.();
      vi.useRealTimers();
    }
  });

  it("does not apply an unmounted scope's late Prepare and recovers its saved original by GET", async () => {
    installReads();
    document.cookie = "sidera_csrf=owned-csrf; Path=/";
    const onRefresh = vi.fn(async () => undefined);
    const posted: CommandRequest[] = [];
    const commandReads: string[] = [];
    const held: {
      original: CommandRequest | null;
      release: ((response: Response) => void) | null;
      signalStarted: () => void;
    } = { original: null, release: null, signalStarted: () => undefined };
    const postStarted = new Promise<void>((resolve) => {
      held.signalStarted = resolve;
    });
    server.use(http.post("/api/v1/commands", async ({ request }) => {
      const command = await request.json() as CommandRequest;
      posted.push(command);
      if (command.command_type === "PrepareProfileHandoff") {
        held.original = command;
        return await new Promise<Response>((resolve) => {
          held.release = resolve;
          held.signalStarted();
        });
      }
      return HttpResponse.json({});
    }));

    const first = mountHook(profileTarget, onRefresh);
    await waitFor(() => expect(first.result.current.sourceReview).not.toBeNull());
    act(() => {
      first.result.current.setReason("Change the implementation owner");
      first.result.current.setInstruction("Continue the validation step");
    });
    await waitFor(() => expect(first.result.current.canPrepare).toBe(true));
    const action: { promise: Promise<void> | null } = { promise: null };
    act(() => {
      action.promise = first.result.current.prepare();
    });
    await postStarted;

    const original = held.original;
    if (original === null) throw new Error("Expected the pending Prepare original");
    const pendingA = localStorage.getItem(handoffDraftKey(scope));
    expect(JSON.parse(pendingA ?? "null")).toMatchObject({
      actions: { prepare: { original, status: "pending" } },
    });
    const refreshCountBeforeUnmount = onRefresh.mock.calls.length;
    first.unmount();

    const second = mountHook(null, vi.fn(async () => undefined), workspaceB);
    await waitFor(() => expect(second.result.current.scope).toEqual(scopeB));
    await waitFor(() => expect(JSON.parse(localStorage.getItem(handoffDraftKey(scopeB)) ?? "null")
      .source_review).not.toBeNull());
    const draftB = localStorage.getItem(handoffDraftKey(scopeB));
    expect(second.result.current.originalLookupNeeded).toBe(false);
    expect(second.result.current.target).toBeNull();

    const released = held.release;
    const preparePromise = action.promise;
    if (released === null || preparePromise === null) throw new Error("Expected the held Prepare reply");
    const mappedPrepare: CommandResponse = {
      command_id: original.command_id,
      aggregate_id: proposal.id,
      aggregate_version: proposal.version,
      event_ids: [],
      data: { proposal },
    };
    await act(async () => {
      released(HttpResponse.json(mappedPrepare));
      await preparePromise;
    });

    expect(localStorage.getItem(handoffDraftKey(scope))).toBe(pendingA);
    expect(localStorage.getItem(handoffDraftKey(scopeB))).toBe(draftB);
    expect(onRefresh).toHaveBeenCalledTimes(refreshCountBeforeUnmount);
    expect(second.result.current.originalLookupNeeded).toBe(false);
    expect(second.result.current.target).toBeNull();
    expect(posted).toHaveLength(1);

    second.unmount();
    server.use(http.get(`${base}/commands/${original.command_id}`, () => {
      commandReads.push(original.command_id);
      return HttpResponse.json({
        command_id: original.command_id,
        command_type: original.command_type,
        target_type: original.target_type,
        target_id: original.target_id,
        expected_version: original.expected_version,
        status: "applied",
        result: mappedPrepare,
      });
    }));
    const currentA = mountHook(null);
    await waitFor(() => expect(currentA.result.current.originalLookupNeeded).toBe(true));
    await act(async () => currentA.result.current.lookupOriginal());

    await waitFor(() => expect(currentA.result.current.prepared?.proposal.id).toBe(proposalId));
    expect(commandReads).toEqual([original.command_id]);
    expect(posted).toHaveLength(1);
  });

  it("fences a late A reply when the same Hook rerenders into scope B", async () => {
    installReads();
    document.cookie = "sidera_csrf=owned-csrf; Path=/";
    const onRefresh = vi.fn(async () => undefined);
    const posted: CommandRequest[] = [];
    const held: {
      release: ((response: Response) => void) | null;
      signalStarted: () => void;
    } = { release: null, signalStarted: () => undefined };
    const postStarted = new Promise<void>((resolve) => {
      held.signalStarted = resolve;
    });
    server.use(http.post("/api/v1/commands", async ({ request }) => {
      const command = await request.json() as CommandRequest;
      posted.push(command);
      return await new Promise<Response>((resolve) => {
        held.release = resolve;
        held.signalStarted();
      });
    }));

    const view = mountHook(profileTarget, onRefresh);
    await waitFor(() => expect(view.result.current.sourceReview).not.toBeNull());
    act(() => {
      view.result.current.setReason("Change the implementation owner");
      view.result.current.setInstruction("Continue the validation step");
    });
    await waitFor(() => expect(view.result.current.canPrepare).toBe(true));
    const action: { promise: Promise<void> | null } = { promise: null };
    act(() => {
      action.promise = view.result.current.prepare();
    });
    await postStarted;
    const pendingA = localStorage.getItem(handoffDraftKey(scope));
    const refreshCountBeforeSwitch = onRefresh.mock.calls.length;

    view.rerender({ currentWorkspace: workspaceB, target: null });
    await waitFor(() => expect(view.result.current.scope).toEqual(scopeB));
    await waitFor(() => expect(JSON.parse(localStorage.getItem(handoffDraftKey(scopeB)) ?? "null")
      .source_review).not.toBeNull());
    const draftB = localStorage.getItem(handoffDraftKey(scopeB));
    const original = posted[0];
    const released = held.release;
    const preparePromise = action.promise;
    if (original === undefined) throw new Error("Expected the posted Prepare command");
    if (released === null || preparePromise === null) throw new Error("Expected the held Prepare reply");
    const mappedPrepare: CommandResponse = {
      command_id: original.command_id,
      aggregate_id: proposal.id,
      aggregate_version: proposal.version,
      event_ids: [],
      data: { proposal },
    };
    await act(async () => {
      released(HttpResponse.json(mappedPrepare));
      await preparePromise;
    });

    expect(localStorage.getItem(handoffDraftKey(scope))).toBe(pendingA);
    expect(localStorage.getItem(handoffDraftKey(scopeB))).toBe(draftB);
    expect(onRefresh).toHaveBeenCalledTimes(refreshCountBeforeSwitch);
    expect(view.result.current.originalLookupNeeded).toBe(false);
    expect(view.result.current.target).toEqual(JSON.parse(draftB ?? "null").target);
    expect(posted).toHaveLength(1);
  });

  it.each(["confirm", "cancel"] as const)(
    "fences a late %s target check after scope A is unmounted",
    async (actionName) => {
      installReads();
      document.cookie = "sidera_csrf=owned-csrf; Path=/";
      const posted: CommandRequest[] = [];
      const onRefresh = vi.fn(async () => undefined);
      let holdExactRead = false;
      const held: {
        release: ((response: Response) => void) | null;
        signalStarted: () => void;
      } = { release: null, signalStarted: () => undefined };
      const targetReadStarted = new Promise<void>((resolve) => {
        held.signalStarted = resolve;
      });
      server.use(
        http.get(`/agents/v1/workspaces/${workspaceId}/profiles/${PROFILE_REF.profile_id}`, ({ request }) => {
          const revision = new URL(request.url).searchParams.get("revision");
          if (revision !== String(PROFILE_REF.revision)) return HttpResponse.json({}, { status: 404 });
          if (!holdExactRead) return HttpResponse.json(PROFILE);
          return new Promise<Response>((resolve) => {
            held.release = resolve;
            held.signalStarted();
          });
        }),
        http.post("/api/v1/commands", async ({ request }) => {
          const command = await request.json() as CommandRequest;
          posted.push(command);
          if (command.command_type === "PrepareProfileHandoff") {
            return HttpResponse.json({
              command_id: command.command_id,
              aggregate_id: proposal.id,
              aggregate_version: proposal.version,
              event_ids: [],
              data: { proposal },
            } satisfies CommandResponse);
          }
          if (command.command_type === "ConfirmProfileHandoff") {
            return HttpResponse.json({
              command_id: command.command_id,
              aggregate_id: transition.id,
              aggregate_version: transition.version,
              event_ids: [],
              data: { proposal: confirmedProposal, transition },
            } satisfies CommandResponse);
          }
          return HttpResponse.json({
            command_id: command.command_id,
            aggregate_id: proposal.id,
            aggregate_version: 2,
            event_ids: [],
            data: { proposal: { ...proposal, version: 2, status: "cancelled" } },
          } satisfies CommandResponse);
        }),
      );
      const randomUUID = vi.spyOn(crypto, "randomUUID");
      const first = mountHook(profileTarget, onRefresh);
      await waitFor(() => expect(first.result.current.sourceReview).not.toBeNull());
      act(() => {
        first.result.current.setReason("Change the implementation owner");
        first.result.current.setInstruction("Continue the validation step");
      });
      await waitFor(() => expect(first.result.current.canPrepare).toBe(true));
      await act(async () => first.result.current.prepare());
      if (actionName === "confirm") {
        await waitFor(() => expect(first.result.current.canConfirm).toBe(true));
      } else {
        await waitFor(() => expect(first.result.current.canCancel).toBe(true));
      }

      holdExactRead = true;
      const action: { promise: Promise<void> | null } = { promise: null };
      act(() => {
        action.promise = actionName === "confirm"
          ? first.result.current.confirm()
          : first.result.current.cancel();
      });
      await targetReadStarted;

      const savedA = localStorage.getItem(handoffDraftKey(scope));
      if (savedA === null) throw new Error("Expected the saved scope A draft");
      const postCountBeforeHeldReply = posted.length;
      const identityCountBeforeHeldReply = randomUUID.mock.calls.length;
      const refreshCountBeforeHeldReply = onRefresh.mock.calls.length;
      first.unmount();

      const second = mountHook(null, vi.fn(async () => undefined), workspaceB);
      await waitFor(() => expect(second.result.current.scope).toEqual(scopeB));
      await waitFor(() => expect(second.result.current.sourceReview).not.toBeNull());
      const savedB = localStorage.getItem(handoffDraftKey(scopeB));
      expect(second.result.current.originalLookupNeeded).toBe(false);
      expect(second.result.current.target).toBeNull();

      const release = held.release;
      const actionPromise = action.promise;
      if (release === null || actionPromise === null) throw new Error("Expected the held exact Profile read");
      await act(async () => {
        release(HttpResponse.json(PROFILE));
        await actionPromise;
      });

      expect(localStorage.getItem(handoffDraftKey(scope))).toBe(savedA);
      expect(localStorage.getItem(handoffDraftKey(scopeB))).toBe(savedB);
      expect(randomUUID).toHaveBeenCalledTimes(identityCountBeforeHeldReply);
      expect(posted).toHaveLength(postCountBeforeHeldReply);
      expect(onRefresh).toHaveBeenCalledTimes(refreshCountBeforeHeldReply);
      expect(JSON.parse(savedA)).toMatchObject({
        prepared: { proposal: { id: proposalId, version: 1, status: "prepared" } },
        actions: {
          confirm: { status: "idle", original: null },
          cancel: { status: "idle", original: null },
        },
      });
      expect(second.result.current.originalLookupNeeded).toBe(false);
      expect(second.result.current.target).toBeNull();

      second.unmount();
      const remountedA = mountHook(profileTarget);
      await waitFor(() => expect(remountedA.result.current.prepared?.proposal).toMatchObject({
        id: proposalId,
        version: 1,
        status: "prepared",
      }));
      expect(remountedA.result.current.actions[actionName].status).toBe("idle");
      expect(posted).toHaveLength(postCountBeforeHeldReply);
    },
  );

  it("persists Prepare before POST and uses separate proposal and transition aggregates", async () => {
    installReads();
    document.cookie = "sidera_csrf=owned-csrf; Path=/";
    let canonicalProposal = proposal;
    const posted: CommandRequest[] = [];
    server.use(
      http.get(`${base}/handoff-proposals/${proposalId}`, () => HttpResponse.json(canonicalProposal)),
      http.post("/api/v1/commands", async ({ request }) => {
        const command = await request.json() as CommandRequest;
        posted.push(command);
        const stored = localStorage.getItem(handoffDraftKey(scope));
        expect(stored).not.toBeNull();
        const action = command.command_type === "PrepareProfileHandoff" ? "prepare" : "confirm";
        expect(JSON.parse(stored ?? "null")).toMatchObject({
          actions: { [action]: { original: command, status: "pending" } },
        });
        if (command.command_type === "PrepareProfileHandoff") {
          return HttpResponse.json({
            command_id: command.command_id,
            aggregate_id: proposal.id,
            aggregate_version: proposal.version,
            event_ids: [],
            data: { proposal },
          } satisfies CommandResponse);
        }
        canonicalProposal = confirmedProposal;
        return HttpResponse.json({
          command_id: command.command_id,
          aggregate_id: transition.id,
          aggregate_version: transition.version,
          event_ids: [],
          data: { proposal: confirmedProposal, transition },
        } satisfies CommandResponse);
      }),
    );
    const view = mountHook();

    await waitFor(() => expect(view.result.current.sourceReview).not.toBeNull());
    act(() => {
      view.result.current.setReason("Change the implementation owner");
      view.result.current.setInstruction("Continue the validation step");
    });
    await waitFor(() => expect(view.result.current.canPrepare).toBe(true));
    await act(async () => {
      await Promise.all([view.result.current.prepare(), view.result.current.prepare()]);
    });

    await waitFor(() => expect(view.result.current.prepared?.proposal.id).toBe(proposal.id));
    expect(posted).toHaveLength(1);
    const prepareCommand = posted[0];
    if (prepareCommand === undefined) throw new Error("Expected the posted Prepare command");
    expect(prepareCommand).toMatchObject({
      command_type: "PrepareProfileHandoff",
      actor: { type: "user", id: principalId },
      target_type: "worker_item",
      target_id: workerItemId,
      expected_version: 7,
      payload: {
        proposal_schema_version: 2,
        context: contextAddress,
        target_assignee: { kind: "profile", id: PROFILE_REF.profile_id },
        target_profile_ref: PROFILE_REF,
        handoff_reason: "Change the implementation owner",
        handoff_instruction: "Continue the validation step",
        input_plan: {},
        proposal_ttl_seconds: 900,
      },
    });
    expect(view.result.current.actions.prepare.status).toBe("applied");

    await waitFor(() => expect(view.result.current.canConfirm).toBe(true));
    await act(async () => view.result.current.confirm());
    await waitFor(() => expect(view.result.current.transitionId).toBe(transition.id));
    expect(posted).toHaveLength(2);
    expect(posted[1]).toMatchObject({
      command_type: "ConfirmProfileHandoff",
      target_type: "handoff_proposal",
      target_id: proposal.id,
      expected_version: proposal.version,
      payload: {
        proposal_id: proposal.id,
        expected_proposal_version: proposal.version,
        expected_proposal_hash: proposal.proposal_hash,
        expected_worker_item_version: 7,
      },
    });
  });

  it("does not accept a converged transition for a different frozen proposal", async () => {
    installReads();
    document.cookie = "sidera_csrf=owned-csrf; Path=/";
    const posted: CommandRequest[] = [];
    let serveForeignTransition = false;
    const foreignConverged: NativeHandoffTransition = {
      ...transition,
      proposal_id: "d0000000-0000-0000-0000-000000000001",
      version: 7,
      status: "converged",
      phase: "complete",
      writer_state: "target_active",
      started_at: "2026-10-07T00:02:00Z",
      completed_at: "2026-10-07T00:03:00Z",
      target_profile_ref: PROFILE_REF,
      target_worker_id: "80000000-0000-0000-0000-000000000002",
      target_execution_id: "60000000-0000-0000-0000-000000000002",
      target_session_id: "70000000-0000-0000-0000-000000000002",
      target_run_id: "90000000-0000-0000-0000-000000000002",
      target_binding_version: 3,
      target_context_version: 5,
      context: {
        ...context,
        target_package_id: "e0000000-0000-0000-0000-000000000001",
        target_package_hash: "e".repeat(64),
        target_context_version: 5,
      },
    };
    server.use(
      http.get(`${base}/handoff-transitions/${transitionId}`, () => HttpResponse.json(
        serveForeignTransition ? foreignConverged : transition,
      )),
      http.post("/api/v1/commands", async ({ request }) => {
        const command = await request.json() as CommandRequest;
        posted.push(command);
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
    const randomUUID = vi.spyOn(crypto, "randomUUID");
    const view = mountHook();
    await waitFor(() => expect(view.result.current.sourceReview).not.toBeNull());
    act(() => {
      view.result.current.setReason("Change the implementation owner");
      view.result.current.setInstruction("Continue the validation step");
    });
    await waitFor(() => expect(view.result.current.canPrepare).toBe(true));
    await act(async () => view.result.current.prepare());
    await waitFor(() => expect(view.result.current.canConfirm).toBe(true));
    await act(async () => view.result.current.confirm());
    await waitFor(() => expect(view.result.current.transition?.id).toBe(transitionId));

    const identityCountAfterConfirm = randomUUID.mock.calls.length;
    const transitionKey = [
      "native-profile-handoff-transition", workspaceId, principalId, workerItemId, transitionId,
    ] as const;
    serveForeignTransition = true;
    await act(async () => {
      await view.client.invalidateQueries({ queryKey: transitionKey, exact: true });
    });

    await waitFor(() => expect(view.client.getQueryState(transitionKey)?.status).toBe("error"));
    expect(view.result.current.canStartNew).toBe(false);
    expect(view.result.current.prepared?.proposal).toMatchObject({ id: proposalId, version: 1, status: "prepared" });
    expect(view.result.current.actions.confirm).toMatchObject({
      original: { command_type: "ConfirmProfileHandoff", expected_version: 1 },
      status: "applied",
    });
    expect(randomUUID).toHaveBeenCalledTimes(identityCountAfterConfirm);
    expect(posted).toHaveLength(2);
  });

  it("freezes trimmed Prepare text in both the original command and persisted draft", async () => {
    installReads();
    document.cookie = "sidera_csrf=owned-csrf; Path=/";
    const posted: CommandRequest[] = [];
    server.use(http.post("/api/v1/commands", async ({ request }) => {
      const command = await request.json() as CommandRequest;
      posted.push(command);
      const stored = JSON.parse(localStorage.getItem(handoffDraftKey(scope)) ?? "null") as {
        reason: string;
        instruction: string;
        actions: { prepare: { original: CommandRequest; status: string } };
      };
      expect(stored.actions.prepare).toEqual({ original: command, status: "pending", rejectionCode: null });
      expect(stored.reason).toBe("Change the implementation owner");
      expect(stored.instruction).toBe("Continue the validation step");
      return HttpResponse.json({
        command_id: command.command_id,
        aggregate_id: proposal.id,
        aggregate_version: proposal.version,
        event_ids: [],
        data: { proposal },
      } satisfies CommandResponse);
    }));
    const view = mountHook();

    await waitFor(() => expect(view.result.current.target).toEqual(profileTarget));
    act(() => {
      view.result.current.setReason("  Change the implementation owner\n");
      view.result.current.setInstruction("\nContinue the validation step  ");
    });
    await waitFor(() => expect(view.result.current.canPrepare).toBe(true));
    expect(view.result.current.reason).toBe("  Change the implementation owner\n");
    expect(view.result.current.instruction).toBe("\nContinue the validation step  ");
    await act(async () => view.result.current.prepare());

    await waitFor(() => expect(view.result.current.prepared?.proposal.id).toBe(proposalId));
    expect(posted).toHaveLength(1);
    const prepareCommand = posted[0];
    if (prepareCommand === undefined) throw new Error("Expected the posted Prepare command");
    expect(prepareCommand).toMatchObject({ payload: {
      handoff_reason: "Change the implementation owner",
      handoff_instruction: "Continue the validation step",
    } });
    expect(view.result.current.actions.prepare.status).toBe("applied");
  });

  it.each(["missing", "foreign"] as const)(
    "keeps a %s command ID POST reply unresolved and recovers only by the exact original GET",
    async (correlation) => {
      installReads();
      document.cookie = "sidera_csrf=owned-csrf; Path=/";
      const posted: CommandRequest[] = [];
      let lookups = 0;
      server.use(http.post("/api/v1/commands", async ({ request }) => {
        const command = await request.json() as CommandRequest;
        posted.push(command);
        const response = {
          aggregate_id: proposal.id,
          aggregate_version: proposal.version,
          event_ids: [],
          data: { proposal },
          ...(correlation === "foreign" ? { command_id: "foreign-command-id" } : {}),
        };
        return HttpResponse.json(response);
      }));
      const view = mountHook();
      await waitFor(() => expect(view.result.current.target).toEqual(profileTarget));
      act(() => {
        view.result.current.setReason("Change the implementation owner");
        view.result.current.setInstruction("Continue the validation step");
      });
      await waitFor(() => expect(view.result.current.canPrepare).toBe(true));
      await act(async () => view.result.current.prepare());

      await waitFor(() => expect(view.result.current.actions.prepare.status).toBe("unknown"));
      const original = posted[0];
      if (original === undefined) throw new Error("Expected the posted Prepare command");
      expect(view.result.current.prepared).toBeNull();
      expect(view.result.current.originalLookupNeeded).toBe(true);
      expect(JSON.parse(localStorage.getItem(handoffDraftKey(scope)) ?? "null").actions.prepare.original)
        .toEqual(original);

      server.use(http.get(`${base}/commands/${original.command_id}`, () => {
        lookups += 1;
        return HttpResponse.json({
          command_id: original.command_id,
          command_type: original.command_type,
          target_type: original.target_type,
          target_id: original.target_id,
          expected_version: original.expected_version,
          status: "applied",
          result: {
            command_id: original.command_id,
            aggregate_id: proposal.id,
            aggregate_version: proposal.version,
            event_ids: [],
            data: { proposal },
          },
        });
      }));
      await act(async () => view.result.current.lookupOriginal());

      await waitFor(() => expect(view.result.current.prepared?.proposal.id).toBe(proposalId));
      expect(view.result.current.actions.prepare.status).toBe("applied");
      expect(lookups).toBe(1);
      expect(posted).toHaveLength(1);
    },
  );

  it("retains an acknowledged Confirm when the subsequent canonical refresh fails", async () => {
    installReads();
    document.cookie = "sidera_csrf=owned-csrf; Path=/";
    let confirmAcknowledged = false;
    const onRefresh = vi.fn(async () => {
      if (confirmAcknowledged) throw new Error("canonical refresh unavailable");
    });
    const posted: CommandRequest[] = [];
    server.use(http.post("/api/v1/commands", async ({ request }) => {
      const command = await request.json() as CommandRequest;
      posted.push(command);
      if (command.command_type === "PrepareProfileHandoff") {
        return HttpResponse.json({
          command_id: command.command_id,
          aggregate_id: proposal.id,
          aggregate_version: proposal.version,
          event_ids: [],
          data: { proposal },
        } satisfies CommandResponse);
      }
      confirmAcknowledged = true;
      return HttpResponse.json({
        command_id: command.command_id,
        aggregate_id: transition.id,
        aggregate_version: transition.version,
        event_ids: [],
        data: { proposal: confirmedProposal, transition },
      } satisfies CommandResponse);
    }));
    const view = mountHook(profileTarget, onRefresh);
    await waitFor(() => expect(view.result.current.target).toEqual(profileTarget));
    act(() => {
      view.result.current.setReason("Change the implementation owner");
      view.result.current.setInstruction("Continue the validation step");
    });
    await waitFor(() => expect(view.result.current.canPrepare).toBe(true));
    await act(async () => view.result.current.prepare());
    await waitFor(() => expect(view.result.current.canConfirm).toBe(true));
    await act(async () => view.result.current.confirm());

    await waitFor(() => expect(view.result.current.transitionId).toBe(transitionId));
    expect(view.result.current.actions.confirm.status).toBe("applied");
    expect(view.result.current.storageError).toBeNull();
    expect(view.result.current.error).toContain("操作已确认");
    expect(view.result.current.error).not.toContain("已保存");
    expect(posted).toHaveLength(2);
    const persisted = JSON.parse(localStorage.getItem(handoffDraftKey(scope)) ?? "null");
    expect(persisted.prepared.proposal).toMatchObject({ version: 1, status: "prepared" });
    expect(persisted.actions.confirm).toMatchObject({
      original: { command_type: "ConfirmProfileHandoff", expected_version: 1 },
      status: "applied",
    });
    expect(persisted.transition_id).toBe(transitionId);
  });

  it("retains journal-confirmed application when only its subsequent read refresh fails", async () => {
    installReads();
    document.cookie = "sidera_csrf=owned-csrf; Path=/";
    const posted: CommandRequest[] = [];
    const originalHolder: { current: CommandRequest | null } = { current: null };
    let rejectRefreshAfterLookup = false;
    server.use(http.post("/api/v1/commands", async ({ request }) => {
      const command = await request.json() as CommandRequest;
      posted.push(command);
      if (command.command_type === "PrepareProfileHandoff") {
        return HttpResponse.json({
          command_id: command.command_id,
          aggregate_id: proposal.id,
          aggregate_version: proposal.version,
          event_ids: [],
          data: { proposal },
        } satisfies CommandResponse);
      }
      originalHolder.current = command;
      return HttpResponse.error();
    }));
    const first = mountHook(profileTarget);
    await waitFor(() => expect(first.result.current.target).toEqual(profileTarget));
    act(() => {
      first.result.current.setReason("Change the implementation owner");
      first.result.current.setInstruction("Continue the validation step");
    });
    await waitFor(() => expect(first.result.current.canPrepare).toBe(true));
    await act(async () => first.result.current.prepare());
    await waitFor(() => expect(first.result.current.canConfirm).toBe(true));
    await act(async () => first.result.current.confirm());
    await waitFor(() => expect(first.result.current.actions.confirm.status).toBe("unknown"));
    first.unmount();

    const original = originalHolder.current;
    if (original === null) throw new Error("Expected the exact original Confirm command");
    const result: CommandResponse = {
      command_id: original.command_id,
      aggregate_id: transition.id,
      aggregate_version: transition.version,
      event_ids: [],
      data: { proposal: confirmedProposal, transition },
    };
    server.use(http.get(`${base}/commands/${original.command_id}`, () => {
      rejectRefreshAfterLookup = true;
      return HttpResponse.json({
        command_id: original.command_id,
        command_type: original.command_type,
        target_type: original.target_type,
        target_id: original.target_id,
        expected_version: original.expected_version,
        status: "applied",
        result,
      });
    }));
    const onRefresh = vi.fn(async () => {
      if (rejectRefreshAfterLookup) throw new Error("canonical refresh unavailable");
    });
    const recovered = mountHook(profileTarget, onRefresh);
    await waitFor(() => expect(recovered.result.current.originalLookupNeeded).toBe(true));
    await waitFor(() => expect(recovered.result.current.target).toEqual(profileTarget));
    await act(async () => recovered.result.current.lookupOriginal());

    await waitFor(() => expect(recovered.result.current.transitionId).toBe(transitionId));
    expect(recovered.result.current.actions.confirm.status).toBe("applied");
    expect(recovered.result.current.storageError).toBeNull();
    expect(recovered.result.current.error).toContain("操作已确认");
    expect(recovered.result.current.error).not.toContain("已保存");
    const persisted = JSON.parse(localStorage.getItem(handoffDraftKey(scope)) ?? "null");
    expect(persisted.actions.confirm).toMatchObject({ original, status: "applied" });
    expect(persisted.prepared.proposal).toMatchObject({ version: 1, status: "prepared" });
    expect(persisted.transition_id).toBe(transitionId);
    expect(posted).toHaveLength(2);

    recovered.unmount();
    const reloaded = mountHook(humanTarget);
    await waitFor(() => expect(reloaded.result.current.actions.confirm.status).toBe("applied"));
    expect(reloaded.result.current.transitionId).toBe(transitionId);
    expect(reloaded.result.current.prepared?.proposal).toMatchObject({ version: 1, status: "prepared" });
    expect(reloaded.result.current.originalLookupNeeded).toBe(false);
    expect(posted).toHaveLength(2);
  });

  it("recovers a lost Prepare reply with the same original GET after reload", async () => {
    installReads();
    document.cookie = "sidera_csrf=owned-csrf; Path=/";
    const originalHolder: { current: CommandRequest | null } = { current: null };
    let posts = 0;
    server.use(
      http.post("/api/v1/commands", async ({ request }) => {
        posts += 1;
        originalHolder.current = await request.json() as CommandRequest;
        return HttpResponse.error();
      }),
    );
    const first = mountHook();
    await waitFor(() => expect(first.result.current.canPrepare).toBe(false));
    await waitFor(() => expect(first.result.current.target).toEqual(profileTarget));
    act(() => {
      first.result.current.setReason("Change the implementation owner");
      first.result.current.setInstruction("Continue the validation step");
    });
    await waitFor(() => expect(first.result.current.canPrepare).toBe(true));
    await act(async () => first.result.current.prepare());
    await waitFor(() => expect(first.result.current.actions.prepare.status).toBe("unknown"));
    const storedOriginal = originalHolder.current;
    if (storedOriginal === null) throw new Error("Expected the frozen Prepare original");
    first.unmount();

    const randomUUID = vi.spyOn(crypto, "randomUUID");
    const mapping: CommandResponse = {
      command_id: storedOriginal.command_id,
      aggregate_id: proposal.id,
      aggregate_version: proposal.version,
      event_ids: [],
      data: { proposal },
    };
    let reads = 0;
    server.use(http.get(
      `${base}/commands/${storedOriginal.command_id}`,
      () => {
        reads += 1;
        return HttpResponse.json({
          command_id: storedOriginal.command_id,
          command_type: storedOriginal.command_type,
          target_type: storedOriginal.target_type,
          target_id: storedOriginal.target_id,
          expected_version: storedOriginal.expected_version,
          status: "applied",
          result: mapping,
        });
      },
    ));
    const second = mountHook(humanTarget);
    await waitFor(() => expect(second.result.current.originalLookupNeeded).toBe(true));
    await waitFor(() => expect(second.result.current.target).toEqual(profileTarget));
    await act(async () => second.result.current.lookupOriginal());

    await waitFor(() => expect(second.result.current.prepared?.proposal.id).toBe(proposal.id));
    expect(reads).toBe(1);
    expect(posts).toBe(1);
    expect(randomUUID).not.toHaveBeenCalled();
  });

  it("recovers a lost Confirm reply from the exact original without changing its V1 anchor", async () => {
    installReads();
    document.cookie = "sidera_csrf=owned-csrf; Path=/";
    const posted: CommandRequest[] = [];
    const originalHolder: { current: CommandRequest | null } = { current: null };
    server.use(http.post("/api/v1/commands", async ({ request }) => {
      const command = await request.json() as CommandRequest;
      posted.push(command);
      if (command.command_type === "PrepareProfileHandoff") {
        return HttpResponse.json({
          command_id: command.command_id,
          aggregate_id: proposal.id,
          aggregate_version: proposal.version,
          event_ids: [],
          data: { proposal },
        } satisfies CommandResponse);
      }
      originalHolder.current = command;
      return HttpResponse.error();
    }));

    const first = mountHook();
    await waitFor(() => expect(first.result.current.sourceReview).not.toBeNull());
    act(() => {
      first.result.current.setReason("Change the implementation owner");
      first.result.current.setInstruction("Continue the validation step");
    });
    await waitFor(() => expect(first.result.current.canPrepare).toBe(true));
    await act(async () => first.result.current.prepare());
    await waitFor(() => expect(first.result.current.prepared?.proposal.version).toBe(1));
    await waitFor(() => expect(first.result.current.canConfirm).toBe(true));
    await act(async () => first.result.current.confirm());
    await waitFor(() => expect(first.result.current.actions.confirm.status).toBe("unknown"));
    first.unmount();

    const storedConfirm = originalHolder.current;
    if (storedConfirm === null) throw new Error("Expected a frozen original Confirm command");
    const storedBeforeReload = JSON.parse(
      localStorage.getItem(handoffDraftKey(scope)) ?? "null",
    ) as { prepared: { proposal: NativeHandoffProposal }; actions: { confirm: { original: CommandRequest } } };
    expect(storedBeforeReload.prepared.proposal).toMatchObject({ id: proposalId, version: 1, status: "prepared" });
    expect(storedBeforeReload.actions.confirm.original).toEqual(storedConfirm);

    const randomUUID = vi.spyOn(crypto, "randomUUID");
    let lookups = 0;
    const confirmResult: CommandResponse = {
      command_id: storedConfirm.command_id,
      aggregate_id: transition.id,
      aggregate_version: transition.version,
      event_ids: [],
      data: { proposal: confirmedProposal, transition },
    };
    const advancedTransition = { ...transition, version: 7 } satisfies NativeHandoffTransition;
    server.use(http.get(`${base}/handoff-transitions/${transitionId}`, () => HttpResponse.json(advancedTransition)));
    server.use(http.get(`${base}/commands/${storedConfirm.command_id}`, () => {
      lookups += 1;
      return HttpResponse.json({
        command_id: storedConfirm.command_id,
        command_type: storedConfirm.command_type,
        target_type: storedConfirm.target_type,
        target_id: storedConfirm.target_id,
        expected_version: storedConfirm.expected_version,
        status: "applied",
        result: confirmResult,
      });
    }));
    const second = mountHook(humanTarget);
    await waitFor(() => expect(second.result.current.originalLookupNeeded).toBe(true));
    await waitFor(() => expect(second.result.current.target).toEqual(profileTarget));
    await act(async () => second.result.current.lookupOriginal());

    await waitFor(() => expect(second.result.current.transitionId).toBe(transitionId));
    expect(second.result.current.actions.confirm.status).toBe("applied");
    expect(second.result.current.prepared?.proposal).toMatchObject({ id: proposalId, version: 1, status: "prepared" });
    await waitFor(() => expect(second.result.current.transition?.version).toBe(7));
    expect(lookups).toBe(1);
    expect(posted).toHaveLength(2);
    expect(randomUUID).not.toHaveBeenCalled();
  });

  it("keeps an unrecognized original status unresolved even when it includes a result", async () => {
    installReads();
    document.cookie = "sidera_csrf=owned-csrf; Path=/";
    const originalHolder: { current: CommandRequest | null } = { current: null };
    let posts = 0;
    server.use(http.post("/api/v1/commands", async ({ request }) => {
      posts += 1;
      originalHolder.current = await request.json() as CommandRequest;
      return HttpResponse.error();
    }));
    const view = mountHook();
    await waitFor(() => expect(view.result.current.target).toEqual(profileTarget));
    act(() => {
      view.result.current.setReason("Change the implementation owner");
      view.result.current.setInstruction("Continue the validation step");
    });
    await waitFor(() => expect(view.result.current.canPrepare).toBe(true));
    await act(async () => view.result.current.prepare());
    await waitFor(() => expect(view.result.current.actions.prepare.status).toBe("unknown"));

    const storedOriginal = originalHolder.current;
    if (storedOriginal === null) throw new Error("Expected a frozen original Prepare command");
    const result: CommandResponse = {
      command_id: storedOriginal.command_id,
      aggregate_id: proposal.id,
      aggregate_version: proposal.version,
      event_ids: [],
      data: { proposal },
    };
    server.use(http.get(`${base}/commands/${storedOriginal.command_id}`, () => HttpResponse.json({
      command_id: storedOriginal.command_id,
      command_type: storedOriginal.command_type,
      target_type: storedOriginal.target_type,
      target_id: storedOriginal.target_id,
      expected_version: storedOriginal.expected_version,
      status: "in_progress",
      result,
    })));
    await act(async () => view.result.current.lookupOriginal());

    expect(view.result.current.actions.prepare.status).toBe("unknown");
    expect(view.result.current.prepared).toBeNull();
    expect(view.result.current.originalLookupNeeded).toBe(true);
    expect(posts).toBe(1);
  });

  it("keeps the frozen prepared proposal when cancelling before activation", async () => {
    installReads();
    document.cookie = "sidera_csrf=owned-csrf; Path=/";
    const posted: CommandRequest[] = [];
    const cancelledProposal = {
      ...proposal,
      version: 2,
      status: "cancelled",
      cancelled_at: "2026-10-07T00:02:00Z",
    } satisfies NativeHandoffProposal;
    server.use(http.post("/api/v1/commands", async ({ request }) => {
      const command = await request.json() as CommandRequest;
      posted.push(command);
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
        aggregate_id: proposal.id,
        aggregate_version: cancelledProposal.version,
        event_ids: [],
        data: { proposal: cancelledProposal },
      } satisfies CommandResponse);
    }));
    const view = mountHook();
    await waitFor(() => expect(view.result.current.target).toEqual(profileTarget));
    act(() => {
      view.result.current.setReason("Change the implementation owner");
      view.result.current.setInstruction("Continue the validation step");
    });
    await waitFor(() => expect(view.result.current.canPrepare).toBe(true));
    await act(async () => view.result.current.prepare());
    await waitFor(() => expect(view.result.current.canCancel).toBe(true));
    const prepareCommand = posted[0];
    if (prepareCommand === undefined) throw new Error("Expected the posted Prepare command");
    expect(prepareCommand).toMatchObject({
      command_type: "PrepareProfileHandoff",
      payload: {
        handoff_reason: "Change the implementation owner",
        handoff_instruction: "Continue the validation step",
      },
    });
    await act(async () => view.result.current.cancel());

    await waitFor(() => expect(view.result.current.actions.cancel.status).toBe("applied"));
    expect(posted).toHaveLength(2);
    expect(posted[1]).toMatchObject({
      command_type: "CancelWorkerHandoff",
      target_type: "handoff_proposal",
      target_id: proposalId,
      expected_version: 1,
      payload: { reason: "Cancel before target activation" },
    });
    expect(view.result.current.prepared?.proposal).toMatchObject({ id: proposalId, version: 1, status: "prepared" });
    expect(view.result.current.proposal).toMatchObject({ id: proposalId, version: 2, status: "cancelled" });
    expect(view.result.current.transitionId).toBeNull();
  });

  it("keeps applied-without-mapping locked and allows a current-human target", async () => {
    installReads();
    document.cookie = "sidera_csrf=owned-csrf; Path=/";
    let posts = 0;
    const originalHolder: { current: CommandRequest | null } = { current: null };
    server.use(
      http.post("/api/v1/commands", async ({ request }) => {
        posts += 1;
        const original = await request.json() as CommandRequest;
        originalHolder.current = original;
        return HttpResponse.json({
          command_id: original.command_id,
          aggregate_id: proposal.id,
          aggregate_version: proposal.version,
          event_ids: [],
          data: null,
        });
      }),
    );
    const view = mountHook(humanTarget);
    await waitFor(() => expect(view.result.current.sourceReview).not.toBeNull());
    act(() => {
      view.result.current.setReason("Move execution to the current human");
      view.result.current.setInstruction("Wait for a human to continue");
    });
    await waitFor(() => expect(view.result.current.canPrepare).toBe(true));
    await act(async () => view.result.current.prepare());

    await waitFor(() => expect(view.result.current.actions.prepare.status).toBe("applied"));
    expect(view.result.current.prepared).toBeNull();
    expect(view.result.current.originalLookupNeeded).toBe(true);
    expect(view.result.current.canPrepare).toBe(false);
    expect(view.result.current.target).toEqual(humanTarget);
    expect(posts).toBe(1);
    const original = originalHolder.current;
    if (original === null) throw new Error("Expected the submitted Prepare original");
    expect(original).toMatchObject({
      payload: { target_assignee: { kind: "human", id: principalId }, target_profile_ref: null },
    });
  });

  it("does not submit when scoped draft storage fails", async () => {
    installReads();
    document.cookie = "sidera_csrf=owned-csrf; Path=/";
    let posts = 0;
    server.use(http.post("/api/v1/commands", () => {
      posts += 1;
      return HttpResponse.json({});
    }));
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("storage denied");
    });
    const view = mountHook();

    await waitFor(() => expect(view.result.current.storageError).not.toBeNull());
    await act(async () => view.result.current.prepare());
    expect(posts).toBe(0);
  });
});
