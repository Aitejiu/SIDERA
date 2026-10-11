import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { profileAt } from "@/features/agents/testFixtures";
import type { AgentBindingView, CommandRequest } from "@/features/assignments/types";
import type { WorkContextDetail } from "@/features/context/types";
import { server } from "@/test/server";
import { planningRequestQueryKey, planningRunsQueryKey } from "./api";
import { failedPlanningFeedbackDraftKey, reviewFailedPlanningFeedback } from "./failedPlanningFeedback.js";
import { loadPlanningDraft } from "./planningConfirmationDraft";
import { PlanRevisionPage } from "./PlanRevisionPage";
import type { PlanningRequest, PlanningRun } from "./types";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const ROOT_TASK_ID = "30000000-0000-0000-0000-000000000001";
const REQUEST_ID = "40000000-0000-0000-0000-000000000001";
const RUN_ID = "50000000-0000-0000-0000-000000000001";
const PLANNING_PROFILE_ID = "60000000-0000-0000-0000-000000000001";
const AGENT_PROFILE_ID = "30000000-0000-0000-0000-000000000001";
const SNAPSHOT_ID = "70000000-0000-0000-0000-000000000001";
const EXISTING_SUBTASK_ID = "80000000-0000-0000-0000-000000000001";
const PRINCIPAL_ID = "principal-current";
const HASH = "a".repeat(64);
const PROPOSAL_HASH = "b".repeat(64);
const DIFF_HASH = "c".repeat(64);
const DRAFT_SCOPE = { kind: "revision" as const, workspace_id: WORKSPACE_ID, request_id: REQUEST_ID,
  principal_id: PRINCIPAL_ID, root_task_id: ROOT_TASK_ID };

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

function run(overrides: Record<string, unknown> = {}) {
  return {
    id: RUN_ID,
    workspace_id: WORKSPACE_ID,
    planning_request_id: REQUEST_ID,
    brief_revision: 2,
    attempt: 1,
    operation_id: "90000000-0000-0000-0000-000000000001",
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
    version: 1,
    ...overrides,
  };
}

const request = {
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
  source_ref: null,
  source_type: "plan_revision",
  profile: { id: PLANNING_PROFILE_ID, workspace_id: WORKSPACE_ID, execution_mode: "native_profile" },
  active_run: run(),
  pending_clarifications: [],
  usage: { input_tokens: 1, output_tokens: 1, runtime_seconds: 1, context_bytes: 1, cost_microunits: 1 },
  last_change_cursor: 1,
  revision_base: base,
  revision_freshness: freshness,
};

const diff = {
  workspace_id: WORKSPACE_ID,
  planning_request_id: REQUEST_ID,
  root_task_id: ROOT_TASK_ID,
  proposal_revision_id: "a0000000-0000-0000-0000-000000000001",
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
      target_subtask_key: "new-api",
      path: "Root / New API",
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
};

const savedBinding: AgentBindingView = {
  workspace_id: WORKSPACE_ID,
  target_type: "subtask",
  target_id: EXISTING_SUBTASK_ID,
  version: 2,
  assignee: { kind: "profile", id: AGENT_PROFILE_ID },
  profile_ref: { workspace_id: WORKSPACE_ID, profile_id: AGENT_PROFILE_ID, revision: 1 },
  created_at: "2026-10-07T00:00:00Z",
  changed_at: "2026-10-07T00:00:01Z",
  runnable: false,
  not_runnable_reasons: ["profile_disabled"],
};

