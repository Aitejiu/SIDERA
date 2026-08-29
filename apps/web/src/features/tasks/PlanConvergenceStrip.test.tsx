import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { PlanConvergenceStrip } from "./PlanConvergenceStrip";

const convergence = {
  status: "converging",
  from_generation: 2,
  target_generation: 3,
  total_count: 4,
  converged_count: 2,
  blocked_count: 1,
  unknown_count: 0,
  in_flight_count: 1,
  has_in_flight: true,
  has_unknown: false,
  has_active_fence: false,
  confirmation_blocked: true,
  plan_transition_id: "10000000-0000-0000-0000-000000000003",
  started_at: "2026-08-23T08:00:00Z",
  completed_at: null,
};

describe("PlanConvergenceStrip", () => {
  it("keeps the root summary quantitative and names the concrete blocked subtask", () => {
    render(
      <PlanConvergenceStrip
        convergence={convergence}
        blockedItems={[{
          key: "callback-idempotency",
          title: "回调幂等",
          detail: "等待确认重试规则",
        }]}
      />,
    );

    const region = screen.getByRole("region", { name: "计划收敛" });
    expect(within(region).getByText("GEN 2 → GEN 3")).toBeVisible();
    expect(within(region).getByText("已收敛 2 / 4")).toBeVisible();
    expect(within(region).getByText("当前卡点")).toBeVisible();
    expect(within(region).getByText("回调幂等")).toBeVisible();
    expect(within(region).getByText("等待确认重试规则")).toBeVisible();
  });

  it("explains a subtask branch without exposing adapter or session details", () => {
    render(
      <PlanConvergenceStrip
        label="分支收敛"
        convergence={{
          status: "waiting_dependency",
          from_generation: 2,
          target_generation: 3,
          total_count: 2,
          converged_count: 0,
          blocked_count: 0,
          unknown_count: 0,
          blocked_predecessor_paths: [["回调幂等", "实现幂等写入"]],
          plan_transition_id: "10000000-0000-0000-0000-000000000003",
          source_generation: 2,
        }}
      />,
    );

    const region = screen.getByRole("region", { name: "分支收敛" });
    expect(within(region).getByText("等待依赖")).toBeVisible();
    expect(within(region).getByText("阻塞前置路径")).toBeVisible();
    expect(within(region).getByText("回调幂等 / 实现幂等写入")).toBeVisible();
    expect(within(region).queryByText(/Session|adapter/i)).not.toBeInTheDocument();
  });
});
