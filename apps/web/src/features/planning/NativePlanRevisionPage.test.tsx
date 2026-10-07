import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { profileAt } from "@/features/agents/testFixtures";
import type { AgentBindingView } from "@/features/assignments/types";
import { server } from "@/test/server";
import { PlanRevisionPage } from "./PlanRevisionPage";

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
  return render(<PlanRevisionPage />, { wrapper });
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
