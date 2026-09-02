import { AlertTriangle, ArrowRight, CircleCheck, CircleDashed, LoaderCircle } from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import { StatusMark, type StatusMarkKind } from "@/shared/ui/StatusMark";

import type { CollaborationStatus, RootCollaborationSummary } from "./types";

export function CollaborationSummary({
  summary,
  onOpenSubtask,
}: {
  summary: RootCollaborationSummary | null | undefined;
  onOpenSubtask: (subtaskKey: string) => void;
}) {
  if (summary === null || summary === undefined) {
    return null;
  }

  const status = collaborationStatus(summary.status);
  const blockedSubtask = summary.blocked_subtasks[0];
  return (
    <section
      className={`collaboration-summary collaboration-summary--${summary.status}`}
      aria-labelledby="collaboration-summary-title"
    >
      <header className="collaboration-summary__header">
        <div>
          <span className="eyebrow">COLLABORATION · GEN {summary.plan_generation}</span>
          <h2 id="collaboration-summary-title">协同调度</h2>
        </div>
        <StatusMark kind={status.kind} label={status.label} />
      </header>

      <div className="collaboration-summary__counts" aria-label="协同进度">
        <SummaryCount icon={<CircleCheck size={15} aria-hidden="true" />} label="已放行" value={summary.released_count} />
        <SummaryCount icon={<LoaderCircle size={15} aria-hidden="true" />} label="执行中" value={summary.running_count} />
        <SummaryCount icon={<CircleDashed size={15} aria-hidden="true" />} label="等待中" value={summary.waiting_count} />
        <SummaryCount icon={<CircleCheck size={15} aria-hidden="true" />} label="已完成" value={summary.completed_count} />
      </div>

      {summary.blocked_subtasks.length > 0 ? (
        <div className="collaboration-summary__attention" aria-label="协同卡点">
          <div className="collaboration-summary__attention-heading">
            <span className="eyebrow"><AlertTriangle size={13} aria-hidden="true" />需要关注</span>
            <span className="mono-label">{summary.blocked_subtask_count} 个子任务</span>
          </div>
          <div className="collaboration-summary__blocked-list">
            {summary.blocked_subtasks.map((item) => (
              <button
                className="collaboration-summary__blocked"
                key={item.subtask_key}
                type="button"
                onClick={() => onOpenSubtask(item.subtask_key)}
                aria-label={`打开子任务：${item.title}`}
              >
                <span>
                  <strong>{item.title}</strong>
                  <span>{item.reason}</span>
                </span>
                <ArrowRight size={15} aria-hidden="true" />
              </button>
            ))}
          </div>
        </div>
      ) : (
        <p className="collaboration-summary__latest">
          {summary.latest_decision_summary ?? "当前没有需要处理的协同卡点。"}
        </p>
      )}

      <div className="collaboration-summary__footer">
        <span className="mono-label">策略 · {formatStrategy(summary.strategy_label)}</span>
        <Link className="quiet-button" to={summary.detail_href}>
          查看协同记录 <ArrowRight size={14} aria-hidden="true" />
        </Link>
      </div>
      {blockedSubtask === undefined && summary.attention_required ? (
        <span className="collaboration-summary__status-note">协同运行需要进一步处理</span>
      ) : null}
    </section>
  );
}

function SummaryCount({
  icon,
  label,
  value,
}: {
  icon: ReactNode;
  label: string;
  value: number;
}) {
  return (
    <div className="collaboration-summary__count" aria-label={`${label} ${value}`}>
      <span>{icon}{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

export function collaborationStatus(status: CollaborationStatus): {
  kind: StatusMarkKind;
  label: string;
} {
  switch (status) {
    case "running":
      return { kind: "running", label: "协同运行中" };
    case "waiting":
      return { kind: "planned", label: "等待协同" };
    case "attention":
      return { kind: "stale", label: "需要关注" };
    case "completed":
      return { kind: "completed", label: "协同已完成" };
  }
}

export function formatStrategy(strategy: string): string {
  if (strategy === "all_required") {
    return "全部就绪后放行";
  }
  return strategy.replaceAll("_", " ");
}
