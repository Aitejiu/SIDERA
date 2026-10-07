import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { PROFILE, PROFILE_REF, profileAt } from "@/features/agents/testFixtures";
import { server } from "@/test/server";
import { planningProposalQueryKey, planningRequestQueryKey } from "./api";
import { loadPlanningDraft } from "./planningConfirmationDraft";
import { PlanningPage } from "./PlanningPage";

const { seedSnapshotCursor } = vi.hoisted(() => ({ seedSnapshotCursor: vi.fn() }));
vi.mock("@/app/WorkspaceStreamProvider", () => ({
  useWorkspaceStreamOptional: () => ({ seedSnapshotCursor }),
}));

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const REQUEST_ID = "30000000-0000-0000-0000-000000000001";
const REQUEST_B_ID = "30000000-0000-0000-0000-000000000002";
const RUN_ID = "40000000-0000-0000-0000-000000000001";
const PLANNING_PROFILE_ID = "50000000-0000-0000-0000-000000000001";
const PRINCIPAL_ID = "principal-current";
const DRAFT_SCOPE = {
  kind: "create" as const,
  workspace_id: WORKSPACE_ID,
  request_id: REQUEST_ID,
  principal_id: PRINCIPAL_ID,
};
const ROOT_TASK_ID = "a1000000-0000-0000-0000-000000000001";
const HASH = "a".repeat(64);
const usage = { input_tokens: 20, output_tokens: 10, cost_microunits: 1, runtime_seconds: 2, context_bytes: 80 };
const plannerProfile = {
  id: PLANNING_PROFILE_ID,
  workspace_id: WORKSPACE_ID,
  profile_key: "native-planner",
  version: 3,
  display_name: "Native planner",
  planner_provider: "reference",
  planner_module_instance_id: null,
  planner_module_config_version: null,
  planner_interface_version: "1.0",
  status: "active",
  user_selectable: true,
  capabilities: null,
  execution_mode: "native_profile",
  planner_agent_ref: null,
  created_at: "2026-10-01T00:00:00Z",
};

function makeRun(overrides: Record<string, unknown> = {}) {
  return {
    id: RUN_ID,
    workspace_id: WORKSPACE_ID,
    planning_request_id: REQUEST_ID,
    brief_revision: 1,
    attempt: 1,
    operation_id: "90000000-0000-0000-0000-000000000001",
    status: "succeeded",
    result_kind: "proposal",
    execution_mode: "native_profile",
    native_run: null,
    planner_agent_ref: PROFILE_REF,
    capability_snapshot: null,
    usage,
    provider_ref: null,
    error_code: null,
    queued_at: "2026-10-01T00:00:00Z",
    started_at: "2026-10-01T00:00:01Z",
    finished_at: "2026-10-01T00:00:02Z",
    version: 3,
    ...overrides,
  };
}

function makeRequest(overrides: Record<string, unknown> = {}) {
  return {
    id: REQUEST_ID,
    workspace_id: WORKSPACE_ID,
    kind: "create",
    status: "proposal_ready",
    current_brief_revision: 1,
    current_proposal_revision: 1,
    current_proposal_content_hash: HASH,
    planning_profile_id: PLANNING_PROFILE_ID,
    planning_profile_version: 3,
    profile_key: "native-planner",
    current_run_id: RUN_ID,
    requires_attention: false,
    version: 8,
    created_at: "2026-10-01T00:00:00Z",
    updated_at: "2026-10-01T00:00:02Z",
    profile: plannerProfile,
    active_run: makeRun(),
    pending_clarifications: [],
    usage,
    last_change_cursor: 1,
    source_ref: null,
    source_type: "user",
    ...overrides,
  };
}

