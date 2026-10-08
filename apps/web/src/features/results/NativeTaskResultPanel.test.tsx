import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CommandRequest } from "@/features/assignments/types";
import * as client from "@/shared/api/client";
import { server } from "@/test/server";

import * as resultApi from "./api";
import { taskResultsQueryKey, type TaskResultOverview } from "./api";
import { loadNativeAcceptanceDraft, nativeAcceptanceDraftKey } from "./nativeResultAcceptance";
import { TaskResultPanel } from "./TaskResultPanel";
import { RESULT_CANDIDATE_ID, RESULT_ROOT_TASK_ID, RESULT_WORKSPACE_ID, resultAction, resultView } from "./testFixtures";

const rootB = "71000000-0000-0000-0000-000000000099";
const oldId = "71000000-0000-0000-0000-000000000014";
const base = `/api/v1/workspaces/${RESULT_WORKSPACE_ID}/tasks/${RESULT_ROOT_TASK_ID}/results`;
const scope = { workspace_id: RESULT_WORKSPACE_ID, root_task_id: RESULT_ROOT_TASK_ID, principal_id: "human-current" };
const session = { workspace_id: RESULT_WORKSPACE_ID, principal_id: "human-current", session_id: "actual-human-session", expires_at: "2099-01-01T00:00:00Z" };

function deferred() {
  let release!: () => void;
  const promise = new Promise<void>(resolve => { release = resolve; });
  return { promise, release };
}

function receipt(command: CommandRequest) {
  return {
    command_id: command.command_id, aggregate_id: RESULT_ROOT_TASK_ID, aggregate_version: 11, event_ids: [],
    data: {
      status: "accepted", acceptance_id: "71000000-0000-0000-0000-000000000040", candidate_id: RESULT_CANDIDATE_ID,
      result_version: 7, task_status: "waiting", apply_operation_id: null, apply_status: null,
      finalization_operation_id: "71000000-0000-0000-0000-000000000041", result_stage: "finalizing",
    },
  };
}

function acceptedView(command: CommandRequest): TaskResultOverview {
  return resultView({
    task_version: 11, task_status: "waiting", result_version: 8, result_stage: "finalizing", available_actions: [],
    acceptance: {
      acceptance_id: "71000000-0000-0000-0000-000000000040", command_id: command.command_id,
      actor_type: "user", actor_id: "human-current", candidate_id: RESULT_CANDIDATE_ID, candidate_hash: "c".repeat(64),
      plan_content_hash: "d".repeat(64), acceptance_basis_hash: "f".repeat(64), result_version: 7,
      criteria: [{ criterion_key: "expired-credentials", decision: "accept_passed", reason: null }], created_at: "2026-10-07T08:00:00Z",
    },
  });
}

function routes() {
  const observed = { commands: [] as CommandRequest[], basis: [] as unknown[], csrf: [] as (string | null)[], lookups: [] as string[], view: resultView() };
  server.use(
    http.get("/main-agent/v1/health", () => HttpResponse.json({ status: "ok", service: "sidera-product" })),
    http.get("/product/v1/session", () => HttpResponse.json(session)),
    http.get(base, () => HttpResponse.json(observed.view)),
    http.get(`/api/v1/workspaces/${RESULT_WORKSPACE_ID}/tasks/${rootB}/results`, () => HttpResponse.json(resultView({ root_task_id: rootB, available_actions: [{ ...resultAction(), target_id: rootB }] }))),
    http.post(`${base}/${RESULT_CANDIDATE_ID}/acceptance-basis`, async ({ request }) => {
      observed.basis.push(await request.json());
      return HttpResponse.json({ candidate_id: RESULT_CANDIDATE_ID, candidate_hash: "c".repeat(64), expected_result_version: 7, acceptance_basis_hash: "f".repeat(64) });
    }),
    http.post("/api/v1/commands", async ({ request }) => {
      const command = await request.json() as CommandRequest;
      observed.commands.push(command);
      observed.csrf.push(request.headers.get("X-Sidera-CSRF"));
      observed.view = acceptedView(command);
      return HttpResponse.json(receipt(command));
    }),
    http.get(`/api/v1/workspaces/${RESULT_WORKSPACE_ID}/commands/:commandId`, ({ params }) => {
      observed.lookups.push(String(params.commandId));
      const command = observed.commands[0]!;
      return HttpResponse.json({ command_id: command.command_id, command_type: "CompleteTask", target_type: "task", target_id: RESULT_ROOT_TASK_ID, expected_version: 11, payload: command.payload, status: "applied", result: receipt(command) });
    }),
  );
  return observed;
}

function panel(candidate?: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const initialEntry = `/workspaces/${RESULT_WORKSPACE_ID}/tasks/${RESULT_ROOT_TASK_ID}?view=results${candidate ? `&candidate=${candidate}` : ""}`;
  const tree = (rootTaskId: string) => <QueryClientProvider client={queryClient}><MemoryRouter initialEntries={[initialEntry]}><TaskResultPanel workspaceId={RESULT_WORKSPACE_ID} rootTaskId={rootTaskId} /></MemoryRouter></QueryClientProvider>;
  const rendered = render(tree(RESULT_ROOT_TASK_ID));
  return { ...rendered, queryClient, rerenderRoot: (rootTaskId: string) => rendered.rerender(tree(rootTaskId)) };
}

