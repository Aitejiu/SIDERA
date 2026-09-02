import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { RevisionDiff } from "./types";
import { ProposalActions } from "./ProposalActions";
import { RevisionDagDiff } from "./RevisionDagDiff";
import { RevisionDecisionSummary } from "./RevisionDecisionSummary";
import { RevisionHierarchyDiff } from "./RevisionHierarchyDiff";
import { RevisionImpactPanel } from "./RevisionImpactPanel";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const base = {
  root_task_id: "30000000-0000-0000-0000-000000000001",
  plan_snapshot_id: "50000000-0000-0000-0000-000000000001",
  plan_generation: 3,
  plan_content_hash: "a".repeat(64),
  root_task_version: 8,
  workspace_id: WORKSPACE_ID,
  schema_version: 1,
  captured_at: "2026-08-29T08:05:00Z",
};

const revisionDiff: RevisionDiff = {
  workspace_id: WORKSPACE_ID,
  planning_request_id: "40000000-0000-0000-0000-000000000001",
  proposal_revision_id: "80000000-0000-0000-0000-000000000001",
  root_task_id: base.root_task_id,
  proposal_revision: 2,
  proposal_content_hash: "b".repeat(64),
  diff_hash: "c".repeat(64),
  base,
  freshness: {
    base,
    base_fresh: true,
    checked_at: "2026-08-29T08:05:00Z",
    current_plan_content_hash: base.plan_content_hash,
    current_plan_generation: 3,
    current_plan_snapshot_id: base.plan_snapshot_id,
    current_root_task_version: base.root_task_version,
    stale_reasons: [],
  },
  target_descriptor_preview: {
    parent_snapshot_id: base.plan_snapshot_id,
    target_generation: 4,
    target_plan_content_hash: "d".repeat(64),
  },
  impact_counts: {
    preserve: 2,
    patch: 1,
    continue: 1,
    create: 1,
    derive: 1,
    move: 1,
    retire: 1,
    affected_branches: 2,
  },
  risk_flags: ["worker_context_patch", "dependency_change"],
  affected_branch_keys: ["api", "verification"],
  affected_branch_paths: ["支付重构 / API", "支付重构 / 验证"],
  collaboration: {
    mode: "disabled",
    default_strategy: null,
    subtask_overrides: [],
    provider: null,
    runtime_interface: null,
    runtime_config_version: null,
  },
  collaboration_policy_changes: [],
  root_changes: [{
    field_path: "acceptance_criteria",
    before: ["旧验收"],
    after: ["新验收"],
  }],
  subtask_changes: [
    {
      target_subtask_key: "api",
      path: "支付重构 / API",
      strategy: "preserve",
      reason: "沿用已经开始的 API 子任务",
      existing_task_id: "90000000-0000-0000-0000-000000000001",
      field_changes: [{ field_path: "goal", before: "旧目标", after: "新目标" }],
    },
    {
      target_subtask_key: "verification",
      path: "支付重构 / 验证",
      strategy: "move",
      reason: "移动到新的执行分支",
      existing_task_id: "90000000-0000-0000-0000-000000000002",
      field_changes: [],
    },
    {
      target_subtask_key: "observability",
      path: "支付重构 / 可观测性",
      strategy: "create",
      reason: "新增验收所需的观测步骤",
      existing_task_id: null,
      field_changes: [],
    },
    {
      target_subtask_key: "derived-checks",
      path: "支付重构 / 派生检查",
      strategy: "derive",
      reason: "从新的验收条件派生",
      existing_task_id: null,
      field_changes: [],
    },
    {
      target_subtask_key: "legacy-checks",
      path: "支付重构 / 旧检查",
      strategy: "retire",
      reason: "旧检查已不再属于活动范围",
      existing_task_id: "90000000-0000-0000-0000-000000000003",
      field_changes: [],
    },
  ],
  worker_item_changes: [
    {
      target_worker_item_key: "backend",
      path: "支付重构 / API / Backend",
      strategy: "patch",
      context_disposition: "patch",
      from_subtask_key: "api",
      to_subtask_key: "api",
      existing_worker_item_id: "a0000000-0000-0000-0000-000000000001",
      reasons: ["目标变化，需要补丁同步"],
      field_changes: [{ field_path: "responsibility", before: "旧职责", after: "新职责" }],
    },
    {
      target_worker_item_key: "review",
      path: "支付重构 / 验证 / Review",
      strategy: "continue",
      context_disposition: "continue",
      from_subtask_key: "verification",
      to_subtask_key: "verification",
      existing_worker_item_id: "a0000000-0000-0000-0000-000000000002",
      reasons: ["保留当前上下文继续执行"],
      field_changes: [],
    },
    {
      target_worker_item_key: "telemetry",
      path: "支付重构 / 可观测性 / Telemetry",
      strategy: "create",
      context_disposition: "create",
      from_subtask_key: null,
      to_subtask_key: "observability",
      existing_worker_item_id: null,
      reasons: ["新增 WorkerItem"],
      field_changes: [],
    },
    {
      target_worker_item_key: "derived",
      path: "支付重构 / 派生检查 / Derived",
      strategy: "derive",
      context_disposition: "derive",
      from_subtask_key: null,
      to_subtask_key: "derived-checks",
      existing_worker_item_id: null,
      reasons: ["由验收条件派生"],
      field_changes: [],
    },
    {
      target_worker_item_key: "legacy",
      path: "支付重构 / 旧检查 / Legacy",
      strategy: "retire",
      context_disposition: "retire",
      from_subtask_key: "legacy-checks",
      to_subtask_key: "legacy-checks",
      existing_worker_item_id: "a0000000-0000-0000-0000-000000000003",
      reasons: ["退出活动计划"],
      field_changes: [],
    },
  ],
  subtask_dependency_changes: [{
    change_type: "added",
    dependency_kind: "hard",
    downstream_key: "verification",
    downstream_path: "支付重构 / 验证",
    graph_scope: "subtask",
    upstream_key: "api",
    upstream_path: "支付重构 / API",
  }],
  worker_dependency_changes: [{
    change_type: "removed",
    dependency_kind: "informational",
    downstream_key: "review",
    downstream_path: "支付重构 / 验证 / Review",
    graph_scope: "worker_item",
    upstream_key: "backend",
    upstream_path: "支付重构 / API / Backend",
  }],
  transition_preview: [
    {
      target_worker_item_key: "backend",
      worker_item_id: "a0000000-0000-0000-0000-000000000001",
      path: "支付重构 / API / Backend",
      allowed_actions: ["patch", "continue"],
      fallback_action: "patch",
      reason_codes: ["context_changed"],
    },
    {
      target_worker_item_key: "review",
      worker_item_id: "a0000000-0000-0000-0000-000000000002",
      path: "支付重构 / 验证 / Review",
      allowed_actions: ["continue"],
      fallback_action: "continue",
      reason_codes: ["same_context"],
    },
  ],
  accepted_transition_policy: {
    activate_immediately: true,
    budget_level: "standard",
    failure_policy: "block_affected_branch",
    manual_gate: false,
    revision_operation: "patch_existing",
  },
};

