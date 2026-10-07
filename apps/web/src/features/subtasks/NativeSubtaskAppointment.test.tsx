import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { createElement, type PropsWithChildren } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";

import { PROFILE, PROFILE_REF } from "@/features/agents/testFixtures";
import type { LocalSession } from "@/features/local-session/api";
import type { TaskView } from "@/features/tasks/types";
import { server } from "@/test/server";
import type { SubtaskWorkspace } from "./types";
import { SubtaskPage } from "./SubtaskPage";
import type { AgentBindingView, CommandRequest, CommandResponse } from "../assignments/types";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const ROOT_TASK_ID = "30000000-0000-0000-0000-000000000001";
const TASK_ID = "30000000-0000-0000-0000-000000000002";
const SUBTASK_KEY = "native-appointment";
const PRINCIPAL = "principal-current";
const BASE = `/api/v1/workspaces/${WORKSPACE_ID}`;
const SESSION: LocalSession = {
  session_id: "50000000-0000-0000-0000-000000000001",
  workspace_id: WORKSPACE_ID,
  principal_id: PRINCIPAL,
  expires_at: "2099-01-01T00:00:00Z",
};
const workspace = {
  workspace_id: WORKSPACE_ID,
  root_task_id: ROOT_TASK_ID,
  task_title: "Payment refactor",
  plan_snapshot_id: "10000000-0000-0000-0000-000000000001",
  plan_generation: 2,
  subtask: {
    key: SUBTASK_KEY,
    kind: "child",
    task_id: TASK_ID,
    title: "Callback idempotency",
    goal_summary: "Keep repeated notifications idempotent.",
    milestone_key: null,
    state: "ready",
    acceptance_refs: [],
    evidence_satisfied: 0,
    evidence_required: 0,
    current_action: null,
  },
  collaboration: null,
  upstream_subtasks: [],
  downstream_subtasks: [],
  worker_items: [],
  worker_graph: { worker_item_ids: [], edges: [] },
  artifacts: [],
  evidence: [],
  checkpoints: [],
  attention_items: [],
  recent_activity: [],
  last_change_cursor: 1,
  as_of: "2026-10-07T00:00:00Z",
} satisfies SubtaskWorkspace;
const task: TaskView = {
  id: TASK_ID,
  workspace_id: WORKSPACE_ID,
  title: "Callback idempotency",
  description: "Implement the idempotency contract.",
  acceptance_criteria: [],
  requested_by: PRINCIPAL,
  priority: 1,
  status: "running",
  version: 7,
  created_at: "2026-10-07T00:00:00Z",
  updated_at: "2026-10-07T00:00:00Z",
};
const binding: AgentBindingView = {
  workspace_id: WORKSPACE_ID,
  target_type: "subtask",
  target_id: TASK_ID,
  assignee: { kind: "human", id: PRINCIPAL },
  profile_ref: null,
  version: 2,
  runnable: false,
  not_runnable_reasons: [],
  created_at: "2026-10-07T00:00:00Z",
  changed_at: "2026-10-07T00:00:00Z",
};

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  function Provider({ children }: PropsWithChildren) {
    return createElement(QueryClientProvider, { client }, children);
  }
  return render(
    <SubtaskPage />,
    {
      wrapper: ({ children }) => (
        <Provider>
          <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}/subtasks/${SUBTASK_KEY}`]}>
            <Routes>
              <Route path="/workspaces/:workspaceId/tasks/:rootTaskId/subtasks/:subtaskKey" element={children} />
            </Routes>
          </MemoryRouter>
        </Provider>
      ),
    },
  );
}

function installHandlers(initialBinding: AgentBindingView | null = binding) {
  let currentBinding: AgentBindingView | null = initialBinding;
  let taskVersion = 7;
  let posts = 0;
  let posted: CommandRequest | null = null;
  server.use(
    http.get(`/api/v1/tasks/${ROOT_TASK_ID}/subtasks/${SUBTASK_KEY}`, () => HttpResponse.json(workspace)),
    http.get("/product/v1/session", () => HttpResponse.json(SESSION)),
    http.get(`${BASE}/assignments/subtask/${TASK_ID}`, () => HttpResponse.json(currentBinding)),
    http.get(`/api/v1/tasks/${TASK_ID}`, () => HttpResponse.json({ ...task, version: taskVersion })),
    http.get(`/agents/v1/workspaces/${WORKSPACE_ID}/profiles`, () => HttpResponse.json({ items: [PROFILE] })),
    http.get(`/agents/v1/workspaces/${WORKSPACE_ID}/profiles/${PROFILE_REF.profile_id}`, ({ request }) => {
      const revision = new URL(request.url).searchParams.get("revision");
      return revision === String(PROFILE_REF.revision) ? HttpResponse.json(PROFILE) : HttpResponse.json({}, { status: 404 });
    }),
    http.post("/api/v1/commands", async ({ request }) => {
      posts += 1;
      const command = await request.json() as CommandRequest;
      posted = command;
      currentBinding = {
        ...binding,
        assignee: { kind: "profile", id: PROFILE_REF.profile_id },
        profile_ref: PROFILE_REF,
        version: 3,
        changed_at: "2026-10-07T01:00:00Z",
      };
      return HttpResponse.json({
        command_id: command.command_id,
        aggregate_id: TASK_ID,
        aggregate_version: 8,
        event_ids: ["60000000-0000-0000-0000-000000000001"],
        data: { binding: currentBinding },
      } satisfies CommandResponse);
    }),
  );
  return {
    setTaskVersion: (version: number) => { taskVersion = version; },
    getPosts: () => posts,
    getPosted: () => posted,
  };
}

async function openDetails(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "查看子任务详情" }));
}

afterEach(() => {
  server.resetHandlers();
  localStorage.clear();
  document.cookie = "sidera_csrf=; Max-Age=0; Path=/";
});

describe("NativeSubtaskAppointment", () => {
  it("lets an unassigned Task choose a permitted Profile before one explicit save", async () => {
    const handlers = installHandlers(null);
    document.cookie = "sidera_csrf=owned-csrf; Path=/";
    const user = userEvent.setup();
    renderPage();
    await openDetails(user);

    const picker = await screen.findByRole("combobox", { name: "设置归属" });
    const save = screen.getByRole("button", { name: "保存归属" });
    await waitFor(() => expect(picker).toBeEnabled());
    expect(screen.getByText("未分配", { selector: "dd" })).toBeInTheDocument();
    expect(picker).toHaveValue("");
    expect(save).toBeDisabled();

    await user.selectOptions(picker, screen.getByRole("option", { name: "Reusable reviewer · R1" }));
    await waitFor(() => expect(save).toBeEnabled());
    expect(screen.getByText("未分配", { selector: "dd" })).toBeInTheDocument();
    expect(handlers.getPosts()).toBe(0);

    await user.click(save);
    await waitFor(() => expect(screen.getByText("归属已确认")).toBeInTheDocument());
    expect(handlers.getPosts()).toBe(1);
  });

  it("edits the local choice for the current owner and posts once against Task version 7", async () => {
    const handlers = installHandlers();
    document.cookie = "sidera_csrf=owned-csrf; Path=/";
    const user = userEvent.setup();
    renderPage();
    await openDetails(user);

    const picker = await screen.findByRole("combobox", { name: "设置归属" });
    const save = screen.getByRole("button", { name: "保存归属" });
    await waitFor(() => expect(picker).toBeEnabled());
    expect(screen.getByText("当前用户", { selector: "dd" })).toBeInTheDocument();
    expect(save).toBeDisabled();

    await user.selectOptions(picker, screen.getByRole("option", { name: "Reusable reviewer · R1" }));
    await waitFor(() => expect(save).toBeEnabled());
    expect(screen.getByText("当前用户", { selector: "dd" })).toBeInTheDocument();
    expect(handlers.getPosts()).toBe(0);

    await user.click(save);
    await waitFor(() => expect(screen.getByText("归属已确认")).toBeInTheDocument());
    expect(handlers.getPosts()).toBe(1);
    expect(handlers.getPosted()).toMatchObject({
      command_type: "SetSubtaskAssignee",
      actor: { type: "user", id: PRINCIPAL },
      target_type: "task",
      target_id: TASK_ID,
      expected_version: 7,
      payload: {
        assignee_kind: "profile",
        assignee_id: PROFILE_REF.profile_id,
        profile_revision: PROFILE_REF.revision,
      },
    });
    expect(Object.keys(handlers.getPosted()?.payload ?? {}).sort()).toEqual([
      "assignee_id", "assignee_kind", "profile_revision",
    ]);
  });

  it("keeps a reloaded stale anchor locked until explicit Revisit, preserving a cleared choice", async () => {
    const handlers = installHandlers();
    const user = userEvent.setup();
    const first = renderPage();
    await openDetails(user);
    const firstPicker = await screen.findByRole("combobox", { name: "设置归属" });
    await waitFor(() => expect(firstPicker).toBeEnabled());
    await user.selectOptions(firstPicker, screen.getByRole("option", { name: "Reusable reviewer · R1" }));
    await waitFor(() => expect(firstPicker).toHaveValue(JSON.stringify([
      "profile", PROFILE_REF.profile_id, PROFILE_REF.workspace_id, PROFILE_REF.revision,
    ])));
    first.unmount();

    handlers.setTaskVersion(8);
    renderPage();
    await openDetails(user);
    const picker = await screen.findByRole("combobox", { name: "设置归属" });
    const save = screen.getByRole("button", { name: "保存归属" });
    const revisit = await screen.findByRole("button", { name: "重新读取并复核" });
    await waitFor(() => expect(picker).toBeEnabled());
    expect(save).toBeDisabled();
    expect(revisit).toBeEnabled();

    await user.selectOptions(picker, "");
    expect(picker).toHaveValue("");
    expect(save).toBeDisabled();
    expect(revisit).toBeInTheDocument();
    await user.click(revisit);

    await waitFor(() => expect(screen.queryByRole("button", { name: "重新读取并复核" })).not.toBeInTheDocument());
    expect(picker).toHaveValue("");
    expect(save).toBeDisabled();
    expect(handlers.getPosts()).toBe(0);
  });
});
