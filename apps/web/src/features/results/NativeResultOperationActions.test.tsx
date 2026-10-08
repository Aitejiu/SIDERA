import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import type { PropsWithChildren } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CommandRequest } from "@/features/assignments/types";
import type { WorkerWorkspace } from "@/features/worker-items/types";
import * as client from "@/shared/api/client";
import { server } from "@/test/server";

import { type ResultAction, type TaskResultOverview } from "./api";
import { loadNativeResultOperationDraft } from "./nativeResultOperations";
import { ResultOperationActions, type ResultOperationActionsProps } from "./ResultOperationActions";
import { RESULT_CANDIDATE_ID, RESULT_OPERATION_ID, RESULT_ROOT_TASK_ID, RESULT_WORKSPACE_ID, resultAction, resultView, resultViewScenario } from "./testFixtures";

const workerId = "71000000-0000-0000-0000-000000000021";
const mappingId = "71000000-0000-0000-0000-000000000040";
const nextId = "71000000-0000-0000-0000-000000000041";
const scope = { workspace_id: RESULT_WORKSPACE_ID, root_task_id: RESULT_ROOT_TASK_ID, worker_item_id: workerId, principal_id: "human-current" };
const session = { session_id: "current-human-session", workspace_id: RESULT_WORKSPACE_ID, principal_id: scope.principal_id, expires_at: "2099-01-01T00:00:00Z" };
type OperationKind = "RequestTaskRework" | "AuthorizeAdditionalRework" | "RetryResultVerification" | "ReintegrateTaskResult";

function action(kind: OperationKind): ResultAction {
  return { ...resultAction("RequestTaskRework"), command_type: kind,
    target_type: kind === "RetryResultVerification" ? "result_operation" : "task",
    target_id: kind === "RetryResultVerification" ? RESULT_OPERATION_ID : RESULT_ROOT_TASK_ID,
    expected_version: kind === "RetryResultVerification" ? 3 : 7,
    allowed_worker_item_ids: kind === "RequestTaskRework" ? [workerId] : [],
    replacement_result_ids: kind === "ReintegrateTaskResult" ? [mappingId] : [] };
}

function workspace(): WorkerWorkspace {
  return { workspace_id: RESULT_WORKSPACE_ID, worker_item: { id: workerId, root_task_id: RESULT_ROOT_TASK_ID,
    stable_key: "implementation", title: "Implementation", responsibility: "Local repair", status: "active", version: 4,
    context_version: 1, acknowledged_context_version: 1, updated_at: "2026-10-07T00:00:00Z" },
    current_placement: { worker_item_key: "implementation", role: "implementation", subtask_key: "repair", subtask_task_id: null,
      plan_snapshot_id: mappingId, plan_generation: 1, capability_requirements: [] },
    current_runtime: null, current_step: null, next_action: null, latest_checkpoint: null,
    usage: { input_tokens: 0, output_tokens: 0, cached_tokens: 0, cost_microunits: 0 }, artifacts: [], evidence: [],
    execution_history: [], placement_history: [], intervention_context: null, trace_descriptor: null, can_intervene: false,
    read_only_reason: "adapter_unavailable", last_change_cursor: 19, as_of: "2026-10-07T00:00:00Z" };
}

function receipt(command: CommandRequest, inspection = false) {
  const data: Record<string, Record<string, unknown>> = {
    RequestTaskRework: { rework_record_id: mappingId, request_ids: [nextId], target_worker_item_ids: [workerId], impact_worker_item_ids: [workerId], status: "requested", round_number: null },
    AuthorizeAdditionalRework: { auto_rounds_used: 2, auto_rounds_limit: 4, auto_seconds_limit: 1200, auto_deadline: null },
    RetryResultVerification: inspection ? { operation_id: RESULT_OPERATION_ID, attempt: 1, status: "unknown", inspection_required: true }
      : { operation_id: nextId, previous_operation_id: RESULT_OPERATION_ID, attempt: 2, status: "pending", execution_request_id: mappingId },
    ReintegrateTaskResult: { source_candidate_id: RESULT_CANDIDATE_ID, execution_request_id: mappingId, input_selection_id: nextId,
      release_id: mappingId, integration_operation_id: nextId, rework_record_id: mappingId, target_observation_id: null, status: "scheduled" },
  };
  const retry = command.command_type === "RetryResultVerification";
  return { command_id: command.command_id, aggregate_id: retry ? inspection ? RESULT_OPERATION_ID : nextId : RESULT_ROOT_TASK_ID,
    aggregate_version: retry ? inspection ? 3 : 1 : command.command_type === "RequestTaskRework" ? 7 : 8, event_ids: [], data: data[command.command_type] };
}

