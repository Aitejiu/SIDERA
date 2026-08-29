import { AlertTriangle, ArrowRight, Check, CirclePlay, Clock3, GitBranch, Layers3 } from "lucide-react";
import { Link } from "react-router-dom";

import type { TaskOverview } from "./api";

export function TaskOverviewStrip({
  workspaceId,
  overview,
}: {
  workspaceId: string;
  overview: TaskOverview;
}) {
  return (
    <Link
      className="task-row"
      to={`/workspaces/${workspaceId}/tasks/${overview.root_task_id}`}
      aria-label={`打开任务 ${overview.title}`}
    >
      <div className="task-row__identity">
        <span className="task-row__title">{overview.title}</span>
        <span className="task-row__summary">{overview.goal_summary}</span>
        <span className="mono-label">{overview.root_task_id.slice(0, 8)} · {formatFreshness(overview.as_of)}</span>
      </div>
      <div className="task-row__metric">
        <span className="eyebrow">计划</span>
        <strong>GEN {overview.generation}</strong>
      </div>
      <div className="task-row__metric task-row__metric--wide">
        <span className="eyebrow">子任务</span>
        <strong><Layers3 size={14} aria-hidden="true" />{overview.subtask_count} / {overview.planned_subtask_count} 已规划</strong>
      </div>
      <div className="task-row__metric">
        <span className="eyebrow">调度</span>
        <strong><CirclePlay size={14} aria-hidden="true" />运行中 {overview.running_subtask_count}</strong>
        <span className={overview.failed_subtask_count > 0 ? "metric-warning" : "metric-muted"}>
          {overview.failed_subtask_count > 0 ? <AlertTriangle size={13} aria-hidden="true" /> : null}
          失败 {overview.failed_subtask_count}
        </span>
      </div>
      <div className="task-row__metric task-row__metric--next">
        <span className="eyebrow">下一节点</span>
        <strong>下一个 {overview.next_subtask_keys[0] ?? "等待规划"}</strong>
        {overview.current_milestone_key ? <span>里程碑 {overview.current_milestone_key}</span> : <span>尚未进入里程碑</span>}
      </div>
      <div className="task-row__attention">
        <span className="eyebrow">需要我</span>
        {overview.attention_items[0] ? (
          <>
            <strong className="attention-value"><AlertTriangle size={14} aria-hidden="true" />{overview.title} / {overview.attention_items[0].subtask_title}</strong>
            <span className="task-row__attention-label">
              {isSchedulingWait(overview.attention_items[0]) ? "调度等待" : "需要处理"}
            </span>
            <span>
              {overview.attention_items[0].waiting_details_summary
                || overview.attention_items[0].reason}
            </span>
            {overview.attention_items.length > 1 ? <span>另有 {overview.attention_items.length - 1} 项待处理</span> : null}
          </>
        ) : overview.revision_attention ? (
          <>
            <strong className="attention-value">
              <GitBranch size={14} aria-hidden="true" />
              {overview.title} / {overview.revision_attention.subtask_path.join(" / ") || "计划调整"}
            </strong>
            <span className="task-row__attention-label">计划调整</span>
            <span>{overview.revision_attention.summary}</span>
          </>
        ) : <strong className="metric-muted"><Check size={14} aria-hidden="true" />当前没有卡点</strong>}
        <ArrowRight className="task-row__arrow" size={17} aria-hidden="true" />
      </div>
    </Link>
  );
}

function isSchedulingWait(item: TaskOverview["attention_items"][number]): boolean {
  return Boolean(item.waiting_details_summary) || item.key.startsWith("scheduling_block:");
}


function formatFreshness(value: string): string {
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) {
    return "时间未知";
  }
  const ageSeconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000));
  if (ageSeconds < 60) {
    return "刚刚更新";
  }
  if (ageSeconds < 3600) {
    return `${Math.floor(ageSeconds / 60)} 分钟前更新`;
  }
  return `更新于 ${new Date(timestamp).toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  })}`;
}

export function FreshnessLabel({ value }: { value: string }) {
  return (
    <span className="freshness-label">
      <Clock3 size={13} aria-hidden="true" />
      {formatFreshness(value)}
    </span>
  );
}
