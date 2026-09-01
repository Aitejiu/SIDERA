import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { server } from "@/test/server";

import { HandoffProgress } from "./HandoffProgress";
import { HandoffDialog } from "./HandoffDialog";
import type {
  HandoffCandidateList,
  HandoffProposal,
  HandoffTransition,
  WorkerWorkspace,
} from "./types";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const ROOT_TASK_ID = "30000000-0000-0000-0000-000000000001";
const SUBTASK_ID = "30000000-0000-0000-0000-000000000002";
const WORKER_ITEM_ID = "40000000-0000-0000-0000-000000000001";
const SOURCE_WORKER_ID = "80000000-0000-0000-0000-000000000001";
const TARGET_WORKER_ID = "80000000-0000-0000-0000-000000000002";
const INELIGIBLE_WORKER_ID = "80000000-0000-0000-0000-000000000003";
const SOURCE_EXECUTION_ID = "60000000-0000-0000-0000-000000000001";
const SOURCE_SESSION_ID = "70000000-0000-0000-0000-000000000001";
const TARGET_EXECUTION_ID = "60000000-0000-0000-0000-000000000002";
const TARGET_SESSION_ID = "70000000-0000-0000-0000-000000000002";
const PROPOSAL_ID = "a0000000-0000-0000-0000-000000000001";
const TRANSITION_ID = "b0000000-0000-0000-0000-000000000001";

const workspace = {
  workspace_id: WORKSPACE_ID,
  worker_item: {
    id: WORKER_ITEM_ID,
    stable_key: "callback-implementer",
    title: "实现回调幂等",
    responsibility: "实现幂等写入和重试策略",
    root_task_id: ROOT_TASK_ID,
    status: "running",
    context_version: 4,
    acknowledged_context_version: 4,
    version: 7,
    updated_at: "2026-08-23T08:05:00Z",
  },
  can_intervene: true,
  read_only_reason: null,
  current_runtime: {
    adapter_type: "opencode",
    attempt: 2,
    execution_id: SOURCE_EXECUTION_ID,
    execution_status: "running",
    fencing_token: 8,
    input_mode: "queued",
    lease_id: "lease-1",
    session_id: SOURCE_SESSION_ID,
    session_status: "active",
    session_version: 4,
    task_id: SUBTASK_ID,
    worker_display_name: "Source Agent",
    worker_id: SOURCE_WORKER_ID,
  },
} as unknown as WorkerWorkspace;

const candidates: HandoffCandidateList = {
  workspace_id: WORKSPACE_ID,
  worker_item_id: WORKER_ITEM_ID,
  source_execution_id: SOURCE_EXECUTION_ID,
  source_session_id: SOURCE_SESSION_ID,
  source_worker_id: SOURCE_WORKER_ID,
  source_worker_item_version: 7,
  candidates: [
    {
      worker_id: TARGET_WORKER_ID,
      display_name: "Target Agent",
      adapter_type: "opencode",
      eligible: true,
      recommended: true,
      ineligibility_code: null,
      runtime_snapshot: { adapter_type: "opencode" },
      capability_snapshot_hash: "a".repeat(64),
      policy_reason: { selected: "lowest_cost" },
      workspace_compatibility: { trust_boundary_match: true },
      runtime_profile_label: "OpenCode · v1",
      capabilities_satisfied: ["python"],
      health_summary: "idle; load 0/2",
      policy_reasons: [{ code: "recommended" }],
      inspected_at: "2026-08-23T08:05:00Z",
    },
    {
      worker_id: INELIGIBLE_WORKER_ID,
      display_name: "Blocked Agent",
      adapter_type: "dsh",
      eligible: false,
      recommended: false,
      ineligibility_code: "handoff_workspace_incompatible",
      runtime_snapshot: null,
      capability_snapshot_hash: null,
      policy_reason: { selected: "rejected" },
      workspace_compatibility: { trust_boundary_match: false },
      runtime_profile_label: "DSH · v1",
      capabilities_satisfied: [],
      health_summary: "not eligible",
      policy_reasons: [{ code: "workspace_incompatible" }],
      inspected_at: "2026-08-23T08:05:00Z",
    },
  ],
  recommended_worker_id: TARGET_WORKER_ID,
  blocking_codes: [],
  inspected_at: "2026-08-23T08:05:00Z",
};