function failedFeedbackFacts() {
  const plannerRef = { workspace_id: WORKSPACE_ID, profile_id: AGENT_PROFILE_ID, revision: 1 };
  const failedRun: PlanningRun = { ...run(), execution_mode: "native_profile", status: "failed", result_kind: "failure",
    provider_ref: "agent-run", error_code: "invalid_output", planner_agent_ref: plannerRef,
    native_run: { status: "closed", closed: true, run_id: RUN_ID, response_id: SNAPSHOT_ID, event_cursor: 3,
      start_operation_id: "90000000-0000-0000-0000-000000000001", reason_code: null } };
  const failedRequest: PlanningRequest = { ...request, kind: "revision", status: "failed",
    current_proposal_revision: null, current_proposal_content_hash: null, active_run: failedRun,
    profile: { ...request.profile, execution_mode: "native_profile", status: "active", version: 3, planner_agent_ref: plannerRef,
      profile_key: "native-planner", display_name: "Planner", capabilities: null, planner_interface_version: "1.0",
      planner_module_instance_id: null, planner_module_config_version: null, planner_provider: null,
      user_selectable: true, created_at: "2026-10-07T00:00:00Z" } };
  const context: WorkContextDetail = { id: "c0000000-0000-0000-0000-000000000001", workspace_id: WORKSPACE_ID,
    planning_request_id: "e0000000-0000-0000-0000-000000000001", root_task_id: ROOT_TASK_ID, status: "active", version: 4,
    current_revision_number: 2, current_revision_id: "d0000000-0000-0000-0000-000000000001",
    created_at: "2026-10-07T00:00:00Z", updated_at: "2026-10-07T00:00:02Z", timeline: [], next_cursor: null,
    current_revision: { id: "d0000000-0000-0000-0000-000000000001", workspace_id: WORKSPACE_ID,
      work_context_id: "c0000000-0000-0000-0000-000000000001", revision_number: 2,
      brief_planning_request_id: REQUEST_ID, brief_revision: 2, content_hash: HASH,
      created_at: "2026-10-07T00:00:02Z", created_by: PRINCIPAL_ID, reason: "user_intervention",
      plan_snapshot_id: null, proposal_revision_id: null,
      snapshot: { schema_version: 1, objective: "Revise retained Task", constraints: [], acceptance_criteria: [], requested_outputs: [],
        accepted_decisions: [], clarification_decisions: [], unresolved_questions: [], artifact_refs: [], evidence_refs: [],
        checkpoint_refs: [], knowledge_refs: [], missing_fact_codes: [] } } };
  return { request: failedRequest, runs: [failedRun], context };
}

function installFailedFeedback() {
  const facts = failedFeedbackFacts(); installNativeHandlers();
  const reads = { request: 0, runs: 0, context: 0 };
  const path = `/api/v1/workspaces/${WORKSPACE_ID}/planning-requests/${REQUEST_ID}`;
  let currentDiff = diff;
  server.use(
    http.get(path, () => { reads.request += 1; return HttpResponse.json(facts.request); }),
    http.get(`${path}/runs`, () => { reads.runs += 1; return HttpResponse.json({ items: facts.runs }); }),
    http.get(`${path}/diff`, () => HttpResponse.json(currentDiff)),
    http.get(`/api/v1/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}/work-context`, () => { reads.context += 1; return HttpResponse.json(facts.context); }),
  );
  function successor() {
    const nextRun: PlanningRun = { ...facts.runs[0]!, id: "50000000-0000-0000-0000-000000000002",
      attempt: 2, brief_revision: 3, status: "succeeded", result_kind: "proposal", native_run: null };
    facts.runs = [...facts.runs, nextRun];
    facts.request = { ...facts.request, version: 6, status: "proposal_ready", current_run_id: nextRun.id, active_run: nextRun,
      current_brief_revision: 3, current_proposal_revision: 3, current_proposal_content_hash: "e".repeat(64) };
    facts.context = { ...facts.context, version: 5, current_revision: { ...facts.context.current_revision!, brief_revision: 3 } };
    currentDiff = { ...diff, proposal_revision: 3, proposal_content_hash: "e".repeat(64), diff_hash: "f".repeat(64) };
  }
  const reviewed = reviewFailedPlanningFeedback({ scope: DRAFT_SCOPE, ...facts });
  if (!reviewed.ok) throw new Error(reviewed.reason);
  return { facts, reads, successor, textKey: failedPlanningFeedbackDraftKey(reviewed.basis) };
}

async function enterFailedFeedback(user: ReturnType<typeof userEvent.setup>, text = "Retain the original Task") {
  await screen.findByRole("button", { name: "补充要求并重新规划" });
  await waitFor(() => expect(screen.getByRole("button", { name: "补充要求并重新规划" })).toBeEnabled());
  await user.click(screen.getByRole("button", { name: "补充要求并重新规划" }));
  await user.type(screen.getByLabelText("补充要求"), text);
}

