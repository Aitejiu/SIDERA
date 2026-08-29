import { ChevronDown, ChevronRight } from "lucide-react";
import { useState } from "react";

import type { RevisionDiff } from "./types";

type FieldChange = RevisionDiff["root_changes"][number];
type HierarchyChange = {
  path: string;
  strategy: string;
  reason: string;
  fieldChanges: FieldChange[];
};

export function RevisionHierarchyDiff({ diff }: { diff: RevisionDiff }) {
  return (
    <section className="revision-review__hierarchy" aria-labelledby="revision-hierarchy-title">
      <div className="revision-review__section-heading">
        <div>
          <span className="eyebrow">TASK / SUBTASK / WORKERITEM</span>
          <h2 id="revision-hierarchy-title">任务层级变化</h2>
        </div>
        <span className="mono-label">按路径审阅</span>
      </div>
      <ChangeGroup
        eyebrow="TASK"
        title="根任务"
        changes={diff.root_changes.map((change) => ({
          path: `根任务 / ${change.field_path}`,
          strategy: "modify",
          reason: "根任务约束发生变化",
          fieldChanges: [change],
        }))}
      />
      <ChangeGroup
        eyebrow="SUBTASK"
        title="子任务"
        changes={diff.subtask_changes.map((change) => ({
          path: change.path,
          strategy: change.strategy,
          reason: change.reason,
          fieldChanges: change.field_changes,
        }))}
      />
      <ChangeGroup
        eyebrow="WORKERITEM"
        title="WorkerItem"
        changes={diff.worker_item_changes.map((change) => ({
          path: change.path,
          strategy: change.strategy,
          reason: change.reasons[0] ?? change.context_disposition,
          fieldChanges: change.field_changes,
        }))}
      />
    </section>
  );
}

function ChangeGroup({
  eyebrow,
  title,
  changes,
}: {
  eyebrow: string;
  title: string;
  changes: HierarchyChange[];
}) {
  return (
    <section className="revision-review__change-group" aria-labelledby={`revision-change-group-${eyebrow.toLowerCase()}`}>
      <div className="revision-review__subheading">
        <div>
          <span className="eyebrow">{eyebrow}</span>
          <h3 id={`revision-change-group-${eyebrow.toLowerCase()}`}>{title}</h3>
        </div>
        <span className="mono-label">{changes.length} 项</span>
      </div>
      {changes.length > 0 ? (
        <div className="revision-review__change-list">
          {changes.map((change, index) => <ChangeRow key={`${change.path}:${change.strategy}:${index}`} change={change} />)}
        </div>
      ) : <p className="revision-review__empty">没有该层级的结构变化。</p>}
    </section>
  );
}

function ChangeRow({ change }: { change: HierarchyChange }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <article className={`revision-review__change-row revision-review__change-row--${change.strategy}`}>
      <div className="revision-review__change-main">
        <div>
          <strong>{change.path}</strong>
          <span className="revision-review__strategy" data-strategy={change.strategy}>
            {strategyLabel(change.strategy)}
          </span>
        </div>
        <p>{change.reason}</p>
      </div>
      {change.fieldChanges.length > 0 ? (
        <details open={expanded} onToggle={(event) => setExpanded(event.currentTarget.open)}>
          <summary>
            {expanded ? <ChevronDown size={13} aria-hidden="true" /> : <ChevronRight size={13} aria-hidden="true" />}
            技术详情 ({change.fieldChanges.length})
          </summary>
          <dl className="revision-review__field-list">
            {change.fieldChanges.map((field) => (
              <div key={field.field_path}>
                <dt>{field.field_path}</dt>
                <dd><span>前</span>{formatValue(field.before)}</dd>
                <dd><span>后</span>{formatValue(field.after)}</dd>
              </div>
            ))}
          </dl>
        </details>
      ) : null}
    </article>
  );
}

function strategyLabel(strategy: string): string {
  switch (strategy) {
    case "modify":
      return "修改";
    case "preserve":
      return "保留";
    case "patch":
      return "补丁";
    case "continue":
      return "续接";
    case "create":
      return "新增";
    case "derive":
      return "派生";
    case "move":
      return "移动";
    case "retire":
      return "退出";
    default:
      return strategy;
  }
}

function formatValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === null) return "空";
  if (value === undefined) return "未提供";
  try {
    const serialized = JSON.stringify(value);
    return serialized.length > 180 ? `${serialized.slice(0, 177)}...` : serialized;
  } catch {
    return "不可展示";
  }
}
