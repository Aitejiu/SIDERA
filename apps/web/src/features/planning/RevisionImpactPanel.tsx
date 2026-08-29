import { GitBranch, ShieldAlert } from "lucide-react";

import type { RevisionDiff } from "./types";

const impactKeys = [
  "preserve",
  "patch",
  "continue",
  "create",
  "derive",
  "move",
  "retire",
] as const;

const impactLabels: Record<(typeof impactKeys)[number], string> = {
  preserve: "保留",
  patch: "补丁",
  continue: "续接",
  create: "新增",
  derive: "派生",
  move: "移动",
  retire: "退出",
};

export function RevisionImpactPanel({ diff }: { diff: RevisionDiff }) {
  const fromGeneration = diff.base.plan_generation;
  const targetGeneration = diff.target_descriptor_preview.target_generation;
  return (
    <section className="revision-review__impact" aria-labelledby="revision-impact-title">
      <div className="revision-review__section-heading">
        <div>
          <span className="eyebrow">IMPACT &amp; CONVERGENCE</span>
          <h2 id="revision-impact-title">影响范围与同步策略</h2>
        </div>
        <strong className="revision-review__generation">Generation {fromGeneration} → {targetGeneration}</strong>
      </div>
      <div className="revision-review__impact-grid" aria-label="Revision 影响计数">
        {impactKeys.map((key) => (
          <div key={key}>
            <span>{impactLabels[key]}</span>
            <strong>{diff.impact_counts[key]}</strong>
          </div>
        ))}
        <div className="revision-review__impact-grid--branches">
          <span>受影响分支</span>
          <strong>{diff.impact_counts.affected_branches}</strong>
        </div>
      </div>
      <div className="revision-review__policy-line">
        <span><GitBranch size={13} aria-hidden="true" />{policyLabel(diff.accepted_transition_policy.revision_operation)}</span>
        <span><ShieldAlert size={13} aria-hidden="true" />{failurePolicyLabel(diff.accepted_transition_policy.failure_policy)}</span>
        <span>{diff.accepted_transition_policy.activate_immediately ? "确认后立即成为活动计划" : "确认后等待调度"}</span>
      </div>
      <div className="revision-review__runtime">
        <div className="revision-review__subheading">
          <div>
            <span className="eyebrow">RUNTIME PREVIEW</span>
            <h3>运行同步预览</h3>
          </div>
          <span className="mono-label">只显示策略预期</span>
        </div>
        {diff.transition_preview.length > 0 ? (
          <div className="revision-review__runtime-list">
            {diff.transition_preview.map((item) => (
              <article key={`${item.target_worker_item_key}:${item.path}`}>
                <div className="revision-review__runtime-title">
                  <strong>{item.path}</strong>
                  <span>{transitionLabel(item.fallback_action, item.allowed_actions)}</span>
                </div>
                <p>{item.reason_codes.length > 0 ? `原因：${item.reason_codes.join("、")}` : "控制面将按已接受策略调度。"}</p>
              </article>
            ))}
          </div>
        ) : <p className="revision-review__empty">没有提供 WorkerItem 运行预览。</p>}
      </div>
    </section>
  );
}

function transitionLabel(fallbackAction: string | null, allowedActions: string[]): string {
  const action = fallbackAction ?? allowedActions[0] ?? "";
  switch (action) {
    case "patch":
      return "预计同步";
    case "continue":
      return "预计续接";
    case "create":
      return "预计创建";
    case "retire":
      return "预计退出";
    case "move":
      return "预计移动";
    default:
      return action || "等待策略";
  }
}

function policyLabel(operation: string | null): string {
  switch (operation) {
    case "patch_existing":
      return "优先复用已有 WorkerItem";
    case "continue_existing":
      return "优先续接已有 WorkerItem";
    default:
      return operation ?? "沿用默认收敛策略";
  }
}

function failurePolicyLabel(policy: string): string {
  switch (policy) {
    case "block_affected_branch":
      return "受影响分支阻塞";
    case "continue_independent_branches":
      return "独立分支继续";
    default:
      return policy;
  }
}