function renderReviewComponents() {
  return render(
    <>
      <RevisionDecisionSummary
        rootTitle="支付重构"
        rootDescription="把支付回调改造成可恢复、可观测的执行链。"
        diff={revisionDiff}
      />
      <RevisionImpactPanel diff={revisionDiff} />
      <RevisionHierarchyDiff diff={revisionDiff} />
      <RevisionDagDiff diff={revisionDiff} />
    </>,
  );
}

describe("Revision review", () => {
  it("presents the review in decision order and keeps the DAG lazy", () => {
    renderReviewComponents();

    const summary = screen.getByRole("heading", { name: "需求理解与风险" });
    const impact = screen.getByRole("heading", { name: "影响范围与同步策略" });
    const hierarchy = screen.getByRole("heading", { name: "任务层级变化" });
    const dagToggle = screen.getByRole("button", { name: "查看依赖变化" });

    expect(summary.compareDocumentPosition(impact) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(impact.compareDocumentPosition(hierarchy) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByText("把支付回调改造成可恢复、可观测的执行链。")).toBeVisible();
    expect(screen.getByText("Worker 上下文需要重新同步")).toBeVisible();
    expect(screen.getByText("依赖关系发生变化")).toBeVisible();
    expect(dagToggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByTestId("revision-dag-change-canvas")).not.toBeInTheDocument();
  });

  it("shows generation movement, bounded counts, and accepted runtime fallback policy", () => {
    render(<RevisionImpactPanel diff={revisionDiff} />);

    expect(screen.getByText("Generation 3 → 4")).toBeVisible();
    expect(screen.getByText("受影响分支")).toBeVisible();
    expect(screen.getByText("预计同步")).toBeVisible();
    expect(screen.getByText("预计续接")).toBeVisible();
    expect(screen.getByText("受影响分支阻塞")).toBeVisible();
    expect(screen.queryByText(/Session/)).not.toBeInTheDocument();
  });

  it("labels each hierarchy strategy with a concrete path and keeps technical details collapsed", async () => {
    const user = userEvent.setup();
    render(<RevisionHierarchyDiff diff={revisionDiff} />);

    expect(screen.getByRole("heading", { name: "任务层级变化" })).toBeVisible();
    for (const label of ["保留", "移动", "新增", "派生", "退出", "补丁", "续接"]) {
      expect(screen.getAllByText(label).length).toBeGreaterThan(0);
    }
    expect(screen.getByText("支付重构 / API / Backend")).toBeVisible();
    expect(screen.getByText("支付重构 / 派生检查 / Derived")).toBeVisible();
    expect(screen.getByText("旧职责")).not.toBeVisible();

    const workerRow = screen.getByText("支付重构 / API / Backend").closest("article");
    expect(workerRow).not.toBeNull();
    await user.click(within(workerRow as HTMLElement).getByText("技术详情 (1)", { selector: "summary" }));
    expect(within(workerRow as HTMLElement).getByText("旧职责")).toBeVisible();
    expect(within(workerRow as HTMLElement).getByText("新职责")).toBeVisible();
  });

  it("opens only changed dependency edges when the user requests the DAG", async () => {
    const user = userEvent.setup();
    render(<RevisionDagDiff diff={revisionDiff} />);

    await user.click(screen.getByRole("button", { name: "查看依赖变化" }));

    expect(screen.getByRole("region", { name: "Revision 依赖变化" })).toBeVisible();
    expect(await screen.findByTestId("revision-dag-change-canvas")).toBeVisible();
    expect(screen.getByText("支付重构 / API → 支付重构 / 验证")).toBeVisible();
    expect(screen.getByText("支付重构 / API / Backend → 支付重构 / 验证 / Review")).toBeVisible();
  });

  it("keeps an older proposal read-only after its binding changes", () => {
    const onConfirm = vi.fn();
    const view = render(
      <ProposalActions
        binding={{
          requestVersion: 4,
          proposalRevision: 2,
          proposalContentHash: "b".repeat(64),
          profileId: "profile-1",
          profileVersion: 1,
        }}
        onConfirm={onConfirm}
      />,
    );

    expect(screen.getByText("新计划会立即成为活动目标，Worker 随后异步同步")).toBeVisible();
    view.rerender(
      <ProposalActions
        binding={{
          requestVersion: 5,
          proposalRevision: 3,
          proposalContentHash: "c".repeat(64),
          profileId: "profile-1",
          profileVersion: 1,
        }}
        onConfirm={onConfirm}
      />,
    );

    expect(screen.getByText("方案版本已变化，请重新查看后再确认。")).toBeVisible();
    expect(screen.getByRole("button", { name: "确认并创建任务" })).toBeDisabled();
  });
});
