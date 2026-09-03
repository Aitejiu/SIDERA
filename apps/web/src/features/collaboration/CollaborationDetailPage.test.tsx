import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { server } from "@/test/server";

import { CollaborationDetailPage } from "./CollaborationDetailPage";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const ROOT_TASK_ID = "30000000-0000-0000-0000-000000000001";
const detailPath = `/api/v1/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}/collaboration`;
const decisionsPath = `${detailPath}/decisions`;

const detail = {
  enabled: true,
  status: "attention",
  strategy_label: "all_required",
  run_id: "60000000-0000-0000-0000-000000000001",
  plan_generation: 4,
  released_count: 2,
  running_count: 1,
  waiting_count: 1,
  completed_count: 3,
  blocked_subtask_count: 1,
  blocked_subtasks: [{
    subtask_key: "payment-callback",
    title: "验证支付回调",
    reason: "等待必要证据",
    last_failure_at: null,
    href: `/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}/subtasks/payment-callback`,
  }],
  latest_decision_summary: "等待支付实现的验收证据",
  latest_decision_at: "2026-09-03T11:58:00Z",
  attention_required: true,
  detail_href: detailPath,
  provider: "reference",
  interface_version: "collaboration-runtime.v1",
  config_version: 3,
  config_summary: "配置版本 3",
};

const firstDecision = {
  decision_id: "80000000-0000-0000-0000-000000000001",
  sequence: 2,
  outcome: "intents",
  reason_code: "waiting_evidence",
  summary: "保持验证支付回调等待，直到上游证据齐备。",
  created_at: "2026-09-03T11:58:00Z",
  projection_id: "90000000-0000-0000-0000-000000000001",
  projection_hash: "a".repeat(64),
  target_paths: [detail.blocked_subtasks[0]],
  applications: [{
    intent_id: "a0000000-0000-0000-0000-000000000001",
    status: "rejected",
    result_code: "request_not_ready",
    result_summary: "请求仍在等待必要证据",
    release_id: null,
    error_code: "request_not_ready",
  }],
};

