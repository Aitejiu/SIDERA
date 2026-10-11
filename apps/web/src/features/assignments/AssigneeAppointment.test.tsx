import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { createElement, type PropsWithChildren } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PROFILE, PROFILE_REF } from "@/features/agents/testFixtures";
import type { LocalSession } from "@/features/local-session/api";
import type { TaskView } from "@/features/tasks/types";
import { server } from "@/test/server";
import type { AgentBindingView, CommandRequest, CommandResponse } from "./types";
import { AssigneeAppointment } from "./AssigneeAppointment";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const TASK_ID = "30000000-0000-0000-0000-000000000002";
const SESSION: LocalSession = {
  session_id: "50000000-0000-0000-0000-000000000001",
  workspace_id: WORKSPACE_ID,
  principal_id: "principal-current",
  expires_at: "2099-01-01T00:00:00Z",
};
const task: TaskView = {
  id: TASK_ID,
  workspace_id: WORKSPACE_ID,
  title: "Implementation",
  description: "Implement the dependency",
  acceptance_criteria: ["Tests pass"],
  requested_by: SESSION.principal_id,
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
  assignee: { kind: "human", id: SESSION.principal_id },
  profile_ref: null,
  version: 2,
  runnable: false,
  not_runnable_reasons: [],
  created_at: "2026-10-07T00:00:00Z",
  changed_at: "2026-10-07T00:00:00Z",
};
const BASE = `/api/v1/workspaces/${WORKSPACE_ID}`;

function renderControl(props: { targetId: string | null; onRefresh?: () => Promise<void> }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  function Provider({ children }: PropsWithChildren) {
    return createElement(QueryClientProvider, { client }, children);
  }
  return render(
    <AssigneeAppointment
      workspaceId={WORKSPACE_ID}
      targetKind="subtask"
      targetId={props.targetId}
      onRefresh={props.onRefresh ?? (async () => Promise.resolve())}
    />,
    { wrapper: Provider },
  );
}

function installReads(options: { canonicalBindingVersionAfterPost?: number } = {}) {
  let currentBinding: AgentBindingView | null = binding;
  let posts = 0;
  let posted: CommandRequest | null = null;
  const statusIds: string[] = [];
  server.use(
    http.get("/product/v1/session", () => HttpResponse.json(SESSION)),
    http.get(`${BASE}/assignments/subtask/${TASK_ID}`, () => HttpResponse.json(currentBinding)),
    http.get(`/api/v1/tasks/${TASK_ID}`, () => HttpResponse.json(task)),
    http.get(`/agents/v1/workspaces/${WORKSPACE_ID}/profiles`, () => HttpResponse.json({ items: [PROFILE] })),
    http.get(`/agents/v1/workspaces/${WORKSPACE_ID}/profiles/${PROFILE_REF.profile_id}`, ({ request }) => {
      const revision = new URL(request.url).searchParams.get("revision");
      return revision === "1" ? HttpResponse.json(PROFILE) : HttpResponse.json({}, { status: 404 });
    }),
    http.post("/api/v1/commands", async ({ request }) => {
      const command = await request.json() as CommandRequest;
      posts += 1;
      posted = command;
      expect(command.target_type).toBe("task");
      expect(command.target_id).toBe(TASK_ID);
      expect(command.expected_version).toBe(7);
      expect(Object.keys(command.payload).sort()).toEqual([
        "assignee_id", "assignee_kind", "profile_revision",
      ]);
      currentBinding = {
        ...binding,
        assignee: { kind: "profile", id: PROFILE_REF.profile_id },
        profile_ref: PROFILE_REF,
        version: options.canonicalBindingVersionAfterPost ?? 3,
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
    http.get(/\/api\/v1\/workspaces\/.*\/commands\/.*/, ({ request }) => {
      const commandId = new URL(request.url).pathname.split("/").at(-1) ?? "";
      statusIds.push(commandId);
      return HttpResponse.json({
        command_id: commandId,
        command_type: "SetSubtaskAssignee",
        target_type: "task",
        target_id: TASK_ID,
        expected_version: 7,
        status: "applied",
        result: null,
      });
    }),
  );
  return {
    getPosts: () => posts,
    getPosted: () => posted,
    getStatusIds: () => statusIds,
  };
}

afterEach(() => {
  server.resetHandlers();
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("AssigneeAppointment", () => {
  it("keeps current owner and local choice separate until an explicit save", async () => {
    installReads();
    const user = userEvent.setup();
    const refresh = vi.fn(async () => Promise.resolve());
    renderControl({ targetId: TASK_ID, onRefresh: refresh });

    const picker = await screen.findByRole("combobox", { name: "设置归属" });
    expect(screen.getByText("当前用户", { selector: "dd" })).toBeInTheDocument();
    const save = screen.getByRole("button", { name: "保存归属" });
    await waitFor(() => expect(picker).toBeEnabled());
    expect(save).toBeDisabled();
    const profileToken = JSON.stringify([
      "profile", PROFILE_REF.profile_id, PROFILE_REF.workspace_id, PROFILE_REF.revision,
    ]);
    await user.selectOptions(picker, screen.getByRole("option", { name: "Reusable reviewer · R1" }));
    await waitFor(() => expect(picker).toHaveValue(profileToken));
    await waitFor(() => expect(save).toBeEnabled());
    expect(screen.getByText("当前用户", { selector: "dd" })).toBeInTheDocument();

    await user.click(save);
    await waitFor(() => expect(screen.getByText("归属已确认")).toBeInTheDocument());
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("keeps original lookup available for an applied but unconfirmed owner", async () => {
    const reads = installReads({ canonicalBindingVersionAfterPost: 2 });
    document.cookie = "sidera_csrf=owned-csrf; Path=/";
    const user = userEvent.setup();
    renderControl({ targetId: TASK_ID });

    const picker = await screen.findByRole("combobox", { name: "设置归属" });
    const save = screen.getByRole("button", { name: "保存归属" });
    await waitFor(() => expect(picker).toBeEnabled());
    await user.selectOptions(picker, screen.getByRole("option", { name: "Reusable reviewer · R1" }));
    await waitFor(() => expect(save).toBeEnabled());
    await user.click(save);
    await waitFor(() => expect(screen.getByText("原始操作已处理，请重新读取归属事实")).toBeInTheDocument());

    const lookup = screen.getByRole("button", { name: "查询原始操作" });
    expect(lookup).toBeEnabled();
    expect(reads.getPosts()).toBe(1);
    await user.click(lookup);
    await waitFor(() => expect(reads.getStatusIds()).toEqual([reads.getPosted()?.command_id]));
    expect(reads.getPosts()).toBe(1);
  });

  it("shows unavailable state for a Subtask without its materialized Task id", () => {
    expect(QueryClientProvider).toBeTypeOf("function");
    renderControl({ targetId: null });
    expect(screen.getByRole("status")).toHaveTextContent("没有可设置归属的实体子任务");
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "保存归属" })).not.toBeInTheDocument();
  });
});
