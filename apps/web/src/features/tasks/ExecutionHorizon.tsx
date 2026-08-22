import type { TaskGraph } from "./types";
import { StatusMark } from "@/shared/ui/StatusMark";

export function ExecutionHorizon({ graph }: { graph: TaskGraph }) {
  const { overview } = graph;
  const currentMilestone = graph.milestones.find(
    (milestone) => milestone.key === overview.current_milestone_key,
  );
  const nextSubtasks = overview.next_subtask_keys
    .map((key) => graph.nodes.find((node) => node.subtask_key === key)?.title ?? key)
    .slice(0, 3);
  const overallStatus = overview.failed_subtask_count > 0
    ? "failed"
    : overview.attention_items.length > 0
      ? "stale"
      : overview.completed_subtask_count === overview.subtask_count && overview.subtask_count > 0
        ? "completed"
        : overview.running_subtask_count > 0
          ? "running"
          : "planned";

  return (
    <section className="execution-horizon" aria-labelledby="horizon-title">
      <div className="section-heading">
        <div>
          <span className="eyebrow">DELIVERY SUMMARY</span>
          <h2 id="horizon-title">推进摘要</h2>
        </div>
        <StatusMark kind={overallStatus} {...(overallStatus === "stale" ? { label: "有待处理" } : {})} />
      </div>
      <div className="delivery-summary-grid">
        <div className="delivery-summary-stat">
          <span className="eyebrow">完成</span>
          <strong>{overview.completed_subtask_count} / {overview.subtask_count}</strong>
          <span>个子任务</span>
        </div>
        <div className="delivery-summary-stat">
          <span className="eyebrow">执行中</span>
          <strong>{overview.running_subtask_count}</strong>
          <span>个子任务</span>
        </div>
        <div className="delivery-summary-stat delivery-summary-stat--attention">
          <span className="eyebrow">待处理</span>
          <strong>{overview.attention_items.length + overview.failed_subtask_count}</strong>
          <span>个风险信号</span>
        </div>
      </div>
      <div className="delivery-summary-context">
        <div>
          <span className="eyebrow">当前里程碑</span>
          <strong>{currentMilestone?.title ?? "尚未开始"}</strong>
        </div>
        <div>
          <span className="eyebrow">下一步</span>
          <strong>{nextSubtasks.length > 0 ? nextSubtasks.join("、") : "等待规划"}</strong>
        </div>
      </div>
    </section>
  );
}
