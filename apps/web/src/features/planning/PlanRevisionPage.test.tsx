import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, RouterProvider, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { server } from "@/test/server";
import { createTestRouter } from "@/app/router";

import { PlanRevisionPage } from "./PlanRevisionPage";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const ROOT_TASK_ID = "30000000-0000-0000-0000-000000000001";
const REQUEST_ID = "40000000-0000-0000-0000-000000000001";
const SNAPSHOT_ID = "50000000-0000-0000-0000-000000000001";
const PROFILE_ID = "60000000-0000-0000-0000-000000000001";
const TRANSITION_ID = "70000000-0000-0000-0000-000000000001";

const base = {
  root_task_id: ROOT_TASK_ID,
  plan_snapshot_id: SNAPSHOT_ID,
  plan_generation: 2,
  plan_content_hash: "a".repeat(64),
  root_task_version: 7,
};

const revision = {
  id: REQUEST_ID,
  workspace_id: WORKSPACE_ID,
  kind: "revision",
  status: "proposal_ready",
  current_brief_revision: 2,
  current_proposal_revision: 1,
  current_proposal_content_hash: "b".repeat(64),
  planning_profile_id: PROFILE_ID,
  planning_profile_version: 2,
  profile_key: "default-opencode",
  current_run_id: null,
  requires_attention: true,
  version: 4,
  created_at: "2026-08-29T08:00:00Z",
  updated_at: "2026-08-29T08:05:00Z",
  source_type: "plan_revision",
  source_ref: "revision-command",
  last_change_cursor: 31,
  active_run: null,
  pending_clarifications: [],
  profile: null,
  usage: {
    input_tokens: 120,
    output_tokens: 80,
    runtime_seconds: 12,
    context_bytes: 4000,
    cost_microunits: 25,
  },
  revision_base: base,
  revision_freshness: {
    base,
    base_fresh: true,
    checked_at: "2026-08-29T08:05:00Z",
    current_plan_content_hash: "a".repeat(64),
    current_plan_generation: 2,
    current_plan_snapshot_id: SNAPSHOT_ID,
    current_root_task_version: 7,
    stale_reasons: [],
  },
};

const diff = {
  workspace_id: WORKSPACE_ID,
  planning_request_id: REQUEST_ID,
  proposal_revision_id: "80000000-0000-0000-0000-000000000001",
  root_task_id: ROOT_TASK_ID,
  proposal_revision: 1,
  proposal_content_hash: "b".repeat(64),
  diff_hash: "c".repeat(64),
  base,
  freshness: revision.revision_freshness,
  target_descriptor_preview: {
    parent_snapshot_id: SNAPSHOT_ID,
    target_generation: 3,
    target_plan_content_hash: "d".repeat(64),
  },
  impact_counts: {
    preserve: 1,
    move: 0,
    patch: 1,
    continue: 0,
    create: 1,
    derive: 0,
    retire: 0,
    affected_branches: 1,
  },
  risk_flags: ["worker_context_patch"],
  affected_branch_keys: ["api"],
  affected_branch_paths: ["支付重构 / API"],
  root_changes: [],
  subtask_changes: [{
    target_subtask_key: "api",
    path: "支付重构 / API",
    strategy: "preserve",
    reason: "复用已有子任务",
    existing_task_id: "90000000-0000-0000-0000-000000000001",
    field_changes: [],
  }],
  worker_item_changes: [{
    target_worker_item_key: "backend",
    path: "支付重构 / API / Backend",
    strategy: "patch",
    context_disposition: "patch",
    from_subtask_key: "api",
    to_subtask_key: "api",
    existing_worker_item_id: "a0000000-0000-0000-0000-000000000001",
    reasons: ["任务目标变化"],
    field_changes: [],
  }],
  subtask_dependency_changes: [],
  worker_dependency_changes: [],
  transition_preview: [{
    target_worker_item_key: "backend",
    worker_item_id: "a0000000-0000-0000-0000-000000000001",
    path: "支付重构 / API / Backend",
    allowed_actions: ["patch", "continue"],
    fallback_action: "continue",
    reason_codes: ["context_changed"],
  }],
  accepted_transition_policy: {
    activate_immediately: true,
    budget_level: "standard",
    failure_policy: "block_affected_branch",
    manual_gate: false,
    revision_operation: "patch_existing",
  },
};