const governanceDecision = {
  decision_id: "80000000-0000-0000-0000-000000000002",
  sequence: 3,
  outcome: "intents",
  reason_code: "release_governance",
  summary: "记录本轮放行建议的处理结果。",
  created_at: "2026-09-03T12:04:00Z",
  projection_id: "90000000-0000-0000-0000-000000000002",
  projection_hash: "b".repeat(64),
  target_paths: [detail.blocked_subtasks[0]],
  applications: [
    {
      intent_id: "a0000000-0000-0000-0000-000000000002",
      status: "rejected",
      result_code: "governance_deferred",
      result_summary: "Proposal 等待处理",
      release_id: null,
      error_code: null,
      proposal_id: "b0000000-0000-0000-0000-000000000001",
      proposal_status: "pending",
      resolution_action: null,
      resolution_result: null,
      resolution_actor: null,
      resolution_transport: null,
    },
    {
      intent_id: "a0000000-0000-0000-0000-000000000003",
      status: "duplicate",
      result_code: "governance_proposal_duplicate",
      result_summary: "Proposal 已存在",
      release_id: null,
      error_code: null,
      proposal_id: null,
      proposal_status: null,
      resolution_action: null,
      resolution_result: null,
      resolution_actor: null,
      resolution_transport: null,
    },
    {
      intent_id: "a0000000-0000-0000-0000-000000000004",
      status: "applied",
      result_code: "governance_approved",
      result_summary: "Release 已签发",
      release_id: "c0000000-0000-0000-0000-000000000001",
      error_code: null,
      proposal_id: "b0000000-0000-0000-0000-000000000002",
      proposal_status: "approved",
      resolution_action: "approve",
      resolution_result: "applied",
      resolution_actor: "aitejiu",
      resolution_transport: "web",
    },
    {
      intent_id: "a0000000-0000-0000-0000-000000000005",
      status: "rejected",
      result_code: "governance_rejected",
      result_summary: "Proposal 已驳回",
      release_id: null,
      error_code: null,
      proposal_id: "b0000000-0000-0000-0000-000000000003",
      proposal_status: "rejected",
      resolution_action: "reject",
      resolution_result: "applied",
      resolution_actor: "aitejiu",
      resolution_transport: "main_agent",
    },
    {
      intent_id: "a0000000-0000-0000-0000-000000000006",
      status: "stale",
      result_code: "collaboration_release_proposal_stale",
      result_summary: "计划代已变化",
      release_id: null,
      error_code: null,
      proposal_id: "b0000000-0000-0000-0000-000000000004",
      proposal_status: "stale",
      resolution_action: "approve",
      resolution_result: "stale",
      resolution_actor: "aitejiu",
      resolution_transport: "web",
    },
  ],
};

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}/collaboration`]}>
        <Routes>
          <Route
            path="/workspaces/:workspaceId/tasks/:rootTaskId/collaboration"
            element={<CollaborationDetailPage />}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("CollaborationDetailPage", () => {
  beforeEach(() => {
    server.resetHandlers();
    server.use(
      http.get(detailPath, () => HttpResponse.json(detail)),
      http.get(decisionsPath, ({ request }) => {
        const url = new URL(request.url);
        if (url.searchParams.get("cursor") === "cursor-1") {
          return HttpResponse.json({ items: [], next_cursor: null });
        }
        return HttpResponse.json({ items: [firstDecision], next_cursor: "cursor-1" });
      }),
    );
  });

  it("shows current generation and redacted decision history with a bounded next page", async () => {
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByRole("heading", { name: "协同记录" })).toBeVisible();
    expect(screen.getByText(/GEN 4/)).toBeVisible();
    expect(screen.getByText("reference")).toBeVisible();
    const decision = await screen.findByRole("article", { name: "第 2 次协同决策" });
    expect(within(decision).getByText("保持验证支付回调等待，直到上游证据齐备。")).toBeVisible();
    expect(within(decision).getByRole("link", { name: "验证支付回调" })).toHaveAttribute(
      "href",
      detail.blocked_subtasks[0]!.href,
    );
    expect(within(decision).getByText("已拒绝")).toBeVisible();
    expect(within(decision).getByText("a".repeat(64))).not.toBeVisible();
    expect(within(decision).getByText("request_not_ready")).not.toBeVisible();

    await user.click(within(decision).getByText("查看诊断"));
    expect(within(decision).getByText("request_not_ready")).toBeVisible();
    expect(within(decision).getByText("a".repeat(64))).toBeVisible();

    await user.click(screen.getByRole("button", { name: "加载更早记录" }));
    await waitFor(() => expect(screen.getByText("没有更多协同记录")).toBeVisible());
  });

  it("offers retry when the collaboration detail cannot be read", async () => {
    const user = userEvent.setup();
    let attempts = 0;
    server.use(
      http.get(detailPath, () => {
        attempts += 1;
        return attempts === 1
          ? new HttpResponse(null, { status: 503 })
          : HttpResponse.json(detail);
      }),
    );
    renderPage();

    expect(await screen.findByText("无法读取协同记录")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "重试读取协同记录" }));
    expect(await screen.findByRole("heading", { name: "协同记录" })).toBeVisible();
  });

  it("shows compact governance lineage and audit metadata without exposing reasons", async () => {
    server.use(
      http.get(decisionsPath, () => HttpResponse.json({ items: [governanceDecision], next_cursor: null })),
    );
    renderPage();

    const decision = await screen.findByRole("article", { name: "第 3 次协同决策" });
    expect(within(decision).getByText("已延后放行")).toBeVisible();
    expect(within(decision).getAllByText("重复跳过").length).toBeGreaterThan(0);
    expect(within(decision).getByText("已批准")).toBeVisible();
    expect(within(decision).getByText("已驳回")).toBeVisible();
    expect(within(decision).getAllByText("已过期").length).toBeGreaterThan(0);
    expect(within(decision).getAllByText("aitejiu · web").length).toBeGreaterThan(0);
    expect(within(decision).getByText("aitejiu · main_agent")).toBeVisible();
    expect(within(decision).queryByText("Proposal 等待处理")).not.toBeInTheDocument();
    expect(within(decision).queryByText("计划代已变化")).not.toBeInTheDocument();
  });
});