function workerSummary(workerId: string, displayName: string) {
  return {
    worker_id: workerId,
    display_name: displayName,
    adapter_type: "opencode",
    version: 3,
    status: "idle",
    desired_state: "active",
    current_load: 0,
    capacity: 2,
    capabilities: ["python"],
    runtime_profile_label: "OpenCode · v1",
    capability_snapshot_hash: "a".repeat(64),
  };
}

function runtimeSummary() {
  return {
    adapter_module_instance_id: "d0000000-0000-0000-0000-000000000001",
    adapter_module_config_version: 1,
    workspace_module_instance_id: "e0000000-0000-0000-0000-000000000001",
    workspace_module_config_version: 1,
    capability_snapshot_hash: "a".repeat(64),
  };
}

function traceDescriptor(executionId: string, sessionId: string) {
  return {
    root_task_id: ROOT_TASK_ID,
    execution_id: executionId,
    session_id: sessionId,
    route: `/api/v1/tasks/${ROOT_TASK_ID}/executions/${executionId}/sessions/${sessionId}/trace`,
    raw_ref: null,
  };
}

function executionSummary(
  executionId: string,
  sessionId: string,
  status: string,
) {
  return {
    execution_id: executionId,
    task_id: SUBTASK_ID,
    status,
    attempt: 2,
    version: 4,
    session_id: sessionId,
    session_status: status === "running" ? "active" : "closed",
    session_version: 4,
    last_sequence: 4,
    trace_descriptor: traceDescriptor(executionId, sessionId),
  };
}

const proposal: HandoffProposal = {
  id: PROPOSAL_ID,
  workspace_id: WORKSPACE_ID,
  root_task_id: ROOT_TASK_ID,
  subtask_id: SUBTASK_ID,
  task_title: "支付重构",
  subtask_title: "回调幂等",
  worker_item: {
    worker_item_id: WORKER_ITEM_ID,
    title: "实现回调幂等",
    responsibility: "实现幂等写入和重试策略",
    status: "running",
    version: 7,
    context_version: 4,
  },
  status: "prepared",
  stale_reason: null,
  version: 1,
  proposal_hash: "f".repeat(64),
  source_channel: "web",
  created_by_type: "local",
  created_by_id: "aitejiu",
  created_at: "2026-08-23T08:05:00Z",
  expires_at: "2026-08-23T09:05:00Z",
  confirmed_at: null,
  cancelled_at: null,
  handoff_reason: "需要专门的 Agent 处理回调重试",
  handoff_instruction: "保留已有测试上下文，继续完成重试策略",
  transfer_mode: "reuse_exact",
  source: {
    worker: workerSummary(SOURCE_WORKER_ID, "Source Agent"),
    runtime: runtimeSummary(),
    execution: executionSummary(SOURCE_EXECUTION_ID, SOURCE_SESSION_ID, "running"),
    worker_item_id: WORKER_ITEM_ID,
    context_version: 4,
    workspace_manifest_id: "f0000000-0000-0000-0000-000000000001",
    workspace_manifest_version: 2,
  },
  target: {
    worker: workerSummary(TARGET_WORKER_ID, "Target Agent"),
    runtime: runtimeSummary(),
    execution: null,
    worker_item_id: WORKER_ITEM_ID,
    context_version: 4,
    workspace_manifest_id: null,
    workspace_manifest_version: null,
  },
  workspace: {
    transfer_mode: "reuse_exact",
    source_manifest_id: "f0000000-0000-0000-0000-000000000001",
    source_manifest_version: 2,
    source_lifecycle: "active",
    target_manifest_id: null,
    target_manifest_version: null,
    target_lifecycle: null,
    compatibility: { trust_boundary_match: true },
  },
  context: {
    source_package_id: "90000000-0000-0000-0000-000000000001",
    source_package_hash: "b".repeat(64),
    source_context_version: 4,
    target_package_id: null,
    target_package_hash: null,
    target_context_version: 4,
  },
  plan_base: {
    snapshot_id: "10000000-0000-0000-0000-000000000001",
    generation: 2,
    head_version: 5,
  },
  input_plan: {},
  impact_summary: { worker_item_version: 7 },
};

