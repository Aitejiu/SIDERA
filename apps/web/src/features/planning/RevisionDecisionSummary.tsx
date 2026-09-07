import { AlertTriangle, ArrowUpRight } from "lucide-react";

import type { RevisionDiff } from "./types";
import { ProposalInputGroups } from "./ProposalInputGroups";
import { normalizeRevisionInputGroups } from "./proposalTypes";

const riskLabels: Record<string, string> = {
  worker_context_patch: "Worker 上下文需要重新同步",
  dependency_change: "依赖关系发生变化",
  unknown_outcome: "存在外部执行结果未知的分支",
  active_branch_impact: "活动执行分支会受到影响",
  integration_spec_changed: "成果集成合同发生变化",
};

export function RevisionDecisionSummary({
  rootTitle,
  rootDescription,
  diff,
}: {
  rootTitle: string;
  rootDescription?: string | undefined;
  diff: RevisionDiff;
}) {
  const understanding = rootDescription?.trim() || `本次调整作用于 ${rootTitle} 的活动执行结构。`;
  const targetGeneration = diff.target_descriptor_preview.target_generation;
  const inputGroups = normalizeRevisionInputGroups(
    diff.input_group_changes,
    diff.worker_item_changes,
    diff.worker_dependency_changes,
    rootTitle,
  );

  return (
    <section className="revision-review__decision" aria-labelledby="revision-decision-title">
      <div className="revision-review__section-heading">
        <div>
          <span className="eyebrow">DECISION SUMMARY</span>
          <h2 id="revision-decision-title">需求理解与风险</h2>
        </div>
        <span className="mono-label">Revision V{diff.proposal_revision}</span>
      </div>
      <div className="revision-review__understanding">
        <span className="revision-review__label">需求理解</span>
        <p>{understanding}</p>
        <p className="revision-review__understanding-meta">
          <ArrowUpRight size={14} aria-hidden="true" />
          目标是将活动计划推进到 Generation {targetGeneration}，不改变未受影响的执行分支。
        </p>
      </div>
      <div className="revision-review__decision-grid">
        <div>
          <span className="revision-review__label">受影响范围</span>
          {diff.affected_branch_paths.length > 0 ? (
            <ul>
              {diff.affected_branch_paths.map((path) => <li key={path}>{path}</li>)}
            </ul>
          ) : <p>没有标记受影响分支。</p>}
        </div>
        <div className="revision-review__risk-list">
          <span className="revision-review__label">风险</span>
          {diff.risk_flags.length > 0 ? (
            <ul>
              {diff.risk_flags.map((flag) => (
                <li key={flag}>
                  <AlertTriangle size={13} aria-hidden="true" />
                  {riskLabels[flag] ?? flag}
                </li>
              ))}
            </ul>
          ) : <p>规划器未标记额外风险。</p>}
        </div>
      </div>
      {inputGroups.before.length > 0 || inputGroups.after.length > 0 ? (
        <div className="revision-review__input-groups">
          <div className="revision-review__input-groups-heading">
            <span className="revision-review__label">输入规则变更</span>
            <p>以下 before / after 来自本次 Revision，确认时沿用当前 Proposal 版本。</p>
          </div>
          <ProposalInputGroups
            groups={inputGroups.before}
            heading="变更前输入规则"
            ariaLabel="变更前结果输入规则"
            showEmpty
            emptyMessage="变更前没有声明输入组。"
          />
          <ProposalInputGroups
            groups={inputGroups.after}
            heading="变更后输入规则"
            ariaLabel="变更后结果输入规则"
            showEmpty
            emptyMessage="变更后没有声明输入组。"
          />
        </div>
      ) : null}
    </section>
  );
}