function makeProposal(displayOverrides: Record<string, unknown> = {}) {
  return {
    id: "60000000-0000-0000-0000-000000000001",
    planning_request_id: REQUEST_ID,
    planning_run_id: RUN_ID,
    kind: "create",
    revision: 1,
    brief_revision: 1,
    content_hash: HASH,
    profile_version: 3,
    proposal_schema_version: 1,
    created_at: "2026-10-01T00:00:02Z",
    derived_diff: { root_task_added: true, immediately_ready_worker_item_keys: ["inspect"] },
    display: {
      root_task: { title: "Native proposal", goal_summary: "A native plan", acceptance_refs: [] },
      subtasks: [
        { key: "prepare", kind: "implementation", title: "Prepare", goal_summary: "Prepare", acceptance_refs: [], expected_outputs: [], milestone_key: null, source_refs: [] },
        { key: "verify", kind: "implementation", title: "Verify", goal_summary: "Verify", acceptance_refs: [], expected_outputs: [], milestone_key: null, source_refs: [] },
      ],
      worker_items: [
        { key: "inspect", subtask_key: "prepare", title: "Inspect", role: "review", responsibility: "Inspect", capability_requirements: [], worker_policy_ref: null, configuration_ref: null, concurrency_limit: 1, source_refs: [], pinned_worker_id: null },
        { key: "implement", subtask_key: "prepare", title: "Implement", role: "build", responsibility: "Implement", capability_requirements: [], worker_policy_ref: null, configuration_ref: null, concurrency_limit: 1, source_refs: [], pinned_worker_id: null },
        { key: "check", subtask_key: "verify", title: "Check", role: "test", responsibility: "Check", capability_requirements: [], worker_policy_ref: null, configuration_ref: null, concurrency_limit: 1, source_refs: [], pinned_worker_id: null },
      ],
      milestones: [],
      subtask_dependencies: [],
      worker_item_dependencies: [],
      execution_intents: [],
      decision_summary: { understanding: "Native proposal", assumptions: [], risks: [], estimated_tokens: 20, estimated_cost_microunits: 1, estimated_runtime_seconds: 2, estimated_concurrency: 1, scope_includes: [], scope_excludes: [] },
      impact_policy: { activate_immediately: false, budget_level: "standard", failure_policy: "block_downstream", manual_gate: true, revision_operation: null },
      ...displayOverrides,
    },
    collaboration: { mode: "enabled", default_strategy: "all_required", subtask_overrides: [], provider: "reference", runtime_interface: "collaboration-runtime.v1", runtime_config_version: 1, input_groups: [] },
  };
}

function installHandlers({
  sessionExpiry = "2099-01-01T00:00:00Z",
  sessionWorkspaceId = WORKSPACE_ID,
  profileEnabled = true,
  profileWorkspaceId = WORKSPACE_ID,
  defaultProfileId = PROFILE_REF.profile_id,
  clarifications = [],
  proposal = makeProposal(),
  request = makeRequest(),
}: {
  sessionExpiry?: string;
  sessionWorkspaceId?: string;
  profileEnabled?: boolean;
  profileWorkspaceId?: string;
  defaultProfileId?: string | null;
  clarifications?: Array<Record<string, unknown>>;
  proposal?: ReturnType<typeof makeProposal>;
  request?: ReturnType<typeof makeRequest>;
} = {}) {
  server.use(
    http.get(`/api/v1/workspaces/${WORKSPACE_ID}/planning-requests/${REQUEST_ID}`, () => HttpResponse.json(request)),
    http.get(`/api/v1/workspaces/${WORKSPACE_ID}/planning-requests/${REQUEST_ID}/runs`, () => HttpResponse.json({ items: [makeRun()] })),
    http.get(`/api/v1/workspaces/${WORKSPACE_ID}/planning-requests/${REQUEST_ID}/briefs`, () => HttpResponse.json({ items: [{ planning_request_id: REQUEST_ID, revision: 1, objective: "Native plan", constraints: [], acceptance_criteria: [], context_refs: [], requested_outputs: [], user_feedback: null, source_correlation: "80000000-0000-0000-0000-000000000001", created_by: PRINCIPAL_ID, created_at: "2026-10-01T00:00:00Z" }] })),
    http.get(`/api/v1/workspaces/${WORKSPACE_ID}/planning-requests/${REQUEST_ID}/clarifications`, () => HttpResponse.json({ items: clarifications })),
    http.get(`/api/v1/workspaces/${WORKSPACE_ID}/planning-requests/${REQUEST_ID}/proposals/1`, () => HttpResponse.json(proposal)),
    http.get("/product/v1/session", () => HttpResponse.json({
      session_id: "a0000000-0000-0000-0000-000000000001",
      workspace_id: sessionWorkspaceId,
      principal_id: PRINCIPAL_ID,
      expires_at: sessionExpiry,
    })),
    http.get(`/agents/v1/workspaces/${WORKSPACE_ID}/profiles`, () => HttpResponse.json({ items: [{
      ...PROFILE,
      profile: { ...PROFILE.profile, ref: { ...PROFILE_REF, workspace_id: profileWorkspaceId } },
      status: { ...PROFILE.status, enabled: profileEnabled },
    }] })),
    http.get(`/agents/v1/workspaces/${WORKSPACE_ID}/profiles/${PROFILE_REF.profile_id}`, () => HttpResponse.json(PROFILE)),
    http.get(`/product/v1/workspaces/${WORKSPACE_ID}/defaults`, () => HttpResponse.json({
      workspace_id: WORKSPACE_ID,
      version: 2,
      main_profile_id: defaultProfileId,
      planner_profile_id: null,
      worker_profile_id: null,
    })),
  );
}

