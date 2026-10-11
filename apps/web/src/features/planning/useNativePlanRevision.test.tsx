import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { assignmentQueryKey, assignmentTargetKey } from "@/features/assignments/api";
import { buildNativeCommand } from "@/features/assignments/commands";
import { profileAt } from "@/features/agents/testFixtures";
import type { AgentBindingView } from "@/features/assignments/types";
import type { LocalSession } from "@/features/local-session/api";
import { taskQueryKey } from "@/features/tasks/api";
import type { TaskView } from "@/features/tasks/types";
import { server } from "@/test/server";
import { reviewNativeRevision } from "./nativeRevisionReview";
import {
  planRevisionDiffQueryKey,
  planningRequestQueryKey,
  planningRunsQueryKey,
} from "./api";
import { savePlanningDraft } from "./planningConfirmationDraft";
import type { PlanningConfirmationDraft, RevisionPlanningDraftScope } from "./planningConfirmationDraft";
import type { PlanningRequest, PlanningRun, RevisionDiff } from "./types";
import { useNativePlanRevision } from "./useNativePlanRevision";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const ROOT_TASK_ID = "30000000-0000-0000-0000-000000000001";
const REQUEST_ID = "40000000-0000-0000-0000-000000000001";
const RUN_ID = "50000000-0000-0000-0000-000000000001";
const PLANNING_PROFILE_ID = "60000000-0000-0000-0000-000000000001";
const AGENT_PROFILE_ID = "30000000-0000-0000-0000-000000000001";
const SNAPSHOT_ID = "80000000-0000-0000-0000-000000000001";
const RESULT_SNAPSHOT_ID = "80000000-0000-0000-0000-000000000002";
const RESULT_TRANSITION_ID = "81000000-0000-0000-0000-000000000001";
const EXISTING_SUBTASK_ID = "90000000-0000-0000-0000-000000000001";
const HASH = "a".repeat(64);
const PROPOSAL_HASH = "b".repeat(64);
const DIFF_HASH = "c".repeat(64);
const PRINCIPAL_ID = "human-current";
const session: LocalSession = {
  session_id: "a0000000-0000-0000-0000-000000000001",
  workspace_id: WORKSPACE_ID,
  principal_id: PRINCIPAL_ID,
  expires_at: "2099-01-01T00:00:00Z",
};
const base = {
  workspace_id: WORKSPACE_ID,
  root_task_id: ROOT_TASK_ID,
  plan_snapshot_id: SNAPSHOT_ID,
  plan_content_hash: HASH,
  plan_generation: 5,
  root_task_version: 11,
  schema_version: 1,
  captured_at: "2026-10-07T00:00:00Z",
};
const freshness = {
  base,
  base_fresh: true,
  checked_at: "2026-10-07T00:00:01Z",
  current_plan_content_hash: HASH,
  current_plan_generation: 5,
  current_plan_snapshot_id: SNAPSHOT_ID,
  current_root_task_version: 11,
  stale_reasons: [],
};

function run(overrides: Record<string, unknown> = {}): PlanningRun {
  return {
    id: RUN_ID,
    workspace_id: WORKSPACE_ID,
    planning_request_id: REQUEST_ID,
    brief_revision: 2,
    attempt: 1,
    operation_id: "b0000000-0000-0000-0000-000000000001",
    status: "succeeded",
    result_kind: "proposal",
    execution_mode: "native_profile",
    native_run: null,
    planner_agent_ref: null,
    capability_snapshot: null,
    usage: { input_tokens: 1, output_tokens: 1, runtime_seconds: 1, context_bytes: 1, cost_microunits: 1 },
    provider_ref: null,
    error_code: null,
    queued_at: "2026-10-07T00:00:00Z",
    started_at: "2026-10-07T00:00:01Z",
    finished_at: "2026-10-07T00:00:02Z",
    version: 2,
    ...overrides,
  } as PlanningRun;
}