function routes(kind: OperationKind, inspection = false) {
  const observed = { commands: [] as CommandRequest[], lookups: [] as string[], csrf: [] as (string | null)[],
    savedAtPost: [] as unknown[], results: 0, workspaces: 0, health: 0, sessions: 0 };
  server.use(
    http.get("/main-agent/v1/health", () => { observed.health += 1; return HttpResponse.json({ status: "ok", service: "sidera-product" }); }),
    http.get("/product/v1/session", () => { observed.sessions += 1; return HttpResponse.json(session); }),
    http.get(`/api/v1/workspaces/${RESULT_WORKSPACE_ID}/tasks/${RESULT_ROOT_TASK_ID}/results`, () => {
      observed.results += 1; return HttpResponse.json(resultView({ available_actions: [action(kind)] }));
    }),
    http.get(`/api/v1/workspaces/${RESULT_WORKSPACE_ID}/worker-items/${workerId}/workspace`, () => {
      observed.workspaces += 1; return HttpResponse.json(workspace());
    }),
    http.post("/api/v1/commands", async ({ request }) => {
      const command = await request.json() as CommandRequest; observed.commands.push(command);
      observed.csrf.push(request.headers.get("X-Sidera-CSRF")); observed.savedAtPost.push(loadNativeResultOperationDraft(scope));
      return HttpResponse.json(receipt(command, inspection));
    }),
    http.get(`/api/v1/workspaces/${RESULT_WORKSPACE_ID}/commands/:commandId`, ({ params }) => {
      observed.lookups.push(String(params.commandId)); const original = observed.commands[0]!;
      return HttpResponse.json({ command_id: original.command_id, command_type: original.command_type, target_type: original.target_type,
        target_id: original.target_id, expected_version: original.expected_version, payload: original.payload, status: "applied", result: receipt(original, inspection) });
    }),
  );
  return observed;
}

function mount(view: TaskResultOverview | undefined, overrides: Partial<ResultOperationActionsProps> = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const props: ResultOperationActionsProps = { workspaceId: RESULT_WORKSPACE_ID, rootTaskId: RESULT_ROOT_TASK_ID, workerItemId: workerId,
    view, isLoading: false, isError: false, canIntervene: false, canOperateResult: true, onRetry: () => {},
    onRefresh: async () => {}, onOpenResults: () => {}, ...overrides };
  const wrapper = ({ children }: PropsWithChildren) => <QueryClientProvider client={queryClient}><MemoryRouter>{children}</MemoryRouter></QueryClientProvider>;
  const rendered = render(<ResultOperationActions {...props} />, { wrapper });
  return { ...rendered, rerenderProps: (changes: Partial<ResultOperationActionsProps>) => rendered.rerender(<ResultOperationActions {...props} {...changes} />) };
}

async function open(name: string) {
  await waitFor(() => expect(screen.getByRole("button", { name })).toBeEnabled());
  await userEvent.setup().click(screen.getByRole("button", { name }));
}

async function submitRework() {
  const user = userEvent.setup(); await open("局部返工");
  const form = screen.getByRole("form", { name: "局部返工" });
  await user.click(within(form).getByLabelText("Expired credentials are rejected"));
  await user.type(within(form).getByLabelText("返工说明"), "修复当前 WorkerItem");
  await user.click(within(form).getByRole("button", { name: "提交局部返工" }));
}

beforeEach(() => { localStorage.clear(); document.cookie = "sidera_csrf=native-result-ui-csrf; path=/"; });
afterEach(() => { vi.restoreAllMocks(); document.cookie = "sidera_csrf=; Max-Age=0; path=/"; });