async function acceptFromForm() {
  const user = userEvent.setup();
  await user.selectOptions(await screen.findByLabelText("Expired credentials are rejected 的决定"), "accept_passed");
  await waitFor(() => expect(screen.getByRole("button", { name: "接受成果" })).toBeEnabled());
  await user.click(screen.getByRole("button", { name: "接受成果" }));
}

beforeEach(() => { localStorage.clear(); document.cookie = "sidera_csrf=native-panel-csrf; path=/"; });
afterEach(() => { vi.restoreAllMocks(); document.cookie = "sidera_csrf=; Max-Age=0; path=/"; });

describe("TaskResultPanel native root acceptance", () => {
  it("accepts the unchanged waiting Task version 11 using the actual current user and CSRF, without claiming completion", async () => {
    const observed = routes();
    const rendered = panel();
    await acceptFromForm();
    expect(await screen.findByText("验收已记录，等待成果收尾")).toBeVisible();
    expect(await screen.findByText("收尾中")).toBeVisible();
    expect(observed.commands).toHaveLength(1);
    expect(observed.commands[0]).toMatchObject({ actor: { type: "user", id: "human-current" }, target_type: "task", target_id: RESULT_ROOT_TASK_ID, expected_version: 11, payload: { candidate_id: RESULT_CANDIDATE_ID, candidate_hash: "c".repeat(64), expected_result_version: 7, criteria: [{ criterion_key: "expired-credentials", decision: "accept_passed", reason: null }] } });
    expect(observed.basis).toEqual([{ criteria: [{ criterion_key: "expired-credentials", decision: "accept_passed", reason: null }] }]);
    expect(observed.csrf).toEqual(["native-panel-csrf"]);
    expect(screen.queryByText("任务已完成")).not.toBeInTheDocument();
    expect(screen.queryByText("已完成")).not.toBeInTheDocument();
    expect(rendered.queryClient.getQueryData<TaskResultOverview>(taskResultsQueryKey(RESULT_WORKSPACE_ID, RESULT_ROOT_TASK_ID))).toMatchObject({ task_status: "waiting", task_version: 11, result_version: 8 });
    rendered.unmount();
  });

  it("reopens the original after application, action removal, and response loss with one basis/POST/GET", async () => {
    const observed = routes();
    const held = deferred();
    const sent = vi.spyOn(resultApi, "buildNativeTaskAcceptanceCommand");
    server.use(http.post("/api/v1/commands", async ({ request }) => {
      const command = await request.json() as CommandRequest;
      observed.commands.push(command);
      observed.csrf.push(request.headers.get("X-Sidera-CSRF"));
      observed.view = acceptedView(command);
      await held.promise;
      return HttpResponse.error();
    }));
    const first = panel();
    await acceptFromForm();
    await waitFor(() => expect(observed.commands).toHaveLength(1));
    await act(() => first.queryClient.invalidateQueries({ queryKey: taskResultsQueryKey(RESULT_WORKSPACE_ID, RESULT_ROOT_TASK_ID) }));
    expect(await screen.findByText("收尾中")).toBeVisible();
    expect(screen.queryByRole("button", { name: "接受成果" })).not.toBeInTheDocument();
    const original = observed.commands[0]!;
    expect(loadNativeAcceptanceDraft(scope)).toMatchObject({ ok: true, draft: { original, status: "pending" } });
    first.unmount();
    const before = localStorage.getItem(nativeAcceptanceDraftKey(scope));
    const set = vi.spyOn(Storage.prototype, "setItem");
    await act(async () => { held.release(); });
    const second = panel();
    const lookup = await screen.findByRole("button", { name: "查询原始验收" });
    await waitFor(() => expect(lookup).toBeEnabled());
    expect(set).not.toHaveBeenCalled();
    expect(localStorage.getItem(nativeAcceptanceDraftKey(scope))).toBe(before);
    await userEvent.setup().click(lookup);
    expect(await screen.findByText("验收已记录，等待成果收尾")).toBeVisible();
    expect(observed.lookups).toEqual([original.command_id]);
    expect(observed.basis).toHaveLength(1);
    expect(observed.commands).toHaveLength(1);
    expect(sent).toHaveBeenCalledTimes(1);
    expect(observed.csrf).toEqual(["native-panel-csrf"]);
    expect(screen.queryByText("任务已完成")).not.toBeInTheDocument();
    second.unmount();
  });

  it.each(["historical", "read-failure", "loading"])("keeps original GET recovery available during %s", async mode => {
    const observed = routes();
    server.use(http.post("/api/v1/commands", async ({ request }) => { observed.commands.push(await request.json() as CommandRequest); return HttpResponse.error(); }));
    const first = panel();
    await acceptFromForm();
    await screen.findByText("验收结果尚未确认，请查询原始验收");
    first.unmount();
    const held = deferred();
    if (mode === "historical") {
      const view = resultView();
      observed.view = resultView({ candidate_history: { items: [view.current_candidate!, { ...view.current_candidate!, candidate_id: oldId, candidate_hash: "1".repeat(64), is_current: false }], next_cursor: null } });
    } else if (mode === "read-failure") server.use(http.get(base, () => HttpResponse.json({ error: { code: "unavailable", message: "private result read" } }, { status: 503 })));
    else server.use(http.get(base, async () => { await held.promise; return HttpResponse.json(acceptedView(observed.commands[0]!)); }));
    const identity = vi.spyOn(client, "createCommandIdentity");
    const second = panel(mode === "historical" ? oldId : undefined);
    const lookup = await screen.findByRole("button", { name: "查询原始验收" });
    if (mode === "historical") expect(await screen.findByText("历史候选，只读")).toBeVisible();
    if (mode === "read-failure") expect(await screen.findByText("无法读取成果")).toBeVisible();
    if (mode === "loading") expect(screen.getByText("正在读取成果")).toBeVisible();
    await waitFor(() => expect(lookup).toBeEnabled());
    await userEvent.setup().click(lookup);
    await waitFor(() => expect(observed.lookups).toEqual([observed.commands[0]!.command_id]));
    held.release();
    expect(await screen.findByText("验收已记录，等待成果收尾")).toBeVisible();
    expect(observed.commands).toHaveLength(1);
    expect(observed.basis).toHaveLength(1);
    expect(identity).not.toHaveBeenCalled();
    expect(screen.queryByText("private result read")).not.toBeInTheDocument();
    second.unmount();
  });

  it("retains the selected criterion after a safe pre-command basis error", async () => {
    const observed = routes();
    server.use(http.post(`${base}/${RESULT_CANDIDATE_ID}/acceptance-basis`, () => HttpResponse.json({ error: { code: "acceptance_stale", message: "private basis detail" } }, { status: 409 })));
    panel();
    await acceptFromForm();
    expect(await screen.findByText("成果已变化，请重新审阅")).toBeVisible();
    expect(screen.getByLabelText("Expired credentials are rejected 的决定")).toHaveValue("accept_passed");
    expect(screen.getByRole("button", { name: "接受成果" })).toBeEnabled();
    expect(observed.commands).toHaveLength(0);
    expect(localStorage.length).toBe(0);
    expect(screen.queryByText("private basis detail")).not.toBeInTheDocument();
  });

  it.each(["absent", "expired", "foreign", "unknown-host"])("keeps %s authority disabled with the real LocalSession control", async mode => {
    const observed = routes();
    if (mode === "unknown-host") server.use(http.get("/main-agent/v1/health", () => HttpResponse.json({ status: "ok", service: "unknown" })));
    else server.use(http.get("/product/v1/session", () => mode === "absent" ? HttpResponse.json({ error: { code: "local_session_required", message: "private auth text" } }, { status: 401 }) : HttpResponse.json({ ...session, ...(mode === "expired" ? { expires_at: "2000-01-01T00:00:00Z" } : { workspace_id: rootB }) })));
    panel();
    expect(await screen.findByRole("button", { name: "接受成果" })).toBeDisabled();
    await userEvent.setup().click(screen.getByRole("button", { name: "本地会话" }));
    expect(await screen.findByRole("dialog", { name: "本地会话" })).toBeVisible();
    expect(observed.commands).toHaveLength(0);
    expect(observed.basis).toHaveLength(0);
    expect(screen.queryByText("private auth text")).not.toBeInTheDocument();
  });

  it("drops a held UI basis after root scope changes without a command, original write, or new-scope mutation", async () => {
    const observed = routes();
    const held = deferred();
    server.use(http.post(`${base}/${RESULT_CANDIDATE_ID}/acceptance-basis`, async ({ request }) => { observed.basis.push(await request.json()); await held.promise; return HttpResponse.json({ candidate_id: RESULT_CANDIDATE_ID, candidate_hash: "c".repeat(64), expected_result_version: 7, acceptance_basis_hash: "f".repeat(64) }); }));
    const basis = vi.spyOn(resultApi, "getTaskResultAcceptanceBasis");
    const rendered = panel();
    await acceptFromForm();
    await waitFor(() => expect(observed.basis).toHaveLength(1));
    rendered.rerenderRoot(rootB);
    await screen.findByText("等待用户验收");
    const identity = vi.spyOn(client, "createCommandIdentity");
    const set = vi.spyOn(Storage.prototype, "setItem");
    await act(async () => { held.release(); await basis.mock.results[0]!.value; });
    expect(identity).not.toHaveBeenCalled();
    expect(set).not.toHaveBeenCalled();
    expect(observed.commands).toHaveLength(0);
    expect(screen.queryByRole("button", { name: "查询原始验收" })).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    rendered.unmount();
  });
});