function renderPage(showRouteControls = false) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const rendered = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/planning/${REQUEST_ID}`]}>
        {showRouteControls ? <RouteTestControls /> : null}
        <Routes>
          <Route path="/workspaces/:workspaceId/planning/:planningRequestId" element={<PlanningPage />} />
          <Route path="/workspaces/:workspaceId/tasks/:rootTaskId" element={<div data-testid="root-task-route">Root Task</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...rendered, queryClient };
}

function deferred<T = void>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  const promise = new Promise<T>((settle) => { resolve = settle; });
  return { promise, resolve };
}

function RouteTestControls() {
  const navigate = useNavigate();
  const location = useLocation();
  return (
    <>
      <button type="button" onClick={() => navigate(`/workspaces/${WORKSPACE_ID}/planning/${REQUEST_B_ID}`)}>
        打开规划 B
      </button>
      <output data-testid="active-route">{location.pathname}</output>
    </>
  );
}

describe("native initial planning page", () => {
  beforeEach(() => {
    server.resetHandlers();
    window.localStorage.clear();
    document.cookie = "sidera_csrf=phase2b-test; Path=/";
    seedSnapshotCursor.mockClear();
  });

  it("keeps five owner choices independent and submits their exact reviewed bindings once", async () => {
    installHandlers();
    let posts = 0;
    let submitted: Record<string, unknown> | null = null;
    let csrfHeader: string | null = null;
    server.use(http.post("/api/v1/commands", async ({ request }) => {
      posts += 1;
      csrfHeader = request.headers.get("X-Sidera-CSRF");
      submitted = await request.json() as Record<string, unknown>;
      return HttpResponse.json({
        command_id: submitted.command_id,
        aggregate_id: REQUEST_ID,
        aggregate_version: 9,
        event_ids: [],
        data: { root_task_id: ROOT_TASK_ID },
      });
    }));
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByLabelText("Prepare owner")).toHaveValue(JSON.stringify(["profile", PROFILE_REF.profile_id, WORKSPACE_ID, 1]));
    await user.selectOptions(screen.getByLabelText("Prepare owner"), "human");
    expect(screen.getByLabelText("Inspect owner")).toHaveValue(JSON.stringify(["profile", PROFILE_REF.profile_id, WORKSPACE_ID, 1]));
    expect(screen.getByLabelText("Implement owner")).toHaveValue(JSON.stringify(["profile", PROFILE_REF.profile_id, WORKSPACE_ID, 1]));
    await user.click(screen.getByRole("button", { name: "确认并创建任务" }));

    await waitFor(() => expect(posts).toBe(1));
    expect(submitted).toMatchObject({
      command_type: "ConfirmPlanningProposal",
      actor: { type: "user", id: PRINCIPAL_ID },
      target_type: "planning_request",
      target_id: REQUEST_ID,
      expected_version: 8,
      payload: {
        proposal_revision: 1,
        proposal_content_hash: HASH,
        profile_id: PLANNING_PROFILE_ID,
        profile_version: 3,
        assignee_bindings: [
          { target_type: "subtask", target_key: "prepare", assignee_kind: "human", assignee_id: PRINCIPAL_ID, profile_revision: null, expected_binding_version: null },
          { target_type: "subtask", target_key: "verify", assignee_kind: "profile", assignee_id: PROFILE_REF.profile_id, profile_revision: 1, expected_binding_version: null },
          { target_type: "worker_item", target_key: "check", assignee_kind: "profile", assignee_id: PROFILE_REF.profile_id, profile_revision: 1, expected_binding_version: null },
          { target_type: "worker_item", target_key: "implement", assignee_kind: "profile", assignee_id: PROFILE_REF.profile_id, profile_revision: 1, expected_binding_version: null },
          { target_type: "worker_item", target_key: "inspect", assignee_kind: "profile", assignee_id: PROFILE_REF.profile_id, profile_revision: 1, expected_binding_version: null },
        ],
      },
    });
    expect(csrfHeader).toBe("phase2b-test");
    expect(await screen.findByTestId("root-task-route")).toHaveTextContent("Root Task");
  });

  it("keeps Profile R1 and the reviewed anchor through edits until explicit revisit", async () => {
    installHandlers();
    let posts = 0;
    server.use(http.post("/api/v1/commands", () => {
      posts += 1;
      return HttpResponse.json({});
    }));
    const user = userEvent.setup();
    const { queryClient } = renderPage();
    const prepare = await screen.findByLabelText("Prepare owner");
    const profileR1 = JSON.stringify(["profile", PROFILE_REF.profile_id, WORKSPACE_ID, 1]);
    await waitFor(() => expect(prepare).toHaveValue(profileR1));
    const confirm = await screen.findByRole("button", { name: "确认并创建任务" });
    await waitFor(() => expect(confirm).toBeEnabled());
    await user.selectOptions(prepare, "human");
    expect(screen.getByRole("button", { name: "确认并创建任务" })).toBeEnabled();

    const nextHash = "b".repeat(64);
    const nextRequest = makeRequest({
      version: 9,
      current_proposal_revision: 2,
      current_proposal_content_hash: nextHash,
      planning_profile_version: 4,
      profile: { ...plannerProfile, version: 4 },
    });
    const nextProposal = { ...makeProposal(), revision: 2, content_hash: nextHash, profile_version: 4 };
    server.use(
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/planning-requests/${REQUEST_ID}`, () => HttpResponse.json(nextRequest)),
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/planning-requests/${REQUEST_ID}/runs`, () => HttpResponse.json({ items: [makeRun()] })),
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/planning-requests/${REQUEST_ID}/proposals/2`, () => HttpResponse.json(nextProposal)),
    );
    queryClient.setQueryData(planningRequestQueryKey(WORKSPACE_ID, REQUEST_ID), nextRequest);
    queryClient.setQueryData(planningProposalQueryKey(WORKSPACE_ID, REQUEST_ID, 2), nextProposal);
    queryClient.setQueryData(["agent-profiles", WORKSPACE_ID], { items: [profileAt(2)] });
    queryClient.setQueryData(["agent-defaults", WORKSPACE_ID], {
      workspace_id: WORKSPACE_ID,
      version: 3,
      main_profile_id: PROFILE_REF.profile_id,
      planner_profile_id: null,
      worker_profile_id: null,
    });

    expect(await screen.findByText("方案版本已变化，请重新查看后再确认。")).toBeVisible();
    expect(screen.getByRole("button", { name: "确认并创建任务" })).toBeDisabled();
    expect(screen.getByLabelText("Inspect owner")).toHaveValue(profileR1);
    await user.selectOptions(screen.getByLabelText("Implement owner"), "human");
    expect(screen.getByRole("button", { name: "确认并创建任务" })).toBeDisabled();
    expect(screen.getByLabelText("Inspect owner")).toHaveValue(profileR1);

    await user.click(screen.getByRole("button", { name: "重新查看最新方案" }));
    expect(await screen.findByText("审阅 V2 · 请求 V9")).toBeVisible();
    expect(screen.getByRole("button", { name: "确认并创建任务" })).toBeEnabled();
    expect(screen.getByLabelText("Prepare owner")).toHaveValue("human");
    expect(screen.getByLabelText("Implement owner")).toHaveValue("human");
    expect(screen.getByLabelText("Inspect owner")).toHaveValue(profileR1);
    expect(posts).toBe(0);
  });

  it("keeps a cleared owner unassigned when revisiting a stale proposal", async () => {
    installHandlers();
    let posts = 0;
    let submitted: Record<string, unknown> | null = null;
    server.use(http.post("/api/v1/commands", async ({ request }) => {
      posts += 1;
      submitted = await request.json() as Record<string, unknown>;
      return HttpResponse.json({
        command_id: submitted.command_id,
        aggregate_id: REQUEST_ID,
        aggregate_version: 10,
        event_ids: [],
        data: { root_task_id: ROOT_TASK_ID },
      });
    }));
    const user = userEvent.setup();
    const { queryClient } = renderPage();
    const profileR1 = JSON.stringify(["profile", PROFILE_REF.profile_id, WORKSPACE_ID, 1]);
    const ownerLabels = ["Prepare", "Verify", "Inspect", "Implement", "Check"];
    for (const label of ownerLabels) {
      await waitFor(() => expect(screen.getByLabelText(`${label} owner`)).toHaveValue(profileR1));
    }

    const prepareOwner = screen.getByLabelText("Prepare owner");
    await user.selectOptions(prepareOwner, "");
    expect(prepareOwner).toHaveValue("");

    const nextHash = "b".repeat(64);
    const nextRequest = makeRequest({
      version: 9,
      current_proposal_revision: 2,
      current_proposal_content_hash: nextHash,
      planning_profile_version: 4,
      profile: { ...plannerProfile, version: 4 },
    });
    const nextProposal = { ...makeProposal(), revision: 2, content_hash: nextHash, profile_version: 4 };
    server.use(
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/planning-requests/${REQUEST_ID}`, () => HttpResponse.json(nextRequest)),
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/planning-requests/${REQUEST_ID}/runs`, () => HttpResponse.json({ items: [makeRun()] })),
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/planning-requests/${REQUEST_ID}/proposals/2`, () => HttpResponse.json(nextProposal)),
    );
    queryClient.setQueryData(planningRequestQueryKey(WORKSPACE_ID, REQUEST_ID), nextRequest);
    queryClient.setQueryData(planningProposalQueryKey(WORKSPACE_ID, REQUEST_ID, 2), nextProposal);
    queryClient.setQueryData(["agent-profiles", WORKSPACE_ID], { items: [profileAt(2)] });
    queryClient.setQueryData(["agent-defaults", WORKSPACE_ID], {
      workspace_id: WORKSPACE_ID,
      version: 3,
      main_profile_id: PROFILE_REF.profile_id,
      planner_profile_id: null,
      worker_profile_id: null,
    });

    expect(await screen.findByText("方案版本已变化，请重新查看后再确认。")).toBeVisible();
    expect(await screen.findByText("Prepare 尚未分配负责人")).toBeVisible();
    expect(screen.getByRole("button", { name: "确认并创建任务" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "重新查看最新方案" }));

    expect(await screen.findByText("审阅 V2 · 请求 V9")).toBeVisible();
    await waitFor(() => expect(screen.getByLabelText("Prepare owner")).toHaveValue(""));
    for (const label of ownerLabels.slice(1)) {
      expect(screen.getByLabelText(`${label} owner`)).toHaveValue(profileR1);
    }
    expect(screen.getByRole("button", { name: "确认并创建任务" })).toBeDisabled();
    expect(posts).toBe(0);

    await user.selectOptions(screen.getByLabelText("Prepare owner"), "human");
    const confirm = screen.getByRole("button", { name: "确认并创建任务" });
    await waitFor(() => expect(confirm).toBeEnabled());
    await user.click(confirm);
    await waitFor(() => expect(posts).toBe(1));
    expect(submitted).toMatchObject({
      expected_version: 9,
      payload: {
        proposal_revision: 2,
        proposal_content_hash: nextHash,
        profile_id: PLANNING_PROFILE_ID,
        profile_version: 4,
      },
    });
  });

  it("persists a lost confirmation before POST and recovers only its original after reload", async () => {
    installHandlers();
    let posts = 0;
    let reads = 0;
    let postedCommandId: string | null = null;
    const persistedAtPost: Array<ReturnType<typeof loadPlanningDraft>> = [];
    let lookupPath = "";
    server.use(
      http.post("/api/v1/commands", async ({ request }) => {
        posts += 1;
        const submitted = await request.json() as Record<string, unknown>;
        postedCommandId = String(submitted.command_id);
        persistedAtPost.push(loadPlanningDraft(DRAFT_SCOPE));
        return HttpResponse.error();
      }),
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/commands/:commandId`, ({ request, params }) => {
        reads += 1;
        lookupPath = new URL(request.url).pathname;
        expect(params.commandId).toBe(postedCommandId);
        return HttpResponse.json({
          command_id: postedCommandId,
          command_type: "ConfirmPlanningProposal",
          target_type: "planning_request",
          target_id: REQUEST_ID,
          expected_version: 8,
          status: "applied",
          result: null,
        });
      }),
    );
    const user = userEvent.setup();
    const firstMount = renderPage();

    expect(await screen.findByLabelText("Prepare owner")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "确认并创建任务" }));
    expect(await screen.findByText("原始命令结果未知")).toBeVisible();
    expect(posts).toBe(1);
    expect(persistedAtPost[0]?.ok).toBe(true);
    if (persistedAtPost[0]?.ok) {
      expect(persistedAtPost[0].draft?.status).toBe("pending");
      expect(persistedAtPost[0].draft?.original?.command_id).toBe(postedCommandId);
      expect(persistedAtPost[0].draft?.original?.command_type).toBe("ConfirmPlanningProposal");
    }
    expect(screen.getByRole("button", { name: "正在创建任务" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "核对原始命令" })).toBeVisible();
    firstMount.unmount();

    installHandlers({ request: makeRequest({ status: "materialized" }) });
    renderPage();
    expect(await screen.findByText("原始命令结果未知")).toBeVisible();
    expect(screen.getByRole("group", { name: "原始命令恢复" })).toBeVisible();
    expect(posts).toBe(1);
    await user.click(screen.getByRole("button", { name: "核对原始命令" }));

    expect(await screen.findByText("原始命令已生效")).toBeVisible();
    expect(reads).toBe(1);
    expect(lookupPath).toBe(`/api/v1/workspaces/${WORKSPACE_ID}/commands/${postedCommandId}`);
    expect(posts).toBe(1);
    expect(screen.queryByTestId("root-task-route")).not.toBeInTheDocument();
  });

  it("does not navigate from A after its applied refresh resolves on planning B", async () => {
    const refreshStarted = deferred<void>();
    const releaseRefresh = deferred<void>();
    let requestReads = 0;
    let posts = 0;
    installHandlers();
    server.use(
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/planning-requests/${REQUEST_ID}`, async () => {
        requestReads += 1;
        if (requestReads > 1) {
          refreshStarted.resolve();
          await releaseRefresh.promise;
        }
        return HttpResponse.json(makeRequest());
      }),
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/planning-requests/${REQUEST_B_ID}`, () => HttpResponse.json(makeRequest({
        id: REQUEST_B_ID,
        status: "planning",
        current_run_id: null,
        active_run: null,
      }))),
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/planning-requests/${REQUEST_B_ID}/runs`, () => HttpResponse.json({ items: [] })),
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/planning-requests/${REQUEST_B_ID}/briefs`, () => HttpResponse.json({ items: [] })),
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/planning-requests/${REQUEST_B_ID}/clarifications`, () => HttpResponse.json({ items: [] })),
      http.post("/api/v1/commands", async ({ request }) => {
        posts += 1;
        const submitted = await request.json() as Record<string, unknown>;
        return HttpResponse.json({
          command_id: submitted.command_id,
          aggregate_id: REQUEST_ID,
          aggregate_version: 9,
          event_ids: [],
          data: { root_task_id: ROOT_TASK_ID },
        });
      }),
    );
    const user = userEvent.setup();
    renderPage(true);

    const confirm = await screen.findByRole("button", { name: "确认并创建任务" });
    await waitFor(() => expect(confirm).toBeEnabled());
    await user.click(confirm);
    await refreshStarted.promise;
    await user.click(screen.getByRole("button", { name: "打开规划 B" }));
    expect(await screen.findByTestId("active-route")).toHaveTextContent(`/workspaces/${WORKSPACE_ID}/planning/${REQUEST_B_ID}`);
    releaseRefresh.resolve();

    await waitFor(() => expect(screen.getByTestId("active-route")).toHaveTextContent(`/workspaces/${WORKSPACE_ID}/planning/${REQUEST_B_ID}`));
    expect(screen.queryByTestId("root-task-route")).not.toBeInTheDocument();
    expect(screen.queryByText("确认已处理，但没有可验证的 Root Task 映射。")).not.toBeInTheDocument();
    expect(posts).toBe(1);
  });

  it("does not write with an expired local session", async () => {
    installHandlers({ sessionExpiry: "2000-01-01T00:00:00Z" });
    let posts = 0;
    server.use(http.post("/api/v1/commands", () => {
      posts += 1;
      return HttpResponse.json({});
    }));
    renderPage();

    expect(await screen.findByRole("button", { name: "本地会话" })).toBeVisible();
    expect(await screen.findByRole("button", { name: "确认并创建任务" })).toBeDisabled();
    expect(posts).toBe(0);
  });

  it("does not write with a local session from another workspace", async () => {
    installHandlers({ sessionWorkspaceId: "foreign-workspace" });
    let posts = 0;
    server.use(http.post("/api/v1/commands", () => {
      posts += 1;
      return HttpResponse.json({});
    }));
    renderPage();

    expect(await screen.findByRole("button", { name: "本地会话" })).toBeVisible();
    expect(await screen.findByRole("button", { name: "确认并创建任务" })).toBeDisabled();
    expect(posts).toBe(0);
  });

  it.each([
    ["a missing main Profile suggestion", { defaultProfileId: null }],
    ["a disabled default Profile", { profileEnabled: false }],
    ["a foreign-workspace default Profile", { profileWorkspaceId: "foreign-workspace" }],
  ] as const)("does not invent an owner from %s", async (_name, options) => {
    installHandlers(options);
    let posts = 0;
    server.use(http.post("/api/v1/commands", () => {
      posts += 1;
      return HttpResponse.json({});
    }));
    renderPage();

    const owner = await screen.findByLabelText("Prepare owner");
    await waitFor(() => expect(owner).toBeEnabled());
    expect(owner).toHaveValue("");
    expect(await screen.findByText("Prepare 尚未分配负责人")).toBeVisible();
    expect(screen.getByRole("button", { name: "确认并创建任务" })).toBeDisabled();
    expect(posts).toBe(0);
  });

  it("keeps malformed proposal targets visible but disables confirmation without POST", async () => {
    const valid = makeProposal();
    const malformed = {
      ...valid,
      display: {
        ...valid.display,
        subtasks: valid.display.subtasks.map((subtask, index) => index === 0 ? { ...subtask, key: " " } : subtask),
      },
    };
    installHandlers({ proposal: malformed });
    let posts = 0;
    server.use(http.post("/api/v1/commands", () => {
      posts += 1;
      return HttpResponse.json({});
    }));
    renderPage();

    expect(await screen.findByText("规划方案事实不可用")).toBeVisible();
    expect(screen.getByRole("button", { name: "确认并创建任务" })).toBeDisabled();
    expect(posts).toBe(0);
  });

  it("answers a pre-proposal clarification with the current user and preserves its unanchored original", async () => {
    const clarification = {
      id: "70000000-0000-0000-0000-000000000001",
      planning_request_id: REQUEST_ID,
      planning_run_id: RUN_ID,
      question: "需要保留兼容模式吗？",
      rationale: "影响后续规划。",
      answer_type: "text",
      options: [],
      blocking: true,
      answer: null,
      answered_by: null,
      answered_at: null,
      answer_brief_revision: null,
      created_at: "2026-10-01T00:00:00Z",
      version: 1,
    };
    installHandlers({ request: makeRequest({
      status: "waiting_for_user",
      active_run: makeRun({ status: "needs_clarification" }),
      pending_clarifications: [clarification],
    }), clarifications: [clarification] });
    let submitted: Record<string, unknown> | null = null;
    let persistedAtPost: ReturnType<typeof loadPlanningDraft> | null = null;
    server.use(http.post("/api/v1/commands", async ({ request }) => {
      submitted = await request.json() as Record<string, unknown>;
      persistedAtPost = loadPlanningDraft(DRAFT_SCOPE);
      return HttpResponse.json({
        command_id: submitted.command_id,
        aggregate_id: REQUEST_ID,
        aggregate_version: 9,
        event_ids: [],
      });
    }));
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByText("需要保留兼容模式吗？")).toBeVisible();
    await user.type(screen.getByLabelText("澄清答复"), "保留");
    await user.click(screen.getByRole("button", { name: "提交澄清答复" }));
    await waitFor(() => expect(submitted).not.toBeNull());

    expect(submitted).toMatchObject({
      command_type: "AnswerPlanningClarification",
      actor: { type: "user", id: PRINCIPAL_ID },
      target_type: "planning_request",
      target_id: REQUEST_ID,
      expected_version: 8,
      payload: { answers: [{ clarification_id: clarification.id, answer: "保留" }] },
    });
    expect(persistedAtPost).toMatchObject({
      ok: true,
      draft: { status: "pending", review: null, original: { command_type: "AnswerPlanningClarification" } },
    });
  });

  it("cancels a pre-proposal native request through the same user command path", async () => {
    installHandlers({ request: makeRequest({
      status: "planning",
      active_run: makeRun({ status: "queued" }),
    }) });
    let submitted: Record<string, unknown> | null = null;
    let persistedAtPost: ReturnType<typeof loadPlanningDraft> | null = null;
    server.use(http.post("/api/v1/commands", async ({ request }) => {
      submitted = await request.json() as Record<string, unknown>;
      persistedAtPost = loadPlanningDraft(DRAFT_SCOPE);
      return HttpResponse.json({
        command_id: submitted.command_id,
        aggregate_id: REQUEST_ID,
        aggregate_version: 9,
        event_ids: [],
      });
    }));
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "取消规划" }));
    const dialog = await screen.findByRole("dialog", { name: "确认取消规划" });
    await user.click(within(dialog).getByRole("button", { name: "确认取消" }));
    await waitFor(() => expect(submitted).not.toBeNull());

    expect(submitted).toMatchObject({
      command_type: "CancelPlanningRequest",
      actor: { type: "user", id: PRINCIPAL_ID },
      target_type: "planning_request",
      target_id: REQUEST_ID,
      expected_version: 8,
      payload: {},
    });
    expect(persistedAtPost).toMatchObject({
      ok: true,
      draft: { status: "pending", review: null, original: { command_type: "CancelPlanningRequest" } },
    });
  });

  it("sends native revision feedback once as the current user with current proposal pins", async () => {
    installHandlers();
    let posts = 0;
    let submitted: Record<string, unknown> | null = null;
    server.use(http.post("/api/v1/commands", async ({ request }) => {
      posts += 1;
      submitted = await request.json() as Record<string, unknown>;
      return HttpResponse.json({
        command_id: submitted.command_id,
        aggregate_id: REQUEST_ID,
        aggregate_version: 9,
        event_ids: [],
      });
    }));
    const user = userEvent.setup();
    renderPage();

    const requestRevision = await screen.findByRole("button", { name: "要求修改" });
    await waitFor(() => expect(requestRevision).toBeEnabled());
    await user.click(requestRevision);
    await user.type(screen.getByLabelText("修改意见"), "请补充验证步骤");
    await user.click(screen.getByRole("button", { name: "提交修改意见" }));
    await waitFor(() => expect(posts).toBe(1));

    expect(submitted).toMatchObject({
      command_type: "RequestProposalRevision",
      actor: { type: "user", id: PRINCIPAL_ID },
      target_type: "planning_request",
      target_id: REQUEST_ID,
      expected_version: 8,
      payload: {
        current_proposal_revision: 1,
        current_proposal_content_hash: HASH,
        feedback: "请补充验证步骤",
      },
    });
  });

  it("rejects a native proposal once with the current user and proposal pins", async () => {
    installHandlers();
    let posts = 0;
    let submitted: Record<string, unknown> | null = null;
    server.use(http.post("/api/v1/commands", async ({ request }) => {
      posts += 1;
      submitted = await request.json() as Record<string, unknown>;
      return HttpResponse.json({
        command_id: submitted.command_id,
        aggregate_id: REQUEST_ID,
        aggregate_version: 9,
        event_ids: [],
      });
    }));
    const user = userEvent.setup();
    renderPage();

    const reject = await screen.findByRole("button", { name: "拒绝方案" });
    await waitFor(() => expect(reject).toBeEnabled());
    await user.click(reject);
    const dialog = await screen.findByRole("dialog", { name: "确认拒绝方案" });
    await user.click(within(dialog).getByRole("button", { name: "确认拒绝" }));
    await waitFor(() => expect(posts).toBe(1));

    expect(submitted).toMatchObject({
      command_type: "RejectPlanningProposal",
      actor: { type: "user", id: PRINCIPAL_ID },
      target_type: "planning_request",
      target_id: REQUEST_ID,
      expected_version: 8,
      payload: {
        current_proposal_revision: 1,
        current_proposal_content_hash: HASH,
        reason: "用户拒绝当前方案",
      },
    });
  });
});