function request(overrides: Record<string, unknown> = {}): PlanningRequest {
  return {
    id: REQUEST_ID,
    workspace_id: WORKSPACE_ID,
    kind: "revision",
    status: "proposal_ready",
    current_brief_revision: 2,
    current_proposal_revision: 2,
    current_proposal_content_hash: PROPOSAL_HASH,
    planning_profile_id: PLANNING_PROFILE_ID,
    planning_profile_version: 3,
    profile_key: "native-planner",
    current_run_id: RUN_ID,
    requires_attention: true,
    version: 4,
    created_at: "2026-10-07T00:00:00Z",
    updated_at: "2026-10-07T00:00:02Z",
    profile: {
      id: PLANNING_PROFILE_ID,
      workspace_id: WORKSPACE_ID,
      execution_mode: "native_profile",
    },
    active_run: run(),
    pending_clarifications: [],
    usage: { input_tokens: 1, output_tokens: 1, runtime_seconds: 1, context_bytes: 1, cost_microunits: 1 },
    last_change_cursor: 1,
    source_ref: null,
    source_type: "plan_revision",
    revision_base: base,
    revision_freshness: freshness,
    ...overrides,
  } as unknown as PlanningRequest;
}

function diff(overrides: Record<string, unknown> = {}): RevisionDiff {
  return {
    workspace_id: WORKSPACE_ID,
    planning_request_id: REQUEST_ID,
    root_task_id: ROOT_TASK_ID,
    proposal_revision_id: "c0000000-0000-0000-0000-000000000001",
    proposal_revision: 2,
    proposal_content_hash: PROPOSAL_HASH,
    diff_hash: DIFF_HASH,
    base,
    freshness,
    target_descriptor_preview: {
      parent_snapshot_id: SNAPSHOT_ID,
      target_generation: 6,
      target_plan_content_hash: "d".repeat(64),
    },
    impact_counts: {
      preserve: 0, move: 0, patch: 0, continue: 0, create: 2, derive: 1, retire: 0, affected_branches: 1,
    },
    risk_flags: [],
    affected_branch_keys: ["api"],
    affected_branch_paths: ["Root / API"],
    root_changes: [],
    subtask_changes: [
      {
        target_subtask_key: "api",
        path: "Root / API",
        strategy: "reuse",
        reason: "Preserve canonical owner",
        existing_task_id: EXISTING_SUBTASK_ID,
        field_changes: [],
      },
      {
        target_subtask_key: "new-subtask",
        path: "Root / New subtask",
        strategy: "create",
        reason: "New branch",
        existing_task_id: null,
        field_changes: [],
      },
    ],
    worker_item_changes: [{
      target_worker_item_key: "derived-worker",
      path: "Root / API / Derived worker",
      strategy: "derive",
      context_disposition: "derive",
      from_subtask_key: null,
      to_subtask_key: "api",
      existing_worker_item_id: null,
      reasons: [],
      field_changes: [],
    }],
    subtask_dependency_changes: [],
    worker_dependency_changes: [],
    transition_preview: [],
    accepted_transition_policy: {
      activate_immediately: true,
      budget_level: "standard",
      failure_policy: "block_affected_branch",
      manual_gate: false,
      revision_operation: "patch_existing",
    },
    ...overrides,
  } as unknown as RevisionDiff;
}

function rootTask(overrides: Record<string, unknown> = {}): TaskView {
  return {
    id: ROOT_TASK_ID,
    workspace_id: WORKSPACE_ID,
    title: "Root task",
    description: "Root task description",
    acceptance_criteria: [],
    requested_by: PRINCIPAL_ID,
    priority: 1,
    status: "running",
    version: 11,
    created_at: "2026-10-07T00:00:00Z",
    updated_at: "2026-10-07T00:00:02Z",
    ...overrides,
  } as TaskView;
}

function binding(version = 2): AgentBindingView {
  return {
    workspace_id: WORKSPACE_ID,
    target_type: "subtask",
    target_id: EXISTING_SUBTASK_ID,
    version,
    assignee: { kind: "profile", id: AGENT_PROFILE_ID },
    profile_ref: { workspace_id: WORKSPACE_ID, profile_id: AGENT_PROFILE_ID, revision: 1 },
    created_at: "2026-10-07T00:00:00Z",
    changed_at: "2026-10-07T00:00:01Z",
    runnable: false,
    not_runnable_reasons: ["profile_disabled"],
  } as AgentBindingView;
}

