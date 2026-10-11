import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { PlanningProposal } from "./types";
import { ProposalView } from "./ProposalView";

const PROPOSAL_HASH = "b".repeat(64);
const PROFILE_ID = "50000000-0000-0000-0000-000000000001";

const proposal = {
  id: "a0000000-0000-0000-0000-000000000001",
  planning_request_id: "30000000-0000-0000-0000-000000000001",
  revision: 1,
  brief_revision: 1,
  planning_run_id: "40000000-0000-0000-0000-000000000001",
  proposal_schema_version: 2,
  kind: "create",
  profile_version: 2,
  created_at: "2026-08-23T08:00:00Z",
  content_hash: PROPOSAL_HASH,
  derived_diff: {
    root_task_added: true,
    added_subtask_keys: ["prepare", "execute"],
    added_worker_item_keys: ["prepare-worker", "execute-worker"],
    added_subtask_dependency_edges: [["prepare", "execute"]],
    added_worker_item_dependency_edges: [["prepare-worker", "execute-worker", "hard"]],
    added_execution_intent_keys: ["prepare-worker", "execute-worker"],
    immediately_ready_worker_item_keys: ["prepare-worker"],
    blocked_worker_item_keys: ["execute-worker"],
  },
  display: {
    root_task: {
      title: "交付控制面页面",
      goal_summary: "让开发者确认后进入可追踪的任务工作台。",
      acceptance_refs: ["用户可以审阅执行架构"],
    },
    subtasks: [
      {
        key: "prepare",
        kind: "implementation",
        title: "准备页面",
        goal_summary: "整理任务状态和审阅入口。",
        acceptance_refs: ["状态可读"],
        expected_outputs: ["页面结构"],
        milestone_key: null,
        source_refs: ["brief:1"],
      },
      {
        key: "execute",
        kind: "implementation",
        title: "执行页面",
        goal_summary: "承载确认后的任务追踪。",
        acceptance_refs: ["任务可追踪"],
        expected_outputs: ["任务视图"],
        milestone_key: null,
        source_refs: ["brief:1"],
      },
    ],
    worker_items: [
      {
        key: "prepare-worker",
        subtask_key: "prepare",
        title: "状态编排 Worker",
        role: "planner-ui",
        responsibility: "提供任务状态和确认前摘要。",
        capability_requirements: ["structured_output", "ui"],
        worker_policy_ref: "default-worker-policy",
        configuration_ref: "planning-ui-v1",
        concurrency_limit: 1,
        source_refs: ["brief:1"],
        pinned_worker_id: null,
      },
      {
        key: "execute-worker",
        subtask_key: "execute",
        title: "任务追踪 Worker",
        role: "task-ui",
        responsibility: "呈现 WorkerItem 进度和介入入口。",
        capability_requirements: ["trace", "ui"],
        worker_policy_ref: null,
        configuration_ref: null,
        concurrency_limit: 2,
        source_refs: ["brief:1"],
        pinned_worker_id: null,
      },
    ],
    milestones: [],
    subtask_dependencies: [
      {
        upstream_subtask_key: "prepare",
        downstream_subtask_key: "execute",
        kind: "hard",
        required_evidence_refs: [],
        enforcement: "gate",
      },
    ],
    worker_item_dependencies: [
      {
        upstream_worker_item_key: "prepare-worker",
        downstream_worker_item_key: "execute-worker",
        kind: "hard",
        required_artifact_refs: ["page-structure"],
      },
    ],
    execution_intents: [],
    decision_summary: {
      understanding: "先交付一个可审阅、可确认的 Task First 方案。",
      assumptions: ["沿用现有任务工作台"],
      risks: ["确认后需要等待投影刷新"],
      estimated_tokens: 1800,
      estimated_cost_microunits: 42,
      estimated_runtime_seconds: 90,
      estimated_concurrency: 2,
      scope_includes: ["方案审阅", "确认后跳转"],
      scope_excludes: ["运行时重规划"],
    },
    impact_policy: {
      activate_immediately: false,
      budget_level: "standard",
      failure_policy: "block_downstream",
      manual_gate: true,
      revision_operation: null,
      },
  },
  collaboration: {
    mode: "enabled",
    default_strategy: "all_required",
    subtask_overrides: [],
    provider: "reference",
    runtime_interface: "collaboration-runtime.v1",
    runtime_config_version: 1,
    input_groups: [{
      group_key: "page-structure",
      title: "页面结构结果",
      consumer_worker_item_key: "execute-worker",
      candidates: [{ worker_item_key: "prepare-worker", priority: 1 }],
      requirements: {
        execution_status: "succeeded",
        required_artifact_refs: ["artifact_type:page"],
        required_evidence_refs: ["acceptance:page-structure"],
        evidence_status: "candidate",
      },
      mode: "all",
      minimum_count: null,
      selection_strategy: "plan_order",
    }],
  },
} as unknown as PlanningProposal;

function renderProposal(overrides: Partial<React.ComponentProps<typeof ProposalView>> = {}) {
  return render(
    <ProposalView
      proposal={proposal}
      requestVersion={4}
      profileId={PROFILE_ID}
      profileVersion={2}
      onConfirm={vi.fn()}
      {...overrides}
    />,
  );
}

