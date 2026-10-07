import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { createElement, type PropsWithChildren } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";

import { PROFILE, PROFILE_REF } from "@/features/agents/testFixtures";
import type { LocalSession } from "@/features/local-session/api";
import { workerResultInputsFixture } from "@/test/resultInputFixtures";
import { server } from "@/test/server";
import { resultView } from "../results/testFixtures";
import {
  appointmentDraftKey,
  saveAppointmentDraft,
  type AppointmentDraft,
  type AppointmentScope,
} from "../assignments/assigneeAppointmentState";
import type {
  AgentBindingView,
  CommandRequest,
  CommandResponse,
  NativeWorkerContextView,
} from "../assignments/types";
import { buildNativeCommand } from "../assignments/commands";
import type { WorkerWorkspace } from "./types";
import { WorkerItemPage } from "./WorkerItemPage";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const WORKER_ID = "40000000-0000-0000-0000-000000000001";
const ROOT_TASK_ID = "30000000-0000-0000-0000-000000000001";
const SUBTASK_ID = "30000000-0000-0000-0000-000000000002";
const PRINCIPAL = "principal-current";
const BASE = `/api/v1/workspaces/${WORKSPACE_ID}`;
const SESSION: LocalSession = {
  session_id: "50000000-0000-0000-0000-000000000001",
  workspace_id: WORKSPACE_ID,
  principal_id: PRINCIPAL,
  expires_at: "2099-01-01T00:00:00Z",
};
const scope: AppointmentScope = {
  workspace_id: WORKSPACE_ID,
  principal_id: PRINCIPAL,
  target_kind: "worker_item",
  target_id: WORKER_ID,
};
const initialWorkspace: WorkerWorkspace = {
  workspace_id: WORKSPACE_ID,
  worker_item: {
    id: WORKER_ID,
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
  current_runtime: null,
  execution_history: [],
  current_step: null,
  next_action: null,
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

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  function Provider({ children }: PropsWithChildren) {
    return createElement(QueryClientProvider, { client }, children);
  }
  return render(
    <WorkerItemPage />,
    {
      wrapper: ({ children }) => (
        <Provider>
          <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/worker-items/${WORKER_ID}`]}>
            <Routes>
              <Route path="/workspaces/:workspaceId/worker-items/:workerItemId" element={children} />
            </Routes>
          </MemoryRouter>
        </Provider>
      ),
    },
  );
}

function installHandlers(options: { status?: string } = {}) {
  const retired = options.status === "retired";
  let currentWorkspace: WorkerWorkspace = {
    ...initialWorkspace,
    worker_item: { ...initialWorkspace.worker_item, status: options.status ?? "active" },
  };
  let currentBinding: AgentBindingView | null = null;
  let posts = 0;
  let candidateGets = 0;
  const commandGets: string[] = [];
  let posted: CommandRequest | null = null;
  const currentContext = (): NativeWorkerContextView => ({
    workspace_id: WORKSPACE_ID,
    worker_item_id: WORKER_ID,
    execution_mode: currentBinding?.profile_ref === null || currentBinding === null ? "unassigned" : "native_profile",
    worker_item_version: currentWorkspace.worker_item.version,
    binding: currentBinding,
    execution: null,
    execution_version: null,
    context: null,
    input_enabled: false,
    read_only_reason: "execution_not_started",
    run: null,
    as_of: "2026-10-07T00:00:00Z",
  });
  server.use(
    http.get("/product/v1/session", () => HttpResponse.json(SESSION)),
    http.get(`${BASE}/worker-items/${WORKER_ID}/workspace`, () => HttpResponse.json(currentWorkspace)),
    http.get(`${BASE}/assignments/worker_item/${WORKER_ID}`, () => HttpResponse.json(currentBinding)),
    http.get(`${BASE}/worker-items/${WORKER_ID}/native-context`, () => HttpResponse.json(currentContext())),
    http.get(`/api/v1/tasks/${ROOT_TASK_ID}/graph`, () => HttpResponse.json({
      overview: { title: "Payment refactor" },
      nodes: [{ subtask_key: "callback-idempotency", title: "Callback idempotency" }],
    })),
    http.get(`${BASE}/tasks/${ROOT_TASK_ID}/results`, () => HttpResponse.json(resultView({
      workspace_id: WORKSPACE_ID,
      root_task_id: ROOT_TASK_ID,
      task_title: "Payment refactor",
    }))),
    http.get(`${BASE}/worker-items/${WORKER_ID}/activity`, () => HttpResponse.json({
      items: [],
      last_change_cursor: 0,
      next_cursor: null,
    })),
    http.get(`${BASE}/worker-items/${WORKER_ID}/result-inputs`, () => HttpResponse.json(workerResultInputsFixture())),
    http.get(`${BASE}/worker-items/${WORKER_ID}/handoff-candidates`, () => {
      candidateGets += 1;
      return HttpResponse.json({ candidates: [], as_of: "2026-10-07T00:00:00Z" });
    }),
    http.get(`/agents/v1/workspaces/${WORKSPACE_ID}/profiles`, () => HttpResponse.json({ items: [PROFILE] })),
    http.get(`/agents/v1/workspaces/${WORKSPACE_ID}/profiles/${PROFILE_REF.profile_id}`, ({ request }) => {
      const revision = new URL(request.url).searchParams.get("revision");
      return revision === String(PROFILE_REF.revision) ? HttpResponse.json(PROFILE) : HttpResponse.json({}, { status: 404 });
    }),
    http.get(/\/api\/v1\/workspaces\/[^/]+\/commands\/[^/]+$/, ({ request }) => {
      const commandId = new URL(request.url).pathname.split("/").at(-1) ?? "";
      commandGets.push(commandId);
      return HttpResponse.json({
        command_id: commandId,
        command_type: "SetWorkerItemAssignee",
        target_type: "worker_item",
        target_id: WORKER_ID,
        expected_version: 7,
        status: "pending",
        result: null,
      });
    }),
    http.post("/api/v1/commands", async ({ request }) => {
      posts += 1;
      const command = await request.json() as CommandRequest;
      posted = command;
      currentBinding = {
        workspace_id: WORKSPACE_ID,
        target_type: "worker_item",
        target_id: WORKER_ID,
        assignee: { kind: "profile", id: PROFILE_REF.profile_id },
        profile_ref: PROFILE_REF,
        version: 3,
        runnable: true,
        not_runnable_reasons: [],
        created_at: "2026-10-07T00:00:00Z",
        changed_at: "2026-10-07T01:00:00Z",
      };
      currentWorkspace = {
        ...currentWorkspace,
        worker_item: { ...currentWorkspace.worker_item, version: 8 },
      };
      return HttpResponse.json({
        command_id: command.command_id,
        aggregate_id: WORKER_ID,
        aggregate_version: 8,
        event_ids: ["60000000-0000-0000-0000-000000000001"],
        data: { binding: currentBinding },
      } satisfies CommandResponse);
    }),
  );
  return {
    isRetired: retired,
    getPosts: () => posts,
    getCandidateGets: () => candidateGets,
    getCommandGets: () => commandGets,
    getPosted: () => posted,
  };
}

afterEach(() => {
  server.resetHandlers();
  localStorage.clear();
  document.cookie = "sidera_csrf=; Max-Age=0; Path=/";
});

describe("NativeWorkerAppointment", () => {
  it("keeps the saved owner separate from execution pins and posts WorkerItem version 7 once", async () => {
    const handlers = installHandlers();
    document.cookie = "sidera_csrf=owned-csrf; Path=/";
    const user = userEvent.setup();
    renderPage();

    const picker = await screen.findByRole("combobox", { name: "设置归属" });
    const save = screen.getByRole("button", { name: "保存归属" });
    await waitFor(() => expect(picker).toBeEnabled());
    expect(screen.getByText("未分配", { selector: "dd" })).toBeInTheDocument();
    expect(screen.getByText("无已保存执行", { selector: "dd" })).toBeInTheDocument();
    expect(screen.getByText("无已保存 Run", { selector: "dd" })).toBeInTheDocument();
    expect(save).toBeDisabled();
    expect(screen.queryByText("执行配置")).not.toBeInTheDocument();
    expect(handlers.getCandidateGets()).toBe(0);

    await user.selectOptions(picker, screen.getByRole("option", { name: "Reusable reviewer · R1" }));
    await waitFor(() => expect(save).toBeEnabled());
    expect(screen.getByText("未分配", { selector: "dd" })).toBeInTheDocument();
    await user.click(save);

    await waitFor(() => expect(screen.getByText("归属已确认")).toBeInTheDocument());
    expect(handlers.getPosts()).toBe(1);
    expect(handlers.getPosted()).toMatchObject({
      command_type: "SetWorkerItemAssignee",
      target_type: "worker_item",
      target_id: WORKER_ID,
      expected_version: 7,
      payload: {
        assignee_kind: "profile",
        assignee_id: PROFILE_REF.profile_id,
        profile_revision: PROFILE_REF.revision,
      },
    });
    expect(handlers.getCandidateGets()).toBe(0);
  });

  it("keeps a retired item's original lookup available without enabling edits or POST", async () => {
    const handlers = installHandlers({ status: "retired" });
    const original = buildNativeCommand({
      session: SESSION,
      workspaceId: WORKSPACE_ID,
      commandType: "SetWorkerItemAssignee",
      targetType: "worker_item",
      targetId: WORKER_ID,
      expectedVersion: 7,
      payload: {
        assignee_kind: "profile",
        assignee_id: PROFILE_REF.profile_id,
        profile_revision: PROFILE_REF.revision,
      },
    });
    const draft: AppointmentDraft = {
      schema_version: 1,
      scope,
      review: { aggregateVersion: 7, bindingFingerprint: "retired-binding", executionFingerprint: "" },
      choice: { kind: "profile", id: PROFILE_REF.profile_id, profileRef: PROFILE_REF },
      original,
      status: "unknown",
      rejectionCode: null,
    };
    expect(saveAppointmentDraft(scope, draft)).toEqual({ ok: true });
    const user = userEvent.setup();
    renderPage();

    const picker = await screen.findByRole("combobox", { name: "设置归属" });
    expect(await screen.findByRole("button", { name: "查询原始操作" })).toBeEnabled();
    expect(picker).toBeDisabled();
    expect(screen.getByRole("button", { name: "保存归属" })).toBeDisabled();
    expect(handlers.isRetired).toBe(true);
    expect(handlers.getCandidateGets()).toBe(0);

    await user.click(screen.getByRole("button", { name: "查询原始操作" }));
    await waitFor(() => expect(handlers.getCommandGets()).toEqual([original.command_id]));
    expect(handlers.getPosts()).toBe(0);
    expect(screen.getByRole("button", { name: "查询原始操作" })).toBeEnabled();
    expect(localStorage.getItem(appointmentDraftKey(scope))).toContain(original.command_id);
  });
});
