import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

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
});