const blockedTransition: HandoffTransition = {
  ...proposal,
  id: TRANSITION_ID,
  proposal_id: PROPOSAL_ID,
  proposal_status: "confirmed",
  version: 2,
  status: "blocked_unknown",
  phase: "start_target_session",
  writer_state: "unknown",
  logical_attempt: 1,
  source_input_cutoff_sequence: 4,
  target_context_version: 4,
  failure_code: "target_start_unknown",
  blocked_operation_id: "c0000000-0000-0000-0000-000000000001",
  next_attempt_at: null,
  confirmed_at: "2026-08-23T08:06:00Z",
  started_at: "2026-08-23T08:06:00Z",
  completed_at: null,
  attention_required: true,
  available_recovery_strategies: ["confirm_target_absent", "select_new_target"],
  source: {
    ...proposal.source,
    execution: executionSummary(SOURCE_EXECUTION_ID, SOURCE_SESSION_ID, "closed"),
  },
  target: {
    ...proposal.target,
    execution: executionSummary(TARGET_EXECUTION_ID, TARGET_SESSION_ID, "failed"),
  },
  workspace: {
    ...proposal.workspace,
    target_manifest_id: "f1000000-0000-0000-0000-000000000001",
    target_manifest_version: 1,
    target_lifecycle: "preparing",
  },
  context: {
    ...proposal.context,
    target_package_id: "90000000-0000-0000-0000-000000000002",
    target_package_hash: "c".repeat(64),
  },
  source_trace_descriptor: traceDescriptor(SOURCE_EXECUTION_ID, SOURCE_SESSION_ID),
  target_trace_descriptor: traceDescriptor(TARGET_EXECUTION_ID, TARGET_SESSION_ID),
  recovery_evidence_refs: ["event:handoff-1"],
  recovery_inspected_at: "2026-08-23T08:07:00Z",
} as unknown as HandoffTransition;