type NativeHookInput = {
  workspaceId: string;
  rootTaskId: string;
  requestId: string;
  request: PlanningRequest;
  diff: RevisionDiff;
  rootTask: TaskView;
  runs: readonly PlanningRun[] | undefined;
  onRefresh: () => Promise<void>;
};

function nativeInput(overrides: Partial<NativeHookInput> = {}): NativeHookInput {
  return {
    workspaceId: WORKSPACE_ID,
    rootTaskId: ROOT_TASK_ID,
    requestId: REQUEST_ID,
    request: request(),
    diff: diff(),
    rootTask: rootTask(),
    runs: [run()],
    onRefresh: vi.fn(async () => undefined),
    ...overrides,
  };
}

function legacyRequest(overrides: Record<string, unknown> = {}): PlanningRequest {
  return request({
    profile: {
      id: PLANNING_PROFILE_ID,
      workspace_id: WORKSPACE_ID,
      execution_mode: "legacy",
    },
    current_run_id: null,
    active_run: null,
    ...overrides,
  });
}

function allReuseDiff(): RevisionDiff {
  return diff({
    subtask_changes: [{
      target_subtask_key: "api",
      path: "Root / API",
      strategy: "reuse",
      reason: "Preserve canonical owner",
      existing_task_id: EXISTING_SUBTASK_ID,
      field_changes: [],
    }],
    worker_item_changes: [],
  });
}

function saveUnknownNativeOriginal(commandType = "ConfirmPlanRevision"): { scope: RevisionPlanningDraftScope; commandId: string } {
  const reviewed = reviewNativeRevision({
    workspaceId: WORKSPACE_ID,
    rootTaskId: ROOT_TASK_ID,
    requestId: REQUEST_ID,
    request: request(),
    diff: allReuseDiff(),
    rootTask: rootTask(),
    runs: [run()],
    reusedBindings: new Map([[assignmentTargetKey("subtask", EXISTING_SUBTASK_ID), binding()]]),
  });
  if (!reviewed.ok) throw new Error("native original fixture review failed");
  const scope: RevisionPlanningDraftScope = {
    kind: "revision",
    workspace_id: WORKSPACE_ID,
    request_id: REQUEST_ID,
    principal_id: PRINCIPAL_ID,
    root_task_id: ROOT_TASK_ID,
  };
  const original = buildNativeCommand({
    session,
    workspaceId: WORKSPACE_ID,
    commandType,
    targetType: "planning_request",
    targetId: REQUEST_ID,
    expectedVersion: reviewed.binding.requestVersion,
    payload: commandType === "ReviseFailedPlanningRequest" ? {
      failed_planning_run_id: RUN_ID, expected_failed_planning_run_version: 2, expected_work_context_version: 3,
      work_context_revision_id: SNAPSHOT_ID, work_context_content_hash: HASH, feedback: "Keep the original Task",
    } : {
      expected_proposal_revision: reviewed.binding.proposalRevision,
      expected_proposal_content_hash: reviewed.binding.proposalContentHash,
      expected_diff_hash: reviewed.binding.diffHash,
      expected_base_snapshot_id: reviewed.binding.baseSnapshotId,
      expected_base_content_hash: reviewed.binding.baseContentHash,
      profile_id: reviewed.binding.profileId,
      profile_version: reviewed.binding.profileVersion,
      assignee_bindings: [],
    },
  });
  const draft: PlanningConfirmationDraft = {
    ...scope,
    schema_version: 1,
    review: commandType === "ReviseFailedPlanningRequest" ? null : reviewed.binding,
    choices: [],
    original,
    status: "unknown",
  };
  if (!savePlanningDraft(scope, draft).ok) throw new Error("native original fixture did not persist");
  return { scope, commandId: original.command_id };
}

