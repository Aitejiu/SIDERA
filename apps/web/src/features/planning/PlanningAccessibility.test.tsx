import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { PlanningProposal } from "./types";
import { ProposalView } from "./ProposalView";

const proposal = {
  id: "proposal-1",
  planning_request_id: "request-1",
  revision: 1,
  brief_revision: 1,
  planning_run_id: "run-1",
  proposal_schema_version: 2,
  kind: "create",
  profile_version: 1,
  created_at: "2026-08-23T08:00:00Z",
  content_hash: "a".repeat(64),
  derived_diff: {
    root_task_added: true,
    added_subtask_keys: ["prepare"],
    added_worker_item_keys: ["prepare-worker"],
    added_subtask_dependency_edges: [],
    added_worker_item_dependency_edges: [],
    added_execution_intent_keys: [],
    immediately_ready_worker_item_keys: ["prepare-worker"],
    blocked_worker_item_keys: [],
  },
  display: {
    root_task: {
      title: "控制面任务",
      goal_summary: "审阅执行架构",
      acceptance_refs: [],
    },
    subtasks: [{
      key: "prepare",
      kind: "implementation",
      title: "准备页面",
      goal_summary: "整理页面结构",
      acceptance_refs: [],
      expected_outputs: ["页面"],
      milestone_key: null,
      source_refs: [],
    }],
    worker_items: [{
      key: "prepare-worker",
      subtask_key: "prepare",
      title: "页面 Worker",
      role: "frontend",
      responsibility: "实现页面",
      capability_requirements: [],
      worker_policy_ref: null,
      configuration_ref: null,
      concurrency_limit: 1,
      source_refs: [],
      pinned_worker_id: null,
    }],
    milestones: [],
    subtask_dependencies: [],
    worker_item_dependencies: [],
    execution_intents: [],
    decision_summary: {
      understanding: "审阅执行架构",
      assumptions: [],
      risks: [],
      estimated_tokens: 10,
      estimated_cost_microunits: 1,
      estimated_runtime_seconds: 1,
      estimated_concurrency: 1,
      scope_includes: [],
      scope_excludes: [],
    },
    impact_policy: {
      activate_immediately: false,
      budget_level: "standard",
      failure_policy: "block_downstream",
      manual_gate: true,
      revision_operation: null,
    },
  },
} as unknown as PlanningProposal;

describe("Planning accessibility", () => {
  it("connects proposal tabs to labelled tab panels and keeps the DAG lazy", async () => {
    const user = userEvent.setup();
    render(
      <ProposalView
        proposal={proposal}
        requestVersion={1}
        profileId="profile-1"
        profileVersion={1}
        onConfirm={vi.fn()}
      />,
    );

    const architectureTab = screen.getByRole("tab", { name: "执行架构" });
    expect(architectureTab).toHaveAttribute("aria-controls", "proposal-panel-architecture");
    expect(screen.getByRole("tabpanel", { name: "执行架构" })).toBeVisible();
    expect(screen.queryByTestId("proposal-graph-canvas")).not.toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "依赖 DAG" }));
    expect(screen.getByRole("tabpanel", { name: "依赖 DAG" })).toBeVisible();
    expect(await screen.findByTestId("proposal-graph-canvas")).toBeVisible();
  });

  it("explains why a stale proposal confirmation is disabled", () => {
    const view = render(
      <ProposalView
        proposal={proposal}
        requestVersion={1}
        profileId="profile-1"
        profileVersion={1}
        onConfirm={vi.fn()}
      />,
    );

    view.rerender(
      <ProposalView
        proposal={proposal}
        requestVersion={2}
        profileId="profile-1"
        profileVersion={1}
        onConfirm={vi.fn()}
      />,
    );

    const confirm = screen.getByRole("button", { name: "确认并创建任务" });
    expect(confirm).toBeDisabled();
    expect(confirm).toHaveAttribute("aria-describedby", "proposal-stale-reason");
    expect(screen.getByRole("alert")).toHaveAttribute(
      "id",
      "proposal-stale-reason",
    );
  });
});