function renderDialog(
  onRefreshCandidates = vi.fn(async () => undefined),
  onRefresh = vi.fn(async () => undefined),
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <HandoffDialog
          workspace={workspace}
          taskTitle="支付重构"
          subtaskTitle="回调幂等"
          candidates={candidates}
          candidatesPending={false}
          candidatesError={null}
          onRefreshCandidates={onRefreshCandidates}
          onRefresh={onRefresh}
          onClose={vi.fn()}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("HandoffDialog", () => {
  beforeEach(() => {
    server.resetHandlers();
  });

  it("keeps the chooser in the current tab, preserves draft fields, and reviews bounded facts", async () => {
    const user = userEvent.setup();
    const onRefreshCandidates = vi.fn(async () => undefined);
    let requestBody: Record<string, unknown> | undefined;
    server.use(
      http.post("/api/v1/commands", async ({ request }) => {
        requestBody = await request.json() as Record<string, unknown>;
        return HttpResponse.json({
          command_id: "command-1",
          aggregate_id: PROPOSAL_ID,
          aggregate_version: 1,
          event_ids: [],
        });
      }),
      http.get(
        `/api/v1/workspaces/${WORKSPACE_ID}/handoff-proposals/${PROPOSAL_ID}`,
        () => HttpResponse.json(proposal),
      ),
    );
    renderDialog(onRefreshCandidates);

    const dialog = await screen.findByRole("dialog", { name: "调整执行 Agent" });
    expect(within(dialog).getByRole("radio", { name: /Target Agent/ })).toBeEnabled();
    expect(within(dialog).getByRole("radio", { name: /Blocked Agent/ })).toBeDisabled();
    expect(within(dialog).getByText("推荐目标")).toBeVisible();
    expect(within(dialog).getByText("handoff_workspace_incompatible")).toBeVisible();

    await user.click(within(dialog).getByRole("radio", { name: /Target Agent/ }));
    await user.type(within(dialog).getByRole("textbox", { name: "切换原因" }), "需要专门的 Agent");
    await user.type(within(dialog).getByRole("textbox", { name: "交接指令" }), "保留测试上下文");
    await user.click(within(dialog).getByRole("button", { name: "刷新候选" }));

    expect(within(dialog).getByRole("textbox", { name: "切换原因" })).toHaveValue("需要专门的 Agent");
    expect(within(dialog).getByRole("textbox", { name: "交接指令" })).toHaveValue("保留测试上下文");
    expect(onRefreshCandidates).toHaveBeenCalledTimes(1);

    await user.click(within(dialog).getByRole("button", { name: "查看切换方案" }));
    const review = await screen.findByRole("dialog", { name: "确认 Agent 切换" });
    expect(within(review).getByText("支付重构")).toBeVisible();
    expect(within(review).getByText("回调幂等")).toBeVisible();
    expect(within(review).getByText("实现回调幂等")).toBeVisible();
    expect(within(review).getByText("Source Agent")).toBeVisible();
    expect(within(review).getByText("Target Agent")).toBeVisible();
    expect(within(review).getByText("reuse_exact")).toBeVisible();
    expect(within(review).getByText("Proposal hash")).toBeVisible();
    expect(requestBody?.command_type).toBe("PrepareWorkerHandoff");
    expect(requestBody?.target_id).toBe(WORKER_ITEM_ID);
  });

  it("confirms in place and exposes source/target trace links with progress", async () => {
    const user = userEvent.setup();
    const onRefresh = vi.fn(async () => undefined);
    const requests: Record<string, unknown>[] = [];
    server.use(
      http.post("/api/v1/commands", async ({ request }) => {
        const body = await request.json() as Record<string, unknown>;
        requests.push(body);
        return HttpResponse.json({
          command_id: "command-2",
          aggregate_id: body.command_type === "PrepareWorkerHandoff" ? PROPOSAL_ID : TRANSITION_ID,
          aggregate_version: body.command_type === "PrepareWorkerHandoff" ? 1 : 2,
          event_ids: [],
        });
      }),
      http.get(
        `/api/v1/workspaces/${WORKSPACE_ID}/handoff-proposals/${PROPOSAL_ID}`,
        () => HttpResponse.json(proposal),
      ),
      http.get(
        `/api/v1/workspaces/${WORKSPACE_ID}/handoff-transitions/${TRANSITION_ID}`,
        () => HttpResponse.json(blockedTransition),
      ),
    );
    renderDialog(undefined, onRefresh);
    const dialog = await screen.findByRole("dialog", { name: "调整执行 Agent" });
    await user.click(within(dialog).getByRole("radio", { name: /Target Agent/ }));
    await user.type(within(dialog).getByRole("textbox", { name: "切换原因" }), "需要专门的 Agent");
    await user.type(within(dialog).getByRole("textbox", { name: "交接指令" }), "继续执行");
    await user.click(within(dialog).getByRole("button", { name: "查看切换方案" }));
    const review = await screen.findByRole("dialog", { name: "确认 Agent 切换" });
    await user.click(within(review).getByRole("button", { name: "确认切换" }));

    const progress = await screen.findByRole("region", { name: "Agent 切换进度" });
    expect(within(progress).getByText("目标 Agent 启动结果未知")).toBeVisible();
    expect(within(progress).getByText("Source Agent")).toBeVisible();
    expect(within(progress).getByText("Target Agent")).toBeVisible();
    expect(within(progress).getByRole("link", { name: "查看源 Agent Trace" })).toHaveAttribute(
      "href",
      `/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}/executions/${SOURCE_EXECUTION_ID}/sessions/${SOURCE_SESSION_ID}/trace`,
    );
    expect(within(progress).getByRole("link", { name: "查看目标 Agent Trace" })).toHaveAttribute(
      "href",
      `/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}/executions/${TARGET_EXECUTION_ID}/sessions/${TARGET_SESSION_ID}/trace`,
    );
    expect(onRefresh).toHaveBeenCalled();
    expect(requests[0]?.command_type).toBe("PrepareWorkerHandoff");
    expect(requests[1]?.command_type).toBe("ConfirmWorkerHandoff");
    expect(requests[1]?.target_id).toBe(PROPOSAL_ID);
  });

  it("disables confirmation for a stale proposal", async () => {
    const user = userEvent.setup();
    server.use(
      http.post("/api/v1/commands", () => HttpResponse.json({
        command_id: "command-3",
        aggregate_id: PROPOSAL_ID,
        aggregate_version: 1,
        event_ids: [],
      })),
      http.get(
        `/api/v1/workspaces/${WORKSPACE_ID}/handoff-proposals/${PROPOSAL_ID}`,
        () => HttpResponse.json({ ...proposal, status: "stale", stale_reason: "source_changed" }),
      ),
    );
    renderDialog();
    const dialog = await screen.findByRole("dialog", { name: "调整执行 Agent" });
    await user.click(within(dialog).getByRole("radio", { name: /Target Agent/ }));
    await user.type(within(dialog).getByRole("textbox", { name: "切换原因" }), "重新分配");
    await user.type(within(dialog).getByRole("textbox", { name: "交接指令" }), "保留当前上下文");
    await user.click(within(dialog).getByRole("button", { name: "查看切换方案" }));

    const review = await screen.findByRole("dialog", { name: "确认 Agent 切换" });
    expect(within(review).getByText("方案已失效")).toBeVisible();
    expect(within(review).getByRole("button", { name: "确认切换" })).toBeDisabled();
  });
});

describe("HandoffProgress", () => {
  beforeEach(() => {
    server.resetHandlers();
  });

  it("shows only backend-allowed recovery strategies", async () => {
    const user = userEvent.setup();
    let requestBody: Record<string, unknown> | undefined;
    server.use(
      http.get(
        `/api/v1/workspaces/${WORKSPACE_ID}/handoff-transitions/${TRANSITION_ID}`,
        () => HttpResponse.json(blockedTransition),
      ),
      http.post("/api/v1/commands", async ({ request }) => {
        requestBody = await request.json() as Record<string, unknown>;
        return HttpResponse.json({ command_id: "command-4", aggregate_id: TRANSITION_ID, aggregate_version: 3, event_ids: [] });
      }),
    );
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter>
          <HandoffProgress
            workspaceId={WORKSPACE_ID}
            transitionId={TRANSITION_ID}
            onRefresh={vi.fn(async () => undefined)}
          />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    const progress = await screen.findByRole("region", { name: "Agent 切换进度" });
    expect(within(progress).getByText("确认目标尚未启动")).toBeVisible();
    expect(within(progress).getByText("选择新的目标 Agent")).toBeVisible();
    expect(within(progress).queryByText("强制成功")).not.toBeInTheDocument();
    await user.type(within(progress).getByRole("textbox", { name: "恢复原因" }), "已核对目标边界");
    await user.type(within(progress).getByRole("textbox", { name: "证据引用" }), "event:handoff-2");
    await user.click(within(progress).getByRole("button", { name: "提交恢复策略" }));
    await waitFor(() => expect(requestBody?.command_type).toBe("RecoverWorkerHandoff"));
    expect(requestBody?.target_type).toBe("handoff_transition");
    expect(requestBody?.target_id).toBe(TRANSITION_ID);
  });
});