function installReadHandlers({
  assignment = binding(),
  emptyTargets = false,
}: {
  assignment?: AgentBindingView | null;
  emptyTargets?: boolean;
} = {}) {
  const currentDiff = emptyTargets
    ? diff({
      subtask_changes: [{
        target_subtask_key: "api",
        path: "Root / API",
        strategy: "reuse",
        reason: "Preserve canonical owner",
        existing_task_id: EXISTING_SUBTASK_ID,
        field_changes: [],
      }],
      worker_item_changes: [],
    })
    : diff();
  server.use(
    http.get("/product/v1/session", () => HttpResponse.json(session)),
    http.get(`/agents/v1/workspaces/${WORKSPACE_ID}/profiles`, () => HttpResponse.json({
      items: [profileAt(2)],
    })),
    http.get(`/agents/v1/workspaces/${WORKSPACE_ID}/profiles/${AGENT_PROFILE_ID}`, ({ request: req }) => {
      const revision = Number(new URL(req.url).searchParams.get("revision"));
      const profile = profileAt(revision);
      return HttpResponse.json(revision === 1
        ? { ...profile, status: { ...profile.status, enabled: false } }
        : profile);
    }),
    http.get(`/product/v1/workspaces/${WORKSPACE_ID}/defaults`, () => HttpResponse.json({
      workspace_id: WORKSPACE_ID,
      version: 2,
      main_profile_id: AGENT_PROFILE_ID,
      planner_profile_id: null,
      worker_profile_id: null,
    })),
    http.get(`/api/v1/workspaces/${WORKSPACE_ID}/assignments/subtask/${EXISTING_SUBTASK_ID}`, () =>
      assignment === null ? HttpResponse.json(null) : HttpResponse.json(assignment)),
    http.get(`/api/v1/workspaces/${WORKSPACE_ID}/planning-requests/${REQUEST_ID}`, () => HttpResponse.json(request())),
    http.get(`/api/v1/workspaces/${WORKSPACE_ID}/planning-requests/${REQUEST_ID}/runs`, () => HttpResponse.json({ items: [run()] })),
    http.get(`/api/v1/workspaces/${WORKSPACE_ID}/planning-requests/${REQUEST_ID}/diff`, () => HttpResponse.json(currentDiff)),
    http.get(`/api/v1/tasks/${ROOT_TASK_ID}`, () => HttpResponse.json(rootTask())),
  );
}

function mountHook(input: NativeHookInput = nativeInput(), seedQueryCache = false) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  if (seedQueryCache) {
    queryClient.setQueryData(planningRequestQueryKey(WORKSPACE_ID, REQUEST_ID), input.request);
    queryClient.setQueryData(planRevisionDiffQueryKey(WORKSPACE_ID, REQUEST_ID), input.diff);
    queryClient.setQueryData(taskQueryKey(WORKSPACE_ID, ROOT_TASK_ID), input.rootTask);
    if (input.runs !== undefined) {
      queryClient.setQueryData(planningRunsQueryKey(WORKSPACE_ID, REQUEST_ID), {
        items: input.runs,
        next_cursor: null,
      });
    }
  }
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  return {
    ...renderHook(({ currentInput }) => useNativePlanRevision(currentInput as never), {
      initialProps: { currentInput: input },
      wrapper,
    }),
    queryClient,
  };
}

