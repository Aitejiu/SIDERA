import { GitBranch, ListChecks, Network } from "lucide-react";

import type { InputGroupDisplay } from "./proposalTypes";

export function ProposalInputGroups({
  groups,
  heading = "结果输入规则",
  ariaLabel = "结果输入规则",
  showEmpty = false,
  emptyMessage = "当前没有声明输入组。",
}: {
  groups: InputGroupDisplay[];
  heading?: string;
  ariaLabel?: string;
  showEmpty?: boolean;
  emptyMessage?: string;
}) {
  if (groups.length === 0 && !showEmpty) return null;

  return (
    <section className="planning-input-groups" aria-label={ariaLabel}>
      <div className="planning-input-groups__heading">
        <div>
          <span className="eyebrow"><ListChecks size={13} aria-hidden="true" />INPUT CONTRACT</span>
          <h2>{heading}</h2>
        </div>
        <span className="mono-label">{groups.length} 个输入组</span>
      </div>
      {groups.length > 0 ? (
        <div className="planning-input-groups__list">
          {groups.map((group) => <InputGroupCard group={group} key={group.groupKey} />)}
        </div>
      ) : <p className="planning-input-groups__empty">{emptyMessage}</p>}
    </section>
  );
}

function InputGroupCard({ group }: { group: InputGroupDisplay }) {
  return (
    <article className="planning-input-group">
      <header className="planning-input-group__header">
        <div>
          <span className="mono-label">GROUP · {group.groupKey}</span>
          <h3>{group.title}</h3>
          <p>消费者子任务：{group.consumerSubtaskPath.join(" / ")} · {group.consumerWorkerItemKey}</p>
        </div>
        <strong className="planning-input-group__threshold">{thresholdLabel(group)}</strong>
      </header>

      <div className="planning-input-group__body">
        <div className="planning-input-group__sources">
          <div className="planning-input-group__subheading">
            <span>来源优先级</span>
            <span className="planning-input-group__strategy">{strategyLabel(group.selectionStrategy)}</span>
          </div>
          <ol>
            {group.candidates.map((candidate) => (
              <li key={candidate.workerItemKey}>
                <span className="planning-input-group__priority">{candidate.priority}</span>
                <span className="planning-input-group__source-copy">
                  <strong>{candidate.workerItemTitle}</strong>
                  <span>{candidate.subtaskPath.join(" / ")} · {candidate.workerItemKey}</span>
                </span>
              </li>
            ))}
          </ol>
        </div>

        <div className="planning-input-group__requirements">
          <RequirementList label="必需 Artifact" items={group.requiredArtifactRefs} />
          <RequirementList label="必需 Evidence" items={group.requiredEvidenceRefs} />
        </div>
      </div>

      <div className="planning-input-group__dependencies">
        <div className="planning-input-group__subheading">
          <span><GitBranch size={13} aria-hidden="true" />独立硬依赖</span>
          {group.hardDependencies.length > 0 ? <span>{group.hardDependencies.length} 条</span> : null}
        </div>
        {group.hardDependencies.length > 0 ? (
          <ul>
            {group.hardDependencies.map((dependency) => (
              <li key={dependency.upstreamWorkerItemKey}>
                <Network size={13} aria-hidden="true" />
                <span>
                  <strong>{dependency.upstreamWorkerItemTitle}</strong>
                  <span>{dependency.upstreamWorkerItemKey}</span>
                  <RequirementRefs dependency={dependency} />
                </span>
              </li>
            ))}
          </ul>
        ) : <p>未声明独立硬依赖</p>}
      </div>
    </article>
  );
}

function RequirementList({ label, items }: { label: string; items: string[] }) {
  return (
    <div className="planning-input-group__requirement">
      <span>{label}</span>
      {items.length > 0 ? (
        <div className="planning-input-group__ref-list">
          {items.map((item) => <code key={item}>{item}</code>)}
        </div>
      ) : <p>未声明</p>}
    </div>
  );
}

function RequirementRefs({ dependency }: { dependency: InputGroupDisplay["hardDependencies"][number] }) {
  const refs = [...dependency.requiredArtifactRefs, ...dependency.requiredEvidenceRefs];
  return refs.length > 0 ? (
    <span className="planning-input-group__dependency-refs">{refs.join(" · ")}</span>
  ) : null;
}

function thresholdLabel(group: InputGroupDisplay): string {
  return group.mode === "all"
    ? `全部 ${group.candidates.length} / ${group.candidates.length}`
    : `至少 ${group.requiredCount} / ${group.candidates.length}`;
}

function strategyLabel(strategy: string): string {
  return strategy === "plan_order" ? "按计划顺序" : strategy.replaceAll("_", " ");
}