function installNativeHandlers({
  requestView = request,
  runs = [run()],
  diffView = diff,
  assignment = savedBinding,
}: {
  requestView?: unknown;
  runs?: unknown[];
  diffView?: unknown;
  assignment?: AgentBindingView | null;
} = {}) {
  let currentRequest = requestView;
  server.use(
    http.get("/product/v1/session", () => HttpResponse.json({
      session_id: "b0000000-0000-0000-0000-000000000001",
      workspace_id: WORKSPACE_ID,
      principal_id: PRINCIPAL_ID,
      expires_at: "2099-01-01T00:00:00Z",
    })),
    http.get(`/agents/v1/workspaces/${WORKSPACE_ID}/profiles`, () => HttpResponse.json({ items: [profileAt(2)] })),
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
    http.get(`/api/v1/workspaces/${WORKSPACE_ID}/planning-requests/${REQUEST_ID}`, () =>
      HttpResponse.json(currentRequest as Record<string, unknown>)),
    http.get(`/api/v1/workspaces/${WORKSPACE_ID}/planning-requests/${REQUEST_ID}/runs`, () => HttpResponse.json({ items: runs })),
    http.get(`/api/v1/workspaces/${WORKSPACE_ID}/planning-requests/${REQUEST_ID}/diff`, () =>
      HttpResponse.json(diffView as Record<string, unknown>)),
    http.get(`/api/v1/tasks/${ROOT_TASK_ID}`, () => HttpResponse.json({
      id: ROOT_TASK_ID,
      workspace_id: WORKSPACE_ID,
      title: "Root",
      description: "Root description",
      acceptance_criteria: [],
      requested_by: "developer",
      priority: 1,
      status: "running",
      version: 11,
      created_at: "2026-10-07T00:00:00Z",
      updated_at: "2026-10-07T00:00:02Z",
    })),
  );
  return { setRequestView: (next: unknown) => { currentRequest = next; } };
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = () => (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}/revisions/${REQUEST_ID}`]}>
        <Routes>
          <Route
            path="/workspaces/:workspaceId/tasks/:rootTaskId/revisions/:planningRequestId"
            element={<PlanRevisionPage />}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
  return { ...render(<PlanRevisionPage />, { wrapper }), queryClient };
}

function deferred<T = void>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  const promise = new Promise<T>((settle) => { resolve = settle; });
  return { promise, resolve };
}

describe("native PlanRevisionPage", () => {
  beforeEach(() => {
    server.resetHandlers();
    vi.restoreAllMocks();
    window.localStorage.clear();
    document.cookie = "sidera_csrf=phase2c-page; Path=/";
  });

  it("opens failed planning feedback against the revision root context", async () => {
    installFailedFeedback();
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("button", { name: "补充要求并重新规划" });
    await waitFor(() => expect(screen.getByRole("button", { name: "补充要求并重新规划" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "补充要求并重新规划" }));
    expect(screen.getByLabelText("补充要求")).toBeVisible();
  });

  it("applies failed feedback without a plan outcome and initializes a usable new revision review", async () => {
    const setup = installFailedFeedback(); const sent: CommandRequest[] = [];
    window.localStorage.setItem("sidera:proposal-feedback:untouched", "proposal history");
    server.use(http.post("/api/v1/commands", async ({ request }) => {
      const command = await request.json() as CommandRequest; sent.push(command);
      expect(loadPlanningDraft(DRAFT_SCOPE)).toMatchObject({ ok: true, draft: { status: "pending", original: command } });
      setup.successor();
      return HttpResponse.json({ command_id: command.command_id, aggregate_id: REQUEST_ID, aggregate_version: 5, event_ids: [] });
    }));
    const user = userEvent.setup(); renderPage(); await enterFailedFeedback(user);
    const reads = { ...setup.reads };
    await user.dblClick(screen.getByRole("button", { name: "补充要求并重新规划" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "确认并启用新计划" })).toBeEnabled());
    expect(sent).toHaveLength(1);
    expect(sent[0]).toMatchObject({ command_type: "ReviseFailedPlanningRequest", expected_version: 4,
      payload: { failed_planning_run_id: RUN_ID, expected_failed_planning_run_version: 1,
        expected_work_context_version: 4, work_context_revision_id: "d0000000-0000-0000-0000-000000000001",
        work_context_content_hash: HASH, feedback: "Retain the original Task" } });
    expect(Object.keys(sent[0]!.payload)).toHaveLength(6);
    expect(window.localStorage.getItem(setup.textKey)).toBeNull();
    expect(window.localStorage.getItem("sidera:proposal-feedback:untouched")).toBe("proposal history");
    expect(loadPlanningDraft(DRAFT_SCOPE)).toMatchObject({ ok: true, draft: { status: "applied", original: sent[0], review: { proposalRevision: 3 } } });
    expect(screen.getByText("原始补充要求已处理")).toBeVisible();
    expect(screen.queryByText("原始确认已处理")).not.toBeInTheDocument();
    expect(screen.queryByText("新计划已启用")).not.toBeInTheDocument();
    expect(screen.queryByText(/命令已处理，等待/)).not.toBeInTheDocument();
    expect(screen.getByLabelText("Root / New API owner")).toBeEnabled();
    for (const key of ["request", "runs", "context"] as const) expect(setup.reads[key]).toBe(reads[key] + 1);
  });

  it("recovers failed feedback through pending and unknown reads then applies a null result without POST", async () => {
    const setup = installFailedFeedback(); let posts = 0, lookups = 0; let sent: CommandRequest | null = null; let status = "pending";
    server.use(http.post("/api/v1/commands", async ({ request }) => { posts += 1; sent = await request.json() as CommandRequest; return HttpResponse.error(); }),
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/commands/:commandId`, ({ params }) => {
        lookups += 1; expect(params.commandId).toBe(sent!.command_id); if (status === "applied") setup.successor();
        return HttpResponse.json({ command_id: sent!.command_id, command_type: sent!.command_type, target_type: "planning_request",
          target_id: REQUEST_ID, expected_version: 4, status, result: null });
      }));
    const user = userEvent.setup(); const first = renderPage(); await enterFailedFeedback(user);
    await user.click(screen.getByRole("button", { name: "补充要求并重新规划" }));
    await waitFor(() => expect(loadPlanningDraft(DRAFT_SCOPE)).toMatchObject({ ok: true, draft: { status: "unknown" } }));
    const bytes = window.localStorage.getItem(setup.textKey); first.unmount(); renderPage();
    await screen.findByRole("group", { name: "原始命令恢复" });
    for (const next of ["pending", "unknown", "applied"]) {
      status = next; await user.click(screen.getByRole("button", { name: "核对原始命令" }));
      await waitFor(() => expect(loadPlanningDraft(DRAFT_SCOPE)).toMatchObject({ ok: true, draft: { status: next, original: sent } }));
      if (next !== "applied") expect(window.localStorage.getItem(setup.textKey)).toBe(bytes);
    }
    await waitFor(() => expect(window.localStorage.getItem(setup.textKey)).toBeNull());
    await waitFor(() => expect(screen.getByRole("button", { name: "确认并启用新计划" })).toBeEnabled());
    expect(posts).toBe(1); expect(lookups).toBe(3); expect(screen.queryByText("新计划已启用")).not.toBeInTheDocument();
  });

  it("refreshes rejected feedback once and retains its text until an explicit recheck permits a new original", async () => {
    const setup = installFailedFeedback(); const sent: CommandRequest[] = []; let lookups = 0;
    server.use(http.post("/api/v1/commands", async ({ request }) => {
      sent.push(await request.json() as CommandRequest);
      return HttpResponse.json({ error: { code: "conflict", message: "stale" } }, { status: 409 });
    }), http.get(`/api/v1/workspaces/${WORKSPACE_ID}/commands/:commandId`, ({ params }) => {
      lookups += 1; setup.facts.request = { ...setup.facts.request, version: 5 };
      setup.facts.context = { ...setup.facts.context, version: 5 };
      return HttpResponse.json({ command_id: params.commandId, command_type: "ReviseFailedPlanningRequest", target_type: "planning_request",
        target_id: REQUEST_ID, expected_version: 4, status: "rejected", result: null });
    }));
    const user = userEvent.setup(); renderPage(); await enterFailedFeedback(user, "Keep retained choices");
    await user.click(screen.getByRole("button", { name: "补充要求并重新规划" }));
    await waitFor(() => expect(loadPlanningDraft(DRAFT_SCOPE)).toMatchObject({ ok: true, draft: { status: "unknown" } }));
    const reads = { ...setup.reads };
    expect(screen.getByLabelText("补充要求")).toHaveAttribute("readonly");
    await user.click(screen.getByRole("button", { name: "核对原始命令" }));
    await waitFor(() => expect(setup.reads.context).toBe(reads.context + 1));
    await waitFor(() => expect(screen.getByRole("button", { name: "补充要求并重新规划" })).toBeDisabled());
    expect(screen.getByLabelText("补充要求")).toHaveValue("Keep retained choices");
    expect(sent).toHaveLength(1); expect(lookups).toBe(1);
    await user.click(screen.getByRole("button", { name: "重新核对" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "补充要求并重新规划" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "补充要求并重新规划" }));
    await waitFor(() => expect(sent).toHaveLength(2));
    expect(sent[1]).toMatchObject({ expected_version: 5, payload: { expected_work_context_version: 5, feedback: "Keep retained choices" } });
    expect(sent[1]!.command_id).not.toBe(sent[0]!.command_id);
  });

  it("retains older revision review and owners across failed feedback until explicit revisit", async () => {
    installNativeHandlers(); const user = userEvent.setup(); const page = renderPage();
    await screen.findByLabelText("Root / New API owner");
    await waitFor(() => expect(screen.getByRole("button", { name: "确认并启用新计划" })).toBeEnabled());
    await user.selectOptions(screen.getByLabelText("Root / New API owner"), "human");
    const previous = loadPlanningDraft(DRAFT_SCOPE); if (!previous.ok || !previous.draft) throw new Error("missing review");
    const setup = installFailedFeedback();
    setup.facts.request = { ...setup.facts.request, current_proposal_revision: 2, current_proposal_content_hash: PROPOSAL_HASH };
    page.queryClient.setQueryData(planningRequestQueryKey(WORKSPACE_ID, REQUEST_ID), setup.facts.request);
    page.queryClient.setQueryData(planningRunsQueryKey(WORKSPACE_ID, REQUEST_ID), { items: setup.facts.runs });
    let sent: CommandRequest | null = null; let posts = 0;
    server.use(http.post("/api/v1/commands", async ({ request }) => {
      posts += 1; sent = await request.json() as CommandRequest; setup.successor();
      return HttpResponse.json({ command_id: sent.command_id, aggregate_id: REQUEST_ID, aggregate_version: 5, event_ids: [] });
    }));
    await enterFailedFeedback(user); await user.click(screen.getByRole("button", { name: "补充要求并重新规划" }));
    await waitFor(() => expect(loadPlanningDraft(DRAFT_SCOPE)).toMatchObject({ ok: true, draft: { status: "applied" } }));
    await screen.findByRole("button", { name: "重新审阅最新 Revision" });
    expect(screen.getByRole("button", { name: "确认并启用新计划" })).toBeDisabled();
    expect(loadPlanningDraft(DRAFT_SCOPE)).toMatchObject({ ok: true, draft: { review: previous.draft.review,
      choices: previous.draft.choices, original: sent, status: "applied" } });
    await user.click(screen.getByRole("button", { name: "重新审阅最新 Revision" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "确认并启用新计划" })).toBeEnabled());
    expect(screen.getByLabelText("Root / New API owner")).toHaveValue("human");
    expect(loadPlanningDraft(DRAFT_SCOPE)).toMatchObject({ ok: true, draft: { original: sent, status: "applied", review: { proposalRevision: 3 } } });
    expect(posts).toBe(1);
  });

  it.each(["POST", "lookup"] as const)("isolates delayed failed feedback %s cleanup from the next principal", async (transport) => {
    const setup = installFailedFeedback(); const started = deferred(), release = deferred();
    const sent: CommandRequest[] = []; let lookups = 0;
    server.use(http.post("/api/v1/commands", async ({ request }) => {
      const command = await request.json() as CommandRequest; sent.push(command);
      if (sent.length === 1) {
        if (transport === "lookup") return HttpResponse.error();
        started.resolve(); await release.promise;
      }
      return HttpResponse.json({ command_id: command.command_id, aggregate_id: REQUEST_ID, aggregate_version: 5, event_ids: [] });
    }), http.get(`/api/v1/workspaces/${WORKSPACE_ID}/commands/:commandId`, async ({ params }) => {
      lookups += 1; started.resolve(); await release.promise;
      return HttpResponse.json({ command_id: params.commandId, command_type: "ReviseFailedPlanningRequest", target_type: "planning_request",
        target_id: REQUEST_ID, expected_version: 4, status: "applied", result: null });
    }));
    const user = userEvent.setup(); const page = renderPage(); await enterFailedFeedback(user, "Private old text");
    const bytes = window.localStorage.getItem(setup.textKey);
    await user.click(screen.getByRole("button", { name: "补充要求并重新规划" }));
    if (transport === "lookup") {
      await waitFor(() => expect(loadPlanningDraft(DRAFT_SCOPE)).toMatchObject({ ok: true, draft: { status: "unknown" } }));
      await user.click(screen.getByRole("button", { name: "核对原始命令" }));
    }
    await started.promise;
    await act(async () => { page.queryClient.setQueryData(["local-session", WORKSPACE_ID], {
      session_id: "b0000000-0000-0000-0000-000000000002", workspace_id: WORKSPACE_ID,
      principal_id: "other-human", expires_at: "2099-01-01T00:00:00Z" }); });
    await enterFailedFeedback(user, "Other requirements");
    const reads = setup.reads.request;
    await act(async () => release.resolve());
    await user.click(screen.getByRole("button", { name: "补充要求并重新规划" }));
    const otherScope = { ...DRAFT_SCOPE, principal_id: "other-human" };
    await waitFor(() => expect(loadPlanningDraft(otherScope)).toMatchObject({ ok: true, draft: { status: "applied" } }));
    await waitFor(() => expect(setup.reads.request).toBe(reads + 1));
    expect(window.localStorage.getItem(setup.textKey)).toBe(bytes);
    expect(loadPlanningDraft(DRAFT_SCOPE)).toMatchObject({ ok: true, draft: { original: sent[0], status: transport === "POST" ? "pending" : "unknown" } });
    expect(sent).toHaveLength(2); expect(sent[1]!.actor.id).toBe("other-human"); expect(lookups).toBe(transport === "lookup" ? 1 : 0);
    expect(screen.queryByText("新计划已启用")).not.toBeInTheDocument();
  });

  it("uses exact saved REUSE pins and confirms only independently selected NEW owners", async () => {
    installNativeHandlers();
    const captured: { commandBody?: Record<string, unknown> } = {};
    server.use(http.post("/api/v1/commands", async ({ request: req }) => {
      const command = await req.json() as Record<string, unknown>;
      captured.commandBody = command;
      return HttpResponse.json({
        command_id: command.command_id,
        aggregate_id: REQUEST_ID,
        aggregate_version: 5,
        event_ids: [],
        data: null,
      });
    }));
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByLabelText("Root / New API owner")).toBeVisible();
    expect(await screen.findByText("Reusable reviewer · R1 · Binding V2")).toBeVisible();
    expect(screen.queryByRole("combobox", { name: "Root / API owner" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("Root / New API owner")).toHaveValue(JSON.stringify([
      "profile", AGENT_PROFILE_ID, WORKSPACE_ID, 2,
    ]));
    expect(screen.getByLabelText("Root / API / Derived worker owner")).toHaveValue(JSON.stringify([
      "profile", AGENT_PROFILE_ID, WORKSPACE_ID, 2,
    ]));

    await user.selectOptions(screen.getByLabelText("Root / New API owner"), "human");
    await user.click(screen.getByRole("button", { name: "确认并启用新计划" }));
    await waitFor(() => expect(captured.commandBody).toBeDefined());

    expect(captured.commandBody).toMatchObject({
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
            target_key: "new-api",
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
    expect(screen.getByText(/命令已处理/)).toBeVisible();
    expect(screen.queryByText("新计划已启用")).not.toBeInTheDocument();
  });

  it("submits an empty assignee list for an all-REUSE revision", async () => {
    installNativeHandlers({
      diffView: {
        ...diff,
        subtask_changes: [diff.subtask_changes[0]],
        worker_item_changes: [],
      },
    });
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
    const user = userEvent.setup();
    renderPage();

    const confirm = await screen.findByRole("button", { name: "确认并启用新计划" });
    await waitFor(() => expect(confirm).toBeEnabled());
    await user.click(confirm);

    await waitFor(() => expect(captured.payload).toBeDefined());
    expect(captured.payload?.assignee_bindings).toEqual([]);
  });

  it("rejects four mutually consistent foreign base descriptors without posting", async () => {
    const foreignBase = { ...base, workspace_id: "foreign-workspace" };
    const foreignRequest = {
      ...request,
      revision_base: foreignBase,
      revision_freshness: { ...freshness, base: foreignBase },
    };
    const foreignDiff = {
      ...diff,
      base: foreignBase,
      freshness: { ...freshness, base: foreignBase },
    };
    installNativeHandlers({ requestView: foreignRequest, diffView: foreignDiff });
    let posts = 0;
    server.use(http.post("/api/v1/commands", () => {
      posts += 1;
      return HttpResponse.json({});
    }));
    renderPage();

    expect(await screen.findByLabelText("Root / New API owner")).toBeVisible();
    const confirm = await screen.findByRole("button", { name: "确认并启用新计划" });
    expect(confirm).toBeDisabled();
    expect(posts).toBe(0);
  });

  it("keeps a foreign diff root readable but does not post", async () => {
    installNativeHandlers({ diffView: { ...diff, root_task_id: "foreign-root" } });
    let posts = 0;
    server.use(http.post("/api/v1/commands", () => {
      posts += 1;
      return HttpResponse.json({});
    }));
    renderPage();

    expect(await screen.findByText("Root / New API")).toBeVisible();
    expect(screen.getByRole("button", { name: "确认并启用新计划" })).toBeDisabled();
    expect(posts).toBe(0);
  });

  it("keeps a legacy Profile with native run evidence unavailable instead of enabling a writer", async () => {
    const legacyRequest = {
      ...request,
      profile: { ...request.profile, execution_mode: "legacy" },
      current_run_id: null,
      active_run: null,
    };
    installNativeHandlers({ requestView: legacyRequest, runs: [run()] });
    let posts = 0;
    server.use(http.post("/api/v1/commands", () => {
      posts += 1;
      return HttpResponse.json({});
    }));
    renderPage();

    const confirm = await screen.findByRole("button", { name: "确认并启用新计划" });
    expect(confirm).toBeDisabled();
    expect(await screen.findByText("无法核实当前原生规划模式")).toBeVisible();
    expect(posts).toBe(0);
  });

  it("keeps native hints unavailable after a failed run read and never posts", async () => {
    installNativeHandlers();
    let posts = 0;
    server.use(
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/planning-requests/${REQUEST_ID}/runs`, () =>
        HttpResponse.json({ error: { code: "unavailable", message: "read failed" } }, { status: 503 })),
      http.post("/api/v1/commands", () => {
        posts += 1;
        return HttpResponse.json({});
      }),
    );
    renderPage();

    expect(await screen.findByText("无法核实当前原生规划模式")).toBeVisible();
    expect(screen.getByRole("button", { name: "确认并启用新计划" })).toBeDisabled();
    expect(posts).toBe(0);
  });

  it("does not post with an expired Product session", async () => {
    installNativeHandlers();
    let posts = 0;
    server.use(
      http.get("/product/v1/session", () => HttpResponse.json({
        session_id: "b0000000-0000-0000-0000-000000000001",
        workspace_id: WORKSPACE_ID,
        principal_id: PRINCIPAL_ID,
        expires_at: "2000-01-01T00:00:00Z",
      })),
      http.post("/api/v1/commands", () => {
        posts += 1;
        return HttpResponse.json({});
      }),
    );
    renderPage();

    expect(await screen.findByText("需要当前工作区的有效本地会话")).toBeVisible();
    expect(screen.getByRole("button", { name: "确认并启用新计划" })).toBeDisabled();
    expect(posts).toBe(0);
  });

  it("does not post when the reviewed draft cannot be durably stored", async () => {
    installNativeHandlers();
    const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("storage unavailable", "QuotaExceededError");
    });
    let posts = 0;
    server.use(http.post("/api/v1/commands", () => {
      posts += 1;
      return HttpResponse.json({});
    }));
    renderPage();

    expect(await screen.findByLabelText("Root / New API owner")).toBeVisible();
    expect(screen.getByRole("button", { name: "确认并启用新计划" })).toBeDisabled();
    expect(setItem).toHaveBeenCalled();
    expect(posts).toBe(0);
  });

  it("keeps confirmation disabled when a canonical REUSE binding is denied", async () => {
    installNativeHandlers({ assignment: null });
    let posts = 0;
    server.use(http.post("/api/v1/commands", () => {
      posts += 1;
      return HttpResponse.json({});
    }));
    renderPage();

    const confirm = await screen.findByRole("button", { name: "确认并启用新计划" });
    expect(confirm).toBeDisabled();
    expect(await screen.findByText("Root / API 的已保存负责人不可用")).toBeVisible();
    expect(posts).toBe(0);
  });

  it("recovers an unknown original after materialization using GET only", async () => {
    const handlers = installNativeHandlers();
    const captured: { commandBody?: Record<string, unknown> } = {};
    let posts = 0;
    let reads = 0;
    server.use(
      http.post("/api/v1/commands", async ({ request: req }) => {
        posts += 1;
        captured.commandBody = await req.json() as Record<string, unknown>;
        return HttpResponse.error();
      }),
      http.get(/\/api\/v1\/workspaces\/[^/]+\/commands\//, ({ request: req }) => {
        reads += 1;
        const commandId = new URL(req.url).pathname.split("/").at(-1);
        return HttpResponse.json({
          command_id: commandId,
          command_type: "ConfirmPlanRevision",
          target_type: "planning_request",
          target_id: REQUEST_ID,
          expected_version: 4,
          status: "applied",
          result: {
            command_id: commandId,
            aggregate_id: REQUEST_ID,
            aggregate_version: 5,
            event_ids: [],
            data: null,
          },
        });
      }),
    );
    const user = userEvent.setup();
    const firstPage = renderPage();
    const confirm = await screen.findByRole("button", { name: "确认并启用新计划" });
    await waitFor(() => expect(confirm).toBeEnabled());
    await user.click(confirm);
    expect(await screen.findByRole("group", { name: "原始命令恢复" })).toBeVisible();
    expect(posts).toBe(1);
    expect(captured.commandBody?.command_type).toBe("ConfirmPlanRevision");

    handlers.setRequestView({
      ...request,
      status: "materialized",
      version: 5,
      current_proposal_revision: null,
      current_proposal_content_hash: null,
    });
    firstPage.unmount();
    renderPage();

    expect(await screen.findByText("新计划已启用")).toBeVisible();
    expect(await screen.findByRole("group", { name: "原始命令恢复" })).toBeVisible();
    await user.click(await screen.findByRole("button", { name: "核对原始命令" }));

    await waitFor(() => expect(reads).toBe(1));
    expect(posts).toBe(1);
    expect(await screen.findByText("原始命令已处理，但新计划状态尚未核实。")).toBeVisible();
    expect(screen.queryByText("新计划已启用，Worker 将按受影响分支异步收敛。")).not.toBeInTheDocument();
  });

  it("classifies an applied stale-base result as a conflict, not plan activation", async () => {
    installNativeHandlers();
    let posts = 0;
    server.use(http.post("/api/v1/commands", async ({ request: req }) => {
      posts += 1;
      const command = await req.json() as Record<string, unknown>;
      return HttpResponse.json({
        command_id: command.command_id,
        aggregate_id: REQUEST_ID,
        aggregate_version: 5,
        event_ids: [],
        data: {
          status: "conflicted",
          reason: "stale_base",
          planning_request_id: REQUEST_ID,
        },
      });
    }));
    const user = userEvent.setup();
    renderPage();

    const confirm = await screen.findByRole("button", { name: "确认并启用新计划" });
    await waitFor(() => expect(confirm).toBeEnabled());
    await user.click(confirm);

    expect(await screen.findByText("活动计划基准已变化；原始确认已处理，但新计划没有启用。", { exact: false })).toBeVisible();
    expect(confirm).toBeDisabled();
    expect(posts).toBe(1);
    expect(screen.queryByText("新计划已启用，Worker 将按受影响分支异步收敛。")).not.toBeInTheDocument();
  });

  it("keeps a newly selected root visible when a deferred command finishes", async () => {
    const postStarted = deferred();
    const releasePost = deferred();
    installNativeHandlers();
    server.use(http.post("/api/v1/commands", async ({ request: req }) => {
      const command = await req.json() as Record<string, unknown>;
      postStarted.resolve();
      await releasePost.promise;
      return HttpResponse.json({
        command_id: command.command_id,
        aggregate_id: REQUEST_ID,
        aggregate_version: 5,
        event_ids: [],
        data: null,
      });
    }));
    function SwitchableRoutes() {
      const navigate = useNavigate();
      return (
        <>
          <button type="button" onClick={() => navigate("/other-root")}>Switch root</button>
          <Routes>
            <Route
              path="/workspaces/:workspaceId/tasks/:rootTaskId/revisions/:planningRequestId"
              element={<PlanRevisionPage />}
            />
            <Route path="/other-root" element={<h1>Other root selected</h1>} />
          </Routes>
        </>
      );
    }
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = () => (
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}/revisions/${REQUEST_ID}`]}>
          <SwitchableRoutes />
        </MemoryRouter>
      </QueryClientProvider>
    );
    const user = userEvent.setup();
    render(<PlanRevisionPage />, { wrapper });

    const confirm = await screen.findByRole("button", { name: "确认并启用新计划" });
    await waitFor(() => expect(confirm).toBeEnabled());
    await user.click(confirm);
    await postStarted.promise;
    await user.click(screen.getByRole("button", { name: "Switch root" }));
    expect(await screen.findByRole("heading", { name: "Other root selected" })).toBeVisible();
    releasePost.resolve();
    expect(await screen.findByRole("heading", { name: "Other root selected" })).toBeVisible();
  });

  it.each(["mismatched", "not_found"] as const)(
    "keeps an unresolved original locked after a %s lookup",
    async (lookupResult) => {
      installNativeHandlers();
      let posts = 0;
      let reads = 0;
      server.use(
        http.post("/api/v1/commands", () => {
          posts += 1;
          return HttpResponse.error();
        }),
        http.get(/\/api\/v1\/workspaces\/[^/]+\/commands\//, ({ request: req }) => {
          reads += 1;
          if (lookupResult === "not_found") {
            return HttpResponse.json({ error: { code: "not_found", message: "missing" } }, { status: 404 });
          }
          const commandId = new URL(req.url).pathname.split("/").at(-1);
          return HttpResponse.json({
            command_id: commandId,
            command_type: "ConfirmPlanRevision",
            target_type: "planning_request",
            target_id: "foreign-request",
            expected_version: 4,
            status: "applied",
            result: null,
          });
        }),
      );
      const user = userEvent.setup();
      renderPage();

      const confirm = await screen.findByRole("button", { name: "确认并启用新计划" });
      await waitFor(() => expect(confirm).toBeEnabled());
      await user.click(confirm);
      expect(await screen.findByRole("group", { name: "原始命令恢复" })).toBeVisible();
      await user.click(await screen.findByRole("button", { name: "核对原始命令" }));

      await waitFor(() => expect(reads).toBe(1));
      expect(posts).toBe(1);
      expect(confirm).toBeDisabled();
      expect(screen.getByRole("group", { name: "原始命令恢复" })).toBeVisible();
      expect(screen.queryByText("新计划已启用，Worker 将按受影响分支异步收敛。")).not.toBeInTheDocument();
    },
  );
});