describe("ProposalView", () => {
  it("starts with a task-first review instead of mounting the DAG", () => {
    renderProposal();

    expect(screen.getByText("方案理解")).toBeVisible();
    expect(screen.getByText("先交付一个可审阅、可确认的 Task First 方案。")).toBeVisible();
    expect(screen.getByText("准备页面")).toBeVisible();
    expect(screen.getAllByText("状态编排 Worker").length).toBeGreaterThan(0);
    expect(screen.getByText("页面结构结果")).toBeVisible();
    expect(screen.getByText("全部 1 / 1")).toBeVisible();
    expect(screen.queryByTestId("proposal-graph-canvas")).not.toBeInTheDocument();
  });

  it("reveals worker responsibility and loads the DAG only on demand", async () => {
    const user = userEvent.setup();
    renderProposal();

    await user.click(screen.getByRole("button", { name: "展开子任务：准备页面" }));
    expect(screen.getByText("提供任务状态和确认前摘要。")).toBeVisible();
    await user.click(screen.getByRole("tab", { name: "依赖 DAG" }));

    expect(await screen.findByTestId("proposal-graph-canvas")).toBeVisible();
    await waitFor(() => expect(screen.getByText("准备页面 → 执行页面")).toBeVisible());
  });

  it("shows a compact diff with readiness instead of raw proposal JSON", async () => {
    const user = userEvent.setup();
    renderProposal();

    await user.click(screen.getByRole("tab", { name: "变更" }));

    expect(screen.getByText("新增 2 个子任务")).toBeVisible();
    expect(screen.getByText("已就绪：状态编排 Worker")).toBeVisible();
    expect(screen.getByText("等待依赖：任务追踪 Worker")).toBeVisible();
    expect(screen.queryByText(/"worker_items"/)).not.toBeInTheDocument();
  });

  it("locks confirmation when the reviewed request version drifts", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    const view = renderProposal({ onConfirm });

    view.rerender(
      <ProposalView
        proposal={proposal}
        requestVersion={5}
        profileId={PROFILE_ID}
        profileVersion={2}
        onConfirm={onConfirm}
      />,
    );

    expect(screen.getByText("方案版本已变化，请重新查看后再确认。")).toBeVisible();
    const confirm = screen.getByRole("button", { name: "确认并创建任务" });
    expect(confirm).toBeDisabled();
    await user.click(confirm);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("keeps stale-review recovery available beside the missing-owner reason", async () => {
    const user = userEvent.setup();
    const anchor = {
      requestVersion: 4,
      proposalRevision: 1,
      proposalContentHash: PROPOSAL_HASH,
      profileId: PROFILE_ID,
      profileVersion: 2,
    };
    const onRevisit = vi.fn();
    const onConfirm = vi.fn();
    renderProposal({
      requestVersion: 5,
      reviewedBinding: anchor,
      disabledReason: "Prepare 尚未分配负责人",
      onRevisit,
      onConfirm,
    });

    expect(screen.getByText("Prepare 尚未分配负责人")).toBeVisible();
    expect(screen.getByText("方案版本已变化，请重新查看后再确认。")).toBeVisible();
    const revisit = screen.getByRole("button", { name: "重新查看最新方案" });
    expect(revisit).toBeVisible();
    const confirm = screen.getByRole("button", { name: "确认并创建任务" });
    expect(confirm).toBeDisabled();
    await user.click(revisit);
    await user.click(confirm);
    expect(onRevisit).toHaveBeenCalledOnce();
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("passes the exact reviewed proposal binding to confirmation", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    renderProposal({ onConfirm });

    await user.click(screen.getByRole("button", { name: "确认并创建任务" }));

    expect(onConfirm).toHaveBeenCalledWith({
      requestVersion: 4,
      proposalRevision: 1,
      proposalContentHash: PROPOSAL_HASH,
      profileId: PROFILE_ID,
      profileVersion: 2,
    });
  });

  it("keeps a persisted native review anchor across a remount and local owner edits", async () => {
    const user = userEvent.setup();
    const anchor = {
      requestVersion: 4,
      proposalRevision: 1,
      proposalContentHash: PROPOSAL_HASH,
      profileId: PROFILE_ID,
      profileVersion: 2,
    };
    const onConfirm = vi.fn();
    const view = renderProposal({ reviewedBinding: anchor, responsibilityContent: <div>Owner rows</div>, onConfirm });
    expect(screen.getByText("Owner rows")).toBeVisible();

    view.unmount();
    renderProposal({
      requestVersion: 5,
      reviewedBinding: anchor,
      responsibilityContent: <div>Owner rows after local edit</div>,
      onConfirm,
    });

    expect(screen.getByText("Owner rows after local edit")).toBeVisible();
    expect(screen.getByText("方案版本已变化，请重新查看后再确认。")).toBeVisible();
    expect(screen.getByRole("button", { name: "确认并创建任务" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "确认并创建任务" }));
    expect(onConfirm).not.toHaveBeenCalled();
  });
});