function installHandlers(request: Record<string, unknown> = revision) {
  server.use(
    http.get(
      `/api/v1/workspaces/${WORKSPACE_ID}/planning-requests/${REQUEST_ID}`,
      () => HttpResponse.json(request),
    ),
    http.get(
      `/api/v1/workspaces/${WORKSPACE_ID}/planning-requests/${REQUEST_ID}/diff`,
      () => HttpResponse.json(diff),
    ),
    http.get(
      `/api/v1/tasks/${ROOT_TASK_ID}`,
      () => HttpResponse.json({
        id: ROOT_TASK_ID,
        workspace_id: WORKSPACE_ID,
        title: "支付重构",
        description: "把支付回调改造成可恢复、可观测的执行流程。",
        acceptance_criteria: [],
        requested_by: "developer",
        priority: 1,
        status: "running",
        version: 7,
        created_at: "2026-08-29T08:00:00Z",
        updated_at: "2026-08-29T08:05:00Z",
      }),
    ),
  );
}

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter
        initialEntries={[`/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}/revisions/${REQUEST_ID}`]}
      >
        <Routes>
          <Route
            path="/workspaces/:workspaceId/tasks/:rootTaskId/revisions/:planningRequestId"
            element={<PlanRevisionPage />}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("PlanRevisionPage", () => {
  beforeEach(() => {
    server.resetHandlers();
  });

  it("renders the root-task revision route and sends the exact whole-plan confirmation", async () => {
    installHandlers();
    let commandBody: Record<string, unknown> | null = null;
    server.use(
      http.post("/api/v1/commands", async ({ request }) => {
        commandBody = await request.json() as Record<string, unknown>;
        return HttpResponse.json({
          command_id: "b0000000-0000-0000-0000-000000000001",
          aggregate_id: REQUEST_ID,
          aggregate_version: 5,
          event_ids: [],
          data: { plan_transition_id: TRANSITION_ID },
        }, { status: 201 });
      }),
    );
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByRole("heading", { name: "调整任务计划" })).toBeVisible();
    expect(screen.getByText("支付重构")).toBeVisible();
    expect((await screen.findAllByText(/Generation 2 → 3/)).length).toBeGreaterThan(0);
    expect((await screen.findAllByText("支付重构 / API / Backend")).length).toBeGreaterThan(0);

    await user.click(screen.getByRole("button", { name: "确认并启用新计划" }));

    await waitFor(() => expect(commandBody).not.toBeNull());
    expect(commandBody).toMatchObject({
      command_type: "ConfirmPlanRevision",
      target_type: "planning_request",
      target_id: REQUEST_ID,
      expected_version: 4,
      payload: {
        expected_proposal_revision: 1,
        expected_proposal_content_hash: "b".repeat(64),
        expected_diff_hash: "c".repeat(64),
        expected_base_snapshot_id: SNAPSHOT_ID,
        expected_base_content_hash: "a".repeat(64),
        profile_id: PROFILE_ID,
        profile_version: 2,
      },
    });
  });

  it("blocks confirmation and explains the stale base", async () => {
    installHandlers({
      ...revision,
      revision_freshness: {
        ...revision.revision_freshness,
        base_fresh: false,
        stale_reasons: ["active plan advanced"] as string[],
      },
    });
    renderPage();

    expect(await screen.findByText("当前基准已过期")).toBeVisible();
    expect(screen.getByRole("button", { name: "确认并启用新计划" })).toBeDisabled();
    expect(screen.getByText("active plan advanced")).toBeVisible();
  });

  it("shows a bounded not-found state", async () => {
    server.use(
      http.get(
        `/api/v1/workspaces/${WORKSPACE_ID}/planning-requests/${REQUEST_ID}`,
        () => HttpResponse.json({ error: { code: "not_found", message: "missing" } }, { status: 404 }),
      ),
    );
    renderPage();

    expect(await screen.findByRole("heading", { name: "无法读取计划调整" })).toBeVisible();
    expect(screen.getByRole("button", { name: "重试读取" })).toBeVisible();
  });

  it("keeps an enabled revision readable as a terminal audit state", async () => {
    installHandlers({
      ...revision,
      status: "materialized",
      current_proposal_revision: null,
      current_proposal_content_hash: null,
    });
    renderPage();

    expect(await screen.findByText("新计划已启用")).toBeVisible();
    expect(screen.getByRole("button", { name: "确认并启用新计划" })).toBeDisabled();
  });

  it("renders through the application router at the nested revision path", async () => {
    installHandlers();
    const router = createTestRouter([
      `/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}/revisions/${REQUEST_ID}`,
    ]);
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });

    render(
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    );

    expect(await screen.findByRole("heading", { name: "调整任务计划" })).toBeVisible();
    expect(screen.getByRole("link", { name: /任务 30000000/ })).toBeVisible();

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "关闭任务 30000000" }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe(`/workspaces/${WORKSPACE_ID}/tasks`);
    });
  });
});
