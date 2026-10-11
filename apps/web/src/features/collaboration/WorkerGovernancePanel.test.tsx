import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { server } from "@/test/server";
import { workerResultInputsFixture } from "@/test/resultInputFixtures";
import type { ResultInputAction } from "../result-inputs/api";

import { WorkerGovernancePanel, type WorkerGovernancePanelProps } from "./WorkerGovernancePanel";
import type { WorkerCollaborationSummary } from "./types";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const WORKER_ITEM_ID = "40000000-0000-0000-0000-000000000001";
const PROPOSAL_ID = "50000000-0000-0000-0000-000000000001";
const HOLD_ID = "50000000-0000-0000-0000-000000000002";
const COMMAND_ID = "60000000-0000-0000-0000-000000000001";

function makeCollaboration(
  overrides: Partial<WorkerCollaborationSummary> = {},
): WorkerCollaborationSummary {
  return {
    active_hold_id: null,
    active_proposal: null,
    attention_state: "none",
    available_actions: [],
    enabled: true,
    governance_history: [],
    governance_mode: "automatic",
    hold_created_at: null,
    hold_reason: null,
    hold_resource_version: null,
    proposal_summary: null,
    release_state: "unreleased",
    state: "waiting_release",
    upstream_links: [],
    waiting_summary: "等待协同放行",
    ...overrides,
  };
}

function makeProps(
  collaboration = makeCollaboration(),
  overrides: Partial<WorkerGovernancePanelProps> = {},
): WorkerGovernancePanelProps {
  return {
    workspaceId: WORKSPACE_ID,
    workerItemId: WORKER_ITEM_ID,
    workerItemVersion: 7,
    targetPlanGeneration: 2,
    taskPath: "支付重构 / 回调幂等 / 实现回调幂等",
    canIntervene: true,
    collaboration,
    onRefresh: vi.fn(async () => undefined),
    ...overrides,
  };
}

const commandResponse = {
  aggregate_id: WORKER_ITEM_ID,
  aggregate_version: 8,
  command_id: COMMAND_ID,
  event_ids: ["70000000-0000-0000-0000-000000000001"],
};

function pauseDescriptor(): ResultInputAction {
  return {
    action: "pause", command_type: "PauseCollaborationRelease", target_type: "worker_item", target_id: WORKER_ITEM_ID,
    expected_version: 7, expected_fingerprint: null, expected_plan_generation: 2,
    expected_input_selection_id: null, expected_input_selection_hash: null,
  };
}