describe("native plan revision hook", () => {
  beforeEach(() => {
    server.resetHandlers();
    window.localStorage.clear();
    document.cookie = "sidera_csrf=phase2c-test; Path=/";
  });

  it("loads the exact saved R1 reuse binding while suggesting R2 only for NEW targets", async () => {
    installReadHandlers();
    const { result } = mountHook();

    await waitFor(() => expect(result.current.targets).toHaveLength(3));
    await waitFor(() => expect(result.current.choices.size).toBe(2));

    expect(result.current.mode).toBe("native_profile");
    expect(result.current.reusedBindings.get(JSON.stringify(["subtask", EXISTING_SUBTASK_ID]))).toMatchObject({ version: 2 });
    expect(result.current.profiles.some((profile) => profile.profile.ref.revision === 1
      && profile.status.enabled === false)).toBe(true);
    expect(result.current.profiles.some((profile) => profile.profile.ref.revision === 2
      && profile.status.enabled === true)).toBe(true);
    expect(result.current.choices.has(JSON.stringify(["subtask", "api"]))).toBe(false);
    expect(result.current.canConfirm).toBe(true);
  });

  it("confirms only NEW create and derive choices with the reviewed request/base pins", async () => {
    installReadHandlers();
    const captured: { sent?: Record<string, unknown> } = {};
    server.use(http.post("/api/v1/commands", async ({ request: req }) => {
      const command = await req.json() as Record<string, unknown>;
      captured.sent = command;
      return HttpResponse.json({
        command_id: command.command_id,
        aggregate_id: REQUEST_ID,
        aggregate_version: 5,
        event_ids: [],
        data: null,
      });
    }));
    const { result } = mountHook();
    await waitFor(() => expect(result.current.canConfirm).toBe(true));
    act(() => {
      result.current.onChoiceChange(JSON.stringify(["subtask", "new-subtask"]), {
        kind: "human",
        id: PRINCIPAL_ID,
        profileRef: null,
      });
    });

    await act(async () => { await result.current.confirm(); });

    expect(captured.sent).toMatchObject({
      command_type: "ConfirmPlanRevision",
      workspace_id: WORKSPACE_ID,
      actor: { type: "user", id: PRINCIPAL_ID },
      target_type: "planning_request",
      target_id: REQUEST_ID,
      expected_version: 4,
      payload: {
        expected_proposal_revision: 2,
        expected_proposal_content_hash: PROPOSAL_HASH,
        expected_diff_hash: DIFF_HASH,
        expected_base_snapshot_id: SNAPSHOT_ID,
        expected_base_content_hash: HASH,
        profile_id: PLANNING_PROFILE_ID,
        profile_version: 3,
        assignee_bindings: [
          {
            target_type: "subtask",
            target_key: "new-subtask",
            assignee_kind: "human",
            assignee_id: PRINCIPAL_ID,
            profile_revision: null,
            expected_binding_version: null,
          },
          {
            target_type: "worker_item",
            target_key: "derived-worker",
            assignee_kind: "profile",
            assignee_id: AGENT_PROFILE_ID,
            profile_revision: 2,
            expected_binding_version: null,
          },
        ],
      },
    });
    expect((captured.sent?.payload as Record<string, unknown>).assignee_bindings).not.toContainEqual(
      expect.objectContaining({ target_key: "api" }),
    );
  });

  it.each([
    {
      pin: "matching proposal and base pins",
      canonical: request({ status: "materialized", version: 5 }),
      expectedOutcome: "plan_enabled",
    },
    {
      pin: "proposal revision",
      canonical: request({ status: "materialized", version: 5, current_proposal_revision: 3 }),
      expectedOutcome: "command_applied",
    },
    {
      pin: "proposal content hash",
      canonical: request({ status: "materialized", version: 5, current_proposal_content_hash: "e".repeat(64) }),
      expectedOutcome: "command_applied",
    },
    {
      pin: "base snapshot",
      canonical: request({
        status: "materialized",
        version: 5,
        revision_base: { ...base, plan_snapshot_id: "80000000-0000-0000-0000-000000000002" },
      }),
      expectedOutcome: "command_applied",
    },
    {
      pin: "base content hash",
      canonical: request({
        status: "materialized",
        version: 5,
        revision_base: { ...base, plan_content_hash: "e".repeat(64) },
      }),
      expectedOutcome: "command_applied",
    },
    {
      pin: "base generation",
      canonical: request({
        status: "materialized",
        version: 5,
        revision_base: { ...base, plan_generation: 6 },
      }),
      expectedOutcome: "command_applied",
    },
    {
      pin: "base Root Task version",
      canonical: request({
        status: "materialized",
        version: 5,
        revision_base: { ...base, root_task_version: 12 },
      }),
      expectedOutcome: "command_applied",
    },
    {
      pin: "base schema version",
      canonical: request({
        status: "materialized",
        version: 5,
        revision_base: { ...base, schema_version: 2 },
      }),
      expectedOutcome: "command_applied",
    },
  ])("keeps an applied original locked when the canonical $pin is checked", async ({ canonical, expectedOutcome }) => {
    installReadHandlers();
    let publishCanonical = () => undefined;
    const onRefresh = vi.fn(async () => publishCanonical());
    let posts = 0;
    server.use(http.post("/api/v1/commands", async ({ request: req }) => {
      posts += 1;
      const command = await req.json() as { command_id: string };
      return HttpResponse.json({
        command_id: command.command_id,
        aggregate_id: REQUEST_ID,
        aggregate_version: 5,
        event_ids: [],
        data: {
          planning_request_id: REQUEST_ID,
          root_task_id: ROOT_TASK_ID,
          proposal_revision: 2,
          proposal_content_hash: PROPOSAL_HASH,
          diff_hash: DIFF_HASH,
          profile_id: PLANNING_PROFILE_ID,
          profile_version: 3,
          plan_snapshot_id: RESULT_SNAPSHOT_ID,
          plan_transition_id: RESULT_TRANSITION_ID,
          plan_generation: 6,
        },
      });
    }));
    const { result, queryClient } = mountHook(nativeInput({ onRefresh }), true);
    publishCanonical = () => {
      queryClient.setQueryData(planningRequestQueryKey(WORKSPACE_ID, REQUEST_ID), canonical);
    };

    await waitFor(() => expect(result.current.canConfirm).toBe(true));
    await act(async () => { await result.current.confirm(); });

    expect(onRefresh).toHaveBeenCalledTimes(1);
    expect(queryClient.getQueryData<PlanningRequest>(planningRequestQueryKey(WORKSPACE_ID, REQUEST_ID)))
      .toMatchObject({ status: "materialized", version: 5 });
    expect(result.current.command.status).toBe("applied");
    expect(result.current.command.original).toMatchObject({
      command_type: "ConfirmPlanRevision",
      target_id: REQUEST_ID,
      expected_version: 4,
      payload: {
        expected_proposal_revision: 2,
        expected_proposal_content_hash: PROPOSAL_HASH,
        expected_base_snapshot_id: SNAPSHOT_ID,
        expected_base_content_hash: HASH,
      },
    });
    expect(result.current.outcome).toBe(expectedOutcome);
    expect(result.current.canConfirm).toBe(false);

    await act(async () => { await result.current.confirm(); });

    expect(posts).toBe(1);
  });

  it("allows an all-REUSE revision with an empty assignee_bindings array", async () => {
    installReadHandlers({ emptyTargets: true });
    const captured: { payload?: Record<string, unknown> } = {};
    server.use(http.post("/api/v1/commands", async ({ request: req }) => {
      const command = await req.json() as Record<string, unknown>;
      captured.payload = command.payload as Record<string, unknown>;
      return HttpResponse.json({
        command_id: command.command_id,
        aggregate_id: REQUEST_ID,
        aggregate_version: 5,
        event_ids: [],
        data: null,
      });
    }));
    const { result } = mountHook(nativeInput({ diff: diff({
      subtask_changes: [{
        target_subtask_key: "api",
        path: "Root / API",
        strategy: "reuse",
        reason: "Preserve canonical owner",
        existing_task_id: EXISTING_SUBTASK_ID,
        field_changes: [],
      }],
      worker_item_changes: [],
    }) }));
    await waitFor(() => expect(result.current.canConfirm).toBe(true));

    await act(async () => { await result.current.confirm(); });

    expect(captured.payload?.assignee_bindings).toEqual([]);
  });

  it("disables confirmation when a required canonical REUSE binding is missing", async () => {
    installReadHandlers({ assignment: null });
    let posts = 0;
    server.use(http.post("/api/v1/commands", () => {
      posts += 1;
      return HttpResponse.json({});
    }));
    const { result } = mountHook();

    await waitFor(() => expect(result.current.disabledReason).toBeTruthy());
    await act(async () => { await result.current.confirm(); });

    expect(result.current.canConfirm).toBe(false);
    expect(posts).toBe(0);
  });

  it("preserves only the exact legacy null-triplet mode after a successful empty run read", () => {
    const legacy = request({
      profile: null,
      current_run_id: null,
      active_run: null,
    });
    const { result } = mountHook(nativeInput({ request: legacy, runs: [] }));

    expect(result.current.mode).toBe("legacy");
    expect(result.current.canConfirm).toBe(false);
  });

  it("preserves a canonical legacy Profile baseline with an empty scoped run list", () => {
    const { result } = mountHook(nativeInput({ request: legacyRequest(), runs: [] }));

    expect(result.current.mode).toBe("legacy");
    expect(result.current.canConfirm).toBe(false);
  });

  it.each(["ConfirmPlanRevision", "ReviseFailedPlanningRequest"])("blocks legacy downgrade with a persisted %s original and recovers only by lookup", async (commandType) => {
    installReadHandlers();
    const { commandId } = saveUnknownNativeOriginal(commandType);
    const legacy = legacyRequest();
    let reads = 0;
    let posts = 0;
    server.use(
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/commands/${commandId}`, () => {
        reads += 1;
        return HttpResponse.json({
          command_id: commandId,
          command_type: commandType,
          target_type: "planning_request",
          target_id: REQUEST_ID,
          expected_version: 4,
          status: "pending",
          result: null,
        });
      }),
      http.post("/api/v1/commands", () => {
        posts += 1;
        return HttpResponse.json({});
      }),
    );
    const { result } = mountHook(nativeInput({ request: legacy, runs: [] }));

    await waitFor(() => expect(result.current.command.original?.command_id).toBe(commandId));
    expect(result.current.mode).toBe("unavailable");
    expect(result.current.nativeHint).toBe(true);
    expect(result.current.canConfirm).toBe(false);
    await act(async () => { await result.current.command.lookupOriginal(); });

    expect(reads).toBe(1);
    expect(posts).toBe(0);
    expect(result.current.command.status).toBe("pending");
  });

  it("blocks legacy downgrade when the canonical same-request run list contains native evidence", () => {
    const { result } = mountHook(nativeInput({ request: legacyRequest(), runs: [run()] }));

    expect(result.current.mode).toBe("unavailable");
    expect(result.current.nativeHint).toBe(true);
    expect(result.current.canConfirm).toBe(false);
  });

  it("rejects foreign run metadata instead of accepting a legacy result", () => {
    const foreignRun = run({ workspace_id: "foreign-workspace" });
    const { result } = mountHook(nativeInput({ request: legacyRequest(), runs: [foreignRun] }));

    expect(result.current.mode).toBe("unavailable");
    expect(result.current.canConfirm).toBe(false);
  });

  it("keeps current page facts authoritative after revisit and requires a fresh explicit review", async () => {
    installReadHandlers();
    let remoteRequest = request({ version: 5 });
    let remoteDiff = diff({ diff_hash: "d".repeat(64) });
    server.use(
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/planning-requests/${REQUEST_ID}`, () => HttpResponse.json(remoteRequest)),
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/planning-requests/${REQUEST_ID}/runs`, () => HttpResponse.json({ items: [run({ version: 3 })] })),
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/planning-requests/${REQUEST_ID}/diff`, () => HttpResponse.json(remoteDiff)),
    );
    const initial = nativeInput();
    const { result, queryClient, rerender } = mountHook(initial, true);
    await waitFor(() => expect(result.current.canConfirm).toBe(true));
    act(() => {
      result.current.onChoiceChange(JSON.stringify(["subtask", "new-subtask"]), {
        kind: "human",
        id: PRINCIPAL_ID,
        profileRef: null,
      });
    });

    await act(async () => { await result.current.revisit(); });
    await waitFor(() => expect(result.current.reviewedBinding?.requestVersion).toBe(5));
    const requestKey = planningRequestQueryKey(WORKSPACE_ID, REQUEST_ID);
    const diffKey = planRevisionDiffQueryKey(WORKSPACE_ID, REQUEST_ID);
    const request5 = queryClient.getQueryData<PlanningRequest>(requestKey)!;
    const diff5 = queryClient.getQueryData<RevisionDiff>(diffKey)!;
    const task5 = queryClient.getQueryData<TaskView>(taskQueryKey(WORKSPACE_ID, ROOT_TASK_ID))!;
    const runs5 = queryClient.getQueryData<{ items: PlanningRun[] }>(planningRunsQueryKey(WORKSPACE_ID, REQUEST_ID))!;
    expect(request5).not.toBe(remoteRequest);
    rerender({ currentInput: { ...initial, request: request5, diff: diff5, rootTask: task5, runs: runs5.items } });
    await waitFor(() => expect(result.current.stale).toBe(false));

    remoteRequest = request({ version: 6 });
    remoteDiff = diff({ diff_hash: "e".repeat(64) });
    const request6 = queryClient.setQueryData<PlanningRequest>(requestKey, remoteRequest)!;
    const diff6 = queryClient.setQueryData<RevisionDiff>(diffKey, remoteDiff)!;
    expect(request6).not.toBe(remoteRequest);
    rerender({ currentInput: { ...initial, request: request6, diff: diff6, rootTask: task5, runs: runs5.items } });
    await waitFor(() => expect(result.current.stale).toBe(true));
    expect(result.current.reviewedBinding?.requestVersion).toBe(5);

    act(() => {
      result.current.onChoiceChange(JSON.stringify(["worker_item", "derived-worker"]), {
        kind: "human",
        id: PRINCIPAL_ID,
        profileRef: null,
      });
    });
    expect(result.current.stale).toBe(true);
    expect(result.current.reviewedBinding?.requestVersion).toBe(5);
    let posts = 0;
    server.use(http.post("/api/v1/commands", () => {
      posts += 1;
      return HttpResponse.json({});
    }));
    await act(async () => { await result.current.confirm(); });
    expect(posts).toBe(0);

    await act(async () => { await result.current.revisit(); });
    await waitFor(() => expect(result.current.reviewedBinding?.requestVersion).toBe(6));
    expect(result.current.stale).toBe(false);
    expect(result.current.canConfirm).toBe(true);
  });

  it("keeps an explicitly cleared owner unassigned through revisit and reload", async () => {
    installReadHandlers();
    const initial = mountHook();
    await waitFor(() => expect(initial.result.current.choices.size).toBe(2));
    const clearedKey = JSON.stringify(["subtask", "new-subtask"]);
    act(() => initial.result.current.onChoiceChange(clearedKey, null));
    expect(initial.result.current.choices.has(clearedKey)).toBe(false);

    await act(async () => { await initial.result.current.revisit(); });
    expect(initial.result.current.choices.has(clearedKey)).toBe(false);
    initial.unmount();

    const reloaded = mountHook();
    await waitFor(() => expect(reloaded.result.current.reviewedBinding).not.toBeNull());
    expect(reloaded.result.current.choices.has(clearedKey)).toBe(false);
    expect(reloaded.result.current.canConfirm).toBe(false);
  });

  it("keeps a binding drift stale after local owner edits and blocks POST", async () => {
    installReadHandlers();
    const { result, queryClient } = mountHook(nativeInput(), true);
    await waitFor(() => expect(result.current.canConfirm).toBe(true));
    act(() => {
      result.current.onChoiceChange(JSON.stringify(["subtask", "new-subtask"]), {
        kind: "human",
        id: PRINCIPAL_ID,
        profileRef: null,
      });
    });
    queryClient.setQueryData(
      assignmentQueryKey(WORKSPACE_ID, "subtask", EXISTING_SUBTASK_ID),
      binding(3),
    );
    await waitFor(() => expect(result.current.stale).toBe(true));
    act(() => {
      result.current.onChoiceChange(JSON.stringify(["worker_item", "derived-worker"]), {
        kind: "human",
        id: PRINCIPAL_ID,
        profileRef: null,
      });
    });
    let posts = 0;
    server.use(http.post("/api/v1/commands", () => {
      posts += 1;
      return HttpResponse.json({});
    }));

    await act(async () => { await result.current.confirm(); });

    expect(result.current.stale).toBe(true);
    expect(result.current.canConfirm).toBe(false);
    expect(posts).toBe(0);
  });

  it("keeps a native hint unavailable when run metadata is missing and never posts", async () => {
    installReadHandlers();
    let posts = 0;
    server.use(http.post("/api/v1/commands", () => {
      posts += 1;
      return HttpResponse.json({});
    }));
    const { result } = mountHook(nativeInput({ runs: undefined }));

    expect(result.current.mode).toBe("unavailable");
    expect(result.current.nativeHint).toBe(true);
    expect(result.current.canConfirm).toBe(false);
    await act(async () => { await result.current.confirm(); });
    expect(posts).toBe(0);
  });
});