describe("native existing Worker result controls", () => {
  it("submits the real local rework form as the current human with unchanged result head 7", async () => {
    const observed = routes("RequestTaskRework"); mount(resultView({ available_actions: [action("RequestTaskRework")] }));
    await submitRework(); await waitFor(() => expect(observed.commands).toHaveLength(1));
    expect(observed.commands[0]).toMatchObject({ actor: { type: "user", id: scope.principal_id }, command_type: "RequestTaskRework",
      target_type: "task", target_id: RESULT_ROOT_TASK_ID, expected_version: 7, payload: { candidate_id: RESULT_CANDIDATE_ID,
        candidate_hash: "c".repeat(64), mode: "user", issue_keys: ["expired-credentials"], target_worker_item_ids: [workerId],
        impact_worker_item_ids: [workerId], reason: "修复当前 WorkerItem", suggestion: null } });
    await waitFor(() => expect(screen.getByText("局部返工已请求")).toBeVisible());
    expect(observed.savedAtPost).toEqual([{ ok: true, draft: expect.objectContaining({ original: observed.commands[0], status: "pending" }) }]);
    expect(observed.csrf).toEqual(["native-result-ui-csrf"]); expect([observed.results, observed.workspaces]).toEqual([1, 1]);
  });

  it("submits budget increments through the real form and displays refreshed cumulative usage", async () => {
    const observed = routes("AuthorizeAdditionalRework");
    const rendered = mount(resultView({ available_actions: [action("AuthorizeAdditionalRework")], rework_budget: {
      auto_rounds_used: 2, auto_rounds_limit: 3, auto_seconds_limit: 900, auto_started_at: null, auto_deadline: null } }));
    const user = userEvent.setup(); await open("增加返工额度");
    await user.clear(screen.getByLabelText("追加轮数")); await user.type(screen.getByLabelText("追加轮数"), "1");
    await user.clear(screen.getByLabelText("追加秒数")); await user.type(screen.getByLabelText("追加秒数"), "300");
    await user.type(screen.getByLabelText("额度调整原因"), "继续局部返工"); await user.click(screen.getByRole("button", { name: "提交额度调整" }));
    await waitFor(() => expect(screen.getByText("返工额度已授权")).toBeVisible());
    expect(observed.commands[0]).toMatchObject({ command_type: "AuthorizeAdditionalRework", expected_version: 7,
      payload: { additional_rounds: 1, additional_seconds: 300, reason: "继续局部返工" } });
    rendered.rerenderProps({ view: resultView({ result_version: 8, available_actions: [], rework_budget: {
      auto_rounds_used: 2, auto_rounds_limit: 4, auto_seconds_limit: 1200, auto_started_at: null, auto_deadline: null } }) });
    expect(screen.getByText("2 / 4 轮")).toBeVisible(); expect(screen.getByText("累计预算 · 20 分钟")).toBeVisible();
    expect(observed.commands).toHaveLength(1);
  });

  it.each([false, true])("submits the real retry form for the independent operation fence, inspection=%s", async (inspection) => {
    const observed = routes("RetryResultVerification", inspection); mount(resultView({ available_actions: [action("RetryResultVerification")] }));
    const user = userEvent.setup(); await open("重试检查"); await user.type(screen.getByLabelText("操作原因"), "核对检查进程");
    await user.click(screen.getByRole("button", { name: "提交重试检查" }));
    await waitFor(() => expect(screen.getByText(inspection ? "原检查状态未知，需要核对" : "检查重试已安排")).toBeVisible());
    expect(observed.commands[0]).toMatchObject({ command_type: "RetryResultVerification", target_type: "result_operation",
      target_id: RESULT_OPERATION_ID, expected_version: 3, payload: { verification_operation_id: RESULT_OPERATION_ID,
        expected_operation_version: 3, expected_result_version: 7, candidate_id: RESULT_CANDIDATE_ID, candidate_hash: "c".repeat(64), reason: "核对检查进程" } });
    expect(receipt(observed.commands[0]!, inspection).aggregate_version).toBe(inspection ? 3 : 1);
    expect(observed.commands).toHaveLength(1); expect(screen.queryByText(/Task\.succeeded|任务已成功/)).not.toBeInTheDocument();
  });

  it("submits the real reintegration form with the frozen source and replacements, displaying scheduled integration", async () => {
    const observed = routes("ReintegrateTaskResult"); mount(resultView({ available_actions: [action("ReintegrateTaskResult")] }));
    const user = userEvent.setup(); await open("重新集成"); await user.type(screen.getByLabelText("操作原因"), "采用已审阅替换结果");
    await user.click(screen.getByRole("button", { name: "提交重新集成" }));
    await waitFor(() => expect(screen.getByText("重新集成已安排")).toBeVisible());
    expect(observed.commands[0]).toMatchObject({ command_type: "ReintegrateTaskResult", expected_version: 7, payload: {
      source_candidate_id: RESULT_CANDIDATE_ID, source_candidate_hash: "c".repeat(64), replacement_result_ids: [mappingId],
      expected_result_version: 7, target_observation_id: null, reason: "采用已审阅替换结果" } });
    expect(screen.queryByText(/Task\.succeeded|任务已成功/)).not.toBeInTheDocument();
  });

  it("keeps the lost original GET visible through form close, removed action, changed candidate, and reopen", async () => {
    const observed = routes("RequestTaskRework");
    server.use(http.post("/api/v1/commands", async ({ request }) => { observed.commands.push(await request.json() as CommandRequest); return HttpResponse.error(); }));
    const rendered = mount(resultView({ available_actions: [action("RequestTaskRework")] }));
    await submitRework(); await waitFor(() => expect(screen.getByRole("button", { name: "查询原始结果操作" })).toBeEnabled());
    await userEvent.setup().click(screen.getByRole("button", { name: "取消" }));
    rendered.rerenderProps({ view: resultView({ available_actions: [], result_version: 8, current_candidate: null }) });
    expect(screen.getByRole("button", { name: "查询原始结果操作" })).toBeVisible(); rendered.unmount();
    mount(undefined, { isError: true, canOperateResult: false });
    await waitFor(() => expect(screen.getByRole("button", { name: "查询原始结果操作" })).toBeEnabled());
    const identity = vi.spyOn(client, "createCommandIdentity");
    await userEvent.setup().click(screen.getByRole("button", { name: "查询原始结果操作" }));
    await waitFor(() => expect(screen.getByText("局部返工已请求")).toBeVisible());
    expect(observed.commands).toHaveLength(1); expect(observed.lookups).toEqual([observed.commands[0]!.command_id]);
    expect([observed.results, observed.workspaces]).toEqual([1, 1]); expect(identity).not.toHaveBeenCalled();
  });

  it("keeps exact recovery and acknowledged handling when the current result read fails", async () => {
    const observed = routes("RequestTaskRework"); const refresh = vi.fn(async () => { throw new Error("private provider detail"); });
    const rendered = mount(resultView({ available_actions: [action("RequestTaskRework")] }), { onRefresh: refresh });
    await submitRework(); await waitFor(() => expect(screen.getByText("结果操作已确认，当前成果暂未刷新")).toBeVisible());
    rendered.rerenderProps({ view: undefined, isError: true });
    expect(screen.getByText("局部返工已请求")).toBeVisible();
    await userEvent.setup().click(screen.getByRole("button", { name: "查询原始结果操作" }));
    await waitFor(() => expect(observed.lookups).toEqual([observed.commands[0]!.command_id]));
    expect(loadNativeResultOperationDraft(scope)).toMatchObject({ ok: true, draft: { status: "applied" } }); expect(observed.commands).toHaveLength(1);
  });

  it("does not silently retarget a form when the candidate/action changes after it opens", async () => {
    const observed = routes("ReintegrateTaskResult"); const rendered = mount(resultView({ available_actions: [action("ReintegrateTaskResult")] }));
    const user = userEvent.setup(); await open("重新集成"); await user.type(screen.getByLabelText("操作原因"), "已审阅原候选");
    const nextAction = { ...action("ReintegrateTaskResult"), candidate_id: nextId, candidate_hash: "b".repeat(64), expected_fingerprint: "b".repeat(64) };
    const view = resultView({ available_actions: [nextAction] });
    view.current_candidate = { ...view.current_candidate!, candidate_id: nextId, candidate_hash: "b".repeat(64) };
    rendered.rerenderProps({ view });
    server.use(http.get(`/api/v1/workspaces/${RESULT_WORKSPACE_ID}/tasks/${RESULT_ROOT_TASK_ID}/results`, () => HttpResponse.json(view)));
    await user.click(screen.getByRole("button", { name: "提交重新集成" }));
    await waitFor(() => expect(screen.getByText("结果操作已变化，请重新审阅")).toBeVisible()); expect(observed.commands).toHaveLength(0);
  });

  it.each(["unknown host", "unavailable host", "expired", "foreign workspace"])("offers authority retry and no legacy POST for %s", async (change) => {
    const observed = routes("RequestTaskRework");
    if (change.endsWith("host")) server.use(http.get("/main-agent/v1/health", () => { observed.health += 1;
      return change === "unavailable host" ? HttpResponse.error() : HttpResponse.json({ status: "ok", service: "unknown-native" }); }));
    else server.use(http.get("/product/v1/session", () => { observed.sessions += 1;
      return HttpResponse.json({ ...session, ...(change === "expired" ? { expires_at: "2000-01-01T00:00:00Z" } : { workspace_id: nextId }) }); }));
    mount(resultView({ available_actions: [action("RequestTaskRework")] }));
    await waitFor(() => expect(change.endsWith("host") ? observed.health : observed.sessions).toBe(1));
    await waitFor(() => expect(screen.getByRole("button", { name: "本地会话" })).toBeVisible());
    expect(screen.getByRole("button", { name: "局部返工" })).toBeDisabled(); expect(observed.commands).toHaveLength(0);
  });

  it("blocks a foreign current principal from the original GET and keeps the prior human's record intact", async () => {
    const observed = routes("RequestTaskRework");
    server.use(http.post("/api/v1/commands", async ({ request }) => { observed.commands.push(await request.json() as CommandRequest); return HttpResponse.error(); }));
    const rendered = mount(resultView({ available_actions: [action("RequestTaskRework")] }));
    await submitRework(); await waitFor(() => expect(screen.getByRole("button", { name: "查询原始结果操作" })).toBeEnabled());
    server.use(http.get("/product/v1/session", () => HttpResponse.json({ ...session, principal_id: "foreign-human" })));
    await userEvent.setup().click(screen.getByRole("button", { name: "查询原始结果操作" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "本地会话" })).toBeVisible());
    expect(observed.lookups).toHaveLength(0); expect(observed.commands).toHaveLength(1);
    expect(loadNativeResultOperationDraft(scope)).toMatchObject({ ok: true, draft: { original: observed.commands[0] } });
    rendered.unmount(); mount(undefined, { isError: true });
    await waitFor(() => expect(screen.queryByRole("button", { name: "本地会话" })).not.toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "查询原始结果操作" })).not.toBeInTheDocument();
  });

  it("keeps storage recovery available without command submission", async () => {
    const observed = routes("RequestTaskRework"); const get = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("private storage"); });
    mount(resultView({ available_actions: [action("RequestTaskRework")] }));
    await waitFor(() => expect(screen.getByText("本地结果操作记录暂不可用")).toBeVisible()); get.mockRestore();
    await userEvent.setup().click(screen.getByRole("button", { name: "重试本地结果操作记录" }));
    await waitFor(() => expect(screen.queryByText("本地结果操作记录暂不可用")).not.toBeInTheDocument()); expect(observed.commands).toHaveLength(0);
  });

  it("keeps the Revision fallback and InspectResultOperation read entry", async () => {
    routes("RequestTaskRework"); const inspect = resultViewScenario("unknown-apply").available_actions[0]!;
    const onOpenResults = vi.fn(); mount(resultView({ available_actions: [
      { ...action("RequestTaskRework"), allowed_worker_item_ids: [nextId] }, inspect,
    ] }), { onOpenResults });
    expect(screen.getByText("当前 WorkerItem 不在可返工范围，需要主 Agent / Revision 调整")).toBeVisible();
    expect(screen.queryByRole("button", { name: "局部返工" })).not.toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: "核对应用状态" })); expect(onOpenResults).toHaveBeenCalledWith(RESULT_CANDIDATE_ID);
  });
});