describe("WorkerGovernancePanel external native owner", () => {
  beforeEach(() => server.resetHandlers());

  it("delegates the reason and action once without invoking the legacy API branch", async () => {
    let legacyWrites = 0;
    server.use(http.post("/api/v1/commands", () => { legacyWrites += 1; return HttpResponse.json(commandResponse); }));
    const calls: Array<{ action: string; reason: string }> = [];
    const nativeActions = {
      reviewAction: () => pauseDescriptor(), busy: false, canSubmit: true, error: null,
      onGovernanceAction: async (action: string, reason: string) => { calls.push({ action, reason }); return true; },
    };
    render(<WorkerGovernancePanel {...makeProps(makeCollaboration({ available_actions: ["pause"] }), { canIntervene: false })} nativeActions={nativeActions} />);
    const user = userEvent.setup(); await user.click(screen.getByRole("button", { name: "暂停自动放行" }));
    const dialog = screen.getByRole("dialog"); await user.type(within(dialog).getByRole("textbox", { name: "暂停原因" }), "  Review first  ");
    await user.click(within(dialog).getByRole("button", { name: "确认暂停自动放行" }));
    expect(calls).toEqual([{ action: "pause", reason: "Review first" }]); expect(legacyWrites).toBe(0);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it.each(["proposal", "version", "selection", "generation"])("refuses a changed %s after opening a native governance dialog", async change => {
    let legacyWrites = 0; let nativeWrites = 0;
    server.use(http.post("/api/v1/commands", () => { legacyWrites += 1; return HttpResponse.json(commandResponse); }));
    const descriptor = { ...workerResultInputsFixture().available_actions[0]!, target_id: PROPOSAL_ID, expected_version: 8, expected_fingerprint: "a".repeat(64), expected_plan_generation: 2 };
    const proposal = {
      proposal_id: PROPOSAL_ID, resource_version: 8, status: "pending" as const, target_request_version: 17,
      summary: "Fixed review", reason_code: "ready", created_at: "2026-10-07T00:00:00Z",
      input_selection_id: descriptor.expected_input_selection_id, input_selection_hash: descriptor.expected_input_selection_hash,
    };
    let currentDescriptor = descriptor;
    const nativeActions = {
      reviewAction: () => currentDescriptor, busy: false, canSubmit: true, error: null,
      onGovernanceAction: async () => { nativeWrites += 1; return true; },
    };
    const props = makeProps(makeCollaboration({ available_actions: ["approve"], active_proposal: proposal }));
    const panel = render(<WorkerGovernancePanel {...props} nativeActions={nativeActions} />);
    const user = userEvent.setup(); await user.click(screen.getByRole("button", { name: "批准 Proposal" }));
    currentDescriptor = { ...descriptor };
    if (change === "proposal") currentDescriptor.target_id = HOLD_ID;
    if (change === "version") currentDescriptor.expected_version = 9;
    if (change === "selection") { currentDescriptor.expected_input_selection_hash = "b".repeat(64); currentDescriptor.expected_fingerprint = "b".repeat(64); }
    if (change === "generation") currentDescriptor.expected_plan_generation = 3;
    panel.rerender(<WorkerGovernancePanel {...props} collaboration={makeCollaboration({
      available_actions: ["approve"], active_proposal: { ...proposal, resource_version: 9 },
    })} nativeActions={nativeActions} />);
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText(`Proposal ${PROPOSAL_ID} · V8`)).toBeVisible();
    await user.click(within(dialog).getByRole("button", { name: "批准这次放行" }));
    expect(within(dialog).getByRole("alert")).toHaveTextContent("操作依据已变化");
    expect(nativeWrites).toBe(0); expect(legacyWrites).toBe(0);
  });

  it("consumes native read-only state before a principal is available", () => {
    render(<WorkerGovernancePanel {...makeProps(makeCollaboration({ available_actions: ["pause"] }))} nativeActions={{
      reviewAction: () => pauseDescriptor(), busy: false, canSubmit: false, error: null, onGovernanceAction: async () => false,
    }} />);
    expect(screen.getByRole("button", { name: "暂停自动放行" })).toBeDisabled();
  });
});

describe("WorkerGovernancePanel", () => {
  beforeEach(() => {
    server.resetHandlers();
  });

  it("pauses automatic release without claiming to stop a running Execution", async () => {
    const user = userEvent.setup();
    const requests: Record<string, unknown>[] = [];
    const onRefresh = vi.fn(async () => undefined);
    server.use(
      http.post("/api/v1/commands", async ({ request }) => {
        requests.push((await request.json()) as Record<string, unknown>);
        return HttpResponse.json(commandResponse);
      }),
    );

    render(<WorkerGovernancePanel {...makeProps(makeCollaboration({ available_actions: ["pause"] }), { onRefresh })} />);

    const panel = screen.getByRole("region", { name: "协同放行控制" });
    const trigger = within(panel).getByRole("button", { name: "暂停自动放行" });
    await user.click(trigger);
    const dialog = screen.getByRole("dialog", { name: "暂停自动放行" });
    expect(within(dialog).getByText(/不会停止当前 Worker 正在运行的 Execution/)).toBeVisible();
    await user.type(within(dialog).getByRole("textbox", { name: "暂停原因" }), "暂停以便审查");
    await user.click(within(dialog).getByRole("button", { name: "确认暂停自动放行" }));

    await screen.findByRole("region", { name: "协同放行控制" });
    expect(requests).toHaveLength(1);
    const request = requests[0];
    if (request === undefined) throw new Error("expected a captured command request");
    expect(request).toMatchObject({
      workspace_id: WORKSPACE_ID,
      command_type: "PauseCollaborationRelease",
      actor: { type: "local", id: "aitejiu" },
      target_type: "worker_item",
      target_id: WORKER_ITEM_ID,
      expected_version: 7,
    });
    expect(request.payload).toEqual({
      reason: "暂停以便审查",
      target_plan_generation: 2,
      transport: "web",
    });
    expect(onRefresh).toHaveBeenCalledTimes(1);
    expect(trigger).toHaveFocus();
  });

  it("keeps governance actions available when the current Session is read-only", () => {
    render(
      <WorkerGovernancePanel
        {...makeProps(
          makeCollaboration({ available_actions: ["pause"] }),
          { canIntervene: false },
        )}
      />,
    );

    const panel = screen.getByRole("region", { name: "协同放行控制" });
    expect(within(panel).getByRole("button", { name: "暂停自动放行" })).toBeEnabled();
  });

  it("mounts the operation dialog outside the WorkerItem sidebar stacking context", async () => {
    const user = userEvent.setup();
    render(
      <WorkerGovernancePanel
        {...makeProps(makeCollaboration({ available_actions: ["pause"] }))}
      />,
    );

    await user.click(screen.getByRole("button", { name: "暂停自动放行" }));
    const backdrop = document.querySelector(".governance-dialog-backdrop");
    expect(backdrop?.parentElement).toBe(document.body);
  });

  it("only exposes the actions returned by the server and keeps the Proposal scope visible", async () => {
    const user = userEvent.setup();
    const proposal = {
      created_at: "2026-09-04T08:00:00Z",
      proposal_id: PROPOSAL_ID,
      reason_code: "upstream_evidence_ready",
      resolved_at: null,
      resource_version: 8,
      status: "pending" as const,
      summary: "上游证据已齐，请确认是否放行回调实现",
      target_request_version: 3,
    };
    const collaboration = makeCollaboration({
      active_hold_id: HOLD_ID,
      active_proposal: proposal,
      attention_state: "decision_required",
      available_actions: ["approve", "reject", "resume_automatic"],
      governance_mode: "paused",
      hold_created_at: "2026-09-04T07:55:00Z",
      hold_reason: "等待人工审查",
      hold_resource_version: 4,
      proposal_summary: proposal.summary,
      release_state: "proposed",
    });
    server.use(http.post("/api/v1/commands", () => HttpResponse.json(commandResponse)));
    render(<WorkerGovernancePanel {...makeProps(collaboration)} />);

    const panel = screen.getByRole("region", { name: "协同放行控制" });
    expect(within(panel).getByText("已暂停")).toBeVisible();
    expect(within(panel).getByText("等待人工审查")).toBeVisible();
    expect(within(panel).getByText(proposal.summary)).toBeVisible();
    expect(within(panel).getByText("Proposal V8 · 请求 V3")).toBeVisible();
    expect(within(panel).getByRole("button", { name: "批准 Proposal" })).toBeVisible();
    expect(within(panel).getByRole("button", { name: "驳回 Proposal" })).toBeVisible();
    expect(within(panel).getByRole("button", { name: "恢复自动放行" })).toBeVisible();
    expect(within(panel).queryByRole("button", { name: "暂停自动放行" })).not.toBeInTheDocument();

    await user.click(within(panel).getByRole("button", { name: "批准 Proposal" }));
    let dialog = screen.getByRole("dialog", { name: "批准协同放行" });
    expect(within(dialog).getByText("支付重构 / 回调幂等 / 实现回调幂等")).toBeVisible();
    expect(within(dialog).getByText(proposal.summary)).toBeVisible();
    expect(within(dialog).getByRole("button", { name: "批准这次放行" })).toBeVisible();
    await user.click(within(dialog).getByRole("button", { name: "取消" }));

    await user.click(within(panel).getByRole("button", { name: "驳回 Proposal" }));
    dialog = screen.getByRole("dialog", { name: "驳回协同放行" });
    expect(within(dialog).getByText(`Proposal ${PROPOSAL_ID} · V8`)).toBeVisible();
    expect(within(dialog).getByRole("textbox", { name: "驳回原因" })).toBeVisible();
    await user.click(within(dialog).getByRole("button", { name: "取消" }));

    await user.click(within(panel).getByRole("button", { name: "恢复自动放行" }));
    dialog = screen.getByRole("dialog", { name: "恢复自动放行" });
    expect(within(dialog).getByText(/待处理 Proposal 会变为 stale/)).toBeVisible();
    expect(within(dialog).getByText(/Runtime 会重新评估/)).toBeVisible();
  });

  it("uses the exact Proposal and hold versions for approve, reject, and resume", async () => {
    const user = userEvent.setup();
    const requests: Record<string, unknown>[] = [];
    const proposal = {
      created_at: "2026-09-04T08:00:00Z",
      proposal_id: PROPOSAL_ID,
      reason_code: "upstream_evidence_ready",
      resolved_at: null,
      resource_version: 8,
      status: "pending" as const,
      summary: "上游证据已齐，请确认是否放行回调实现",
      target_request_version: 3,
    };
    server.use(
      http.post("/api/v1/commands", async ({ request }) => {
        requests.push((await request.json()) as Record<string, unknown>);
        return HttpResponse.json(commandResponse);
      }),
    );
    render(<WorkerGovernancePanel {...makeProps(makeCollaboration({
      active_hold_id: HOLD_ID,
      active_proposal: proposal,
      available_actions: ["approve", "reject", "resume_automatic"],
      governance_mode: "paused",
      hold_resource_version: 4,
      release_state: "proposed",
    }))} />);
    const panel = screen.getByRole("region", { name: "协同放行控制" });

    await user.click(within(panel).getByRole("button", { name: "批准 Proposal" }));
    await user.click(within(screen.getByRole("dialog", { name: "批准协同放行" })).getByRole("button", { name: "批准这次放行" }));
    await screen.findByRole("region", { name: "协同放行控制" });

    await user.click(within(panel).getByRole("button", { name: "驳回 Proposal" }));
    const rejectDialog = screen.getByRole("dialog", { name: "驳回协同放行" });
    await user.type(within(rejectDialog).getByRole("textbox", { name: "驳回原因" }), "证据仍不完整");
    await user.click(within(rejectDialog).getByRole("button", { name: "驳回这次放行" }));
    await screen.findByRole("region", { name: "协同放行控制" });

    await user.click(within(panel).getByRole("button", { name: "恢复自动放行" }));
    const resumeDialog = screen.getByRole("dialog", { name: "恢复自动放行" });
    await user.type(within(resumeDialog).getByRole("textbox", { name: "恢复说明" }), "人工审查已结束");
    await user.click(within(resumeDialog).getByRole("button", { name: "确认恢复自动放行" }));

    expect(requests).toHaveLength(3);
    expect(requests.map((request) => ({
      command_type: request.command_type,
      target_type: request.target_type,
      target_id: request.target_id,
      expected_version: request.expected_version,
      payload: request.payload,
    }))).toEqual([
      {
        command_type: "ApproveCollaborationReleaseProposal",
        target_type: "collaboration_release_proposal",
        target_id: PROPOSAL_ID,
        expected_version: 8,
        payload: { reason: null, transport: "web" },
      },
      {
        command_type: "RejectCollaborationReleaseProposal",
        target_type: "collaboration_release_proposal",
        target_id: PROPOSAL_ID,
        expected_version: 8,
        payload: { reason: "证据仍不完整", transport: "web" },
      },
      {
        command_type: "ResumeAutomaticCollaboration",
        target_type: "collaboration_governance_hold",
        target_id: HOLD_ID,
        expected_version: 4,
        payload: { reason: "人工审查已结束", transport: "web" },
      },
    ]);
  });

  it("binds approval to the current fixed input Selection", async () => {
    const user = userEvent.setup();
    let requestBody: Record<string, unknown> | null = null;
    const proposal = {
      created_at: "2026-09-04T08:00:00Z",
      proposal_id: PROPOSAL_ID,
      reason_code: "upstream_evidence_ready",
      resolved_at: null,
      resource_version: 8,
      status: "pending" as const,
      summary: "上游证据已齐，请确认是否放行回调实现",
      target_request_version: 3,
      input_selection_id: "61000000-0000-0000-0000-000000000001",
      input_selection_hash: "a".repeat(64),
    };
    server.use(
      http.post("/api/v1/commands", async ({ request }) => {
        requestBody = await request.json() as Record<string, unknown>;
        return HttpResponse.json(commandResponse);
      }),
    );
    render(
      <WorkerGovernancePanel
        {...makeProps(makeCollaboration({
          active_proposal: proposal,
          available_actions: ["approve"],
          attention_state: "decision_required",
          proposal_summary: proposal.summary,
          release_state: "proposed",
        }))}
        resultInputs={workerResultInputsFixture()}
      />,
    );

    const panel = screen.getByRole("region", { name: "协同放行控制" });
    await user.click(within(panel).getByRole("button", { name: "批准 Proposal" }));
    const dialog = screen.getByRole("dialog", { name: "批准协同放行" });
    expect(within(dialog).getByText("Selection 61000000 · Proposal V8")).toBeVisible();
    expect(within(dialog).getByText(/Research A · 技术评估 \/ 研究 A/)).toBeVisible();
    await user.click(within(dialog).getByRole("button", { name: "批准这次放行" }));

    await screen.findByRole("region", { name: "协同放行控制" });
    expect(requestBody).toMatchObject({
      command_type: "ApproveCollaborationReleaseProposal",
      target_id: PROPOSAL_ID,
      expected_version: 8,
      payload: {
        reason: null,
        transport: "web",
        expected_input_selection_id: "61000000-0000-0000-0000-000000000001",
        expected_input_selection_hash: "a".repeat(64),
      },
    });
  });

  it("keeps the dialog disabled while pending and refreshes after a version conflict", async () => {
    const user = userEvent.setup();
    let resolveRequest: (() => void) | undefined;
    const onRefresh = vi.fn(async () => undefined);
    server.use(
      http.post("/api/v1/commands", () => new Promise((resolve) => {
        resolveRequest = () => resolve(HttpResponse.json(commandResponse));
      })),
    );
    render(<WorkerGovernancePanel {...makeProps(makeCollaboration({ available_actions: ["pause"] }), { onRefresh })} />);
    const panel = screen.getByRole("region", { name: "协同放行控制" });
    await user.click(within(panel).getByRole("button", { name: "暂停自动放行" }));
    const dialog = screen.getByRole("dialog", { name: "暂停自动放行" });
    await user.type(within(dialog).getByRole("textbox", { name: "暂停原因" }), "等待确认");
    const submit = within(dialog).getByRole("button", { name: "确认暂停自动放行" });
    await user.click(submit);
    expect(submit).toBeDisabled();
    resolveRequest?.();
    await screen.findByRole("region", { name: "协同放行控制" });
    expect(onRefresh).toHaveBeenCalledTimes(1);

    server.use(
      http.post(
        "/api/v1/commands",
        () => HttpResponse.json(
          { error: { code: "version_conflict", message: "协作状态已变化" } },
          { status: 409 },
        ),
      ),
    );
    await user.click(within(panel).getByRole("button", { name: "暂停自动放行" }));
    const conflictDialog = screen.getByRole("dialog", { name: "暂停自动放行" });
    await user.type(within(conflictDialog).getByRole("textbox", { name: "暂停原因" }), "再次暂停");
    await user.click(within(conflictDialog).getByRole("button", { name: "确认暂停自动放行" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("协作状态已变化");
    expect(onRefresh).toHaveBeenCalledTimes(2);
  });

  it("returns focus to the action that opened the dialog", async () => {
    const user = userEvent.setup();
    render(<WorkerGovernancePanel {...makeProps(makeCollaboration({ available_actions: ["pause"] }))} />);
    const trigger = screen.getByRole("button", { name: "暂停自动放行" });
    await user.click(trigger);
    await user.keyboard("{Escape}");
    expect(trigger).toHaveFocus();
  });
});
