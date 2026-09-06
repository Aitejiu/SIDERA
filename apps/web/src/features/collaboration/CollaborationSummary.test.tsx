import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { collaborationSummaryFixture } from "@/test/resultInputFixtures";

import { CollaborationSummary } from "./CollaborationSummary";
import type { RootCollaborationSummary } from "./types";

const summary: RootCollaborationSummary = {
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
    last_failure_at: "2026-09-03T12:00:00Z",
    href: "/workspaces/ws-1/tasks/root-1/subtasks/payment-callback",
  }],
  latest_decision_summary: "等待支付实现的验收证据",
  latest_decision_at: "2026-09-03T11:58:00Z",
  attention_required: true,
  detail_href: "/workspaces/ws-1/tasks/root-1/collaboration",
  paused_worker_item_count: 0,
  pending_release_proposal_count: 0,
  attention_subtasks: [],
};

describe("CollaborationSummary", () => {
  it("puts the blocked subtask and dispatch counts before technical details", () => {
    render(
      <MemoryRouter>
        <CollaborationSummary summary={summary} onOpenSubtask={vi.fn()} />
      </MemoryRouter>,
    );

    expect(screen.getByRole("region", { name: "协同调度" })).toBeVisible();
    expect(screen.getAllByText("需要关注").length).toBeGreaterThan(0);
    expect(screen.getByText("验证支付回调")).toBeVisible();
    expect(screen.getByText("等待必要证据")).toBeVisible();
    expect(screen.getByLabelText("已放行 2")).toBeVisible();
    expect(screen.getByLabelText("执行中 1")).toBeVisible();
    expect(screen.getByLabelText("等待中 1")).toBeVisible();
    expect(screen.getByLabelText("已完成 3")).toBeVisible();
    expect(screen.queryByText(summary.run_id)).not.toBeInTheDocument();
  });

  it("links to the read-only collaboration record", () => {
    render(
      <MemoryRouter>
        <CollaborationSummary summary={summary} onOpenSubtask={vi.fn()} />
      </MemoryRouter>,
    );

    expect(screen.getByRole("link", { name: "查看协同记录" })).toHaveAttribute(
      "href",
      summary.detail_href,
    );
  });

  it("names the affected subtask and pending proposal count before worker detail", async () => {
    const user = userEvent.setup();
    const openSubtask = vi.fn();
    render(
      <MemoryRouter>
        <CollaborationSummary
          summary={{
            ...summary,
            blocked_subtasks: [],
            blocked_subtask_count: 0,
            attention_required: true,
            pending_release_proposal_count: 2,
            attention_subtasks: [{
              deep_link: "/workspaces/ws-1/worker-items/worker-1",
              first_worker_item_title: "审查者",
              pending_count: 2,
              subtask_key: "payment-callback",
              subtask_title: "验证支付回调",
            }],
          }}
          onOpenSubtask={openSubtask}
        />
      </MemoryRouter>,
    );

    expect(screen.getByText("等待你决定")).toBeVisible();
    expect(screen.getByText("2 个 Proposal 待确认")).toBeVisible();
    expect(screen.queryByText("审查者")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "打开子任务：验证支付回调" }));
    expect(openSubtask).toHaveBeenCalledWith("payment-callback");
  });

  it("shows pending approvals and a different blocked subtask together", () => {
    render(
      <MemoryRouter>
        <CollaborationSummary
          summary={collaborationSummaryFixture()}
          onOpenSubtask={vi.fn()}
          rootTaskTitle="技术评估"
        />
      </MemoryRouter>,
    );

    expect(screen.getByLabelText("待决定 Proposal")).toBeVisible();
    expect(screen.getByLabelText("协同卡点")).toBeVisible();
    expect(screen.getByText("技术评估 / 汇总结果")).toBeVisible();
    expect(screen.getByText("技术评估 / 研究结果输入")).toBeVisible();
    expect(screen.getByText("等待 Research C 的必要证据")).toBeVisible();
  });
});
