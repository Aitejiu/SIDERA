import type { Milestone } from "./types";

export function MilestoneTrack({ milestones }: { milestones: readonly Milestone[] }) {
  return (
    <section className="milestone-track" aria-labelledby="milestone-title">
      <div className="section-heading">
        <div>
          <span className="eyebrow">MILESTONE TRACK</span>
          <h2 id="milestone-title">里程碑</h2>
        </div>
        <span className="mono-label">{milestones.length} 个节点组</span>
      </div>
      <div className="milestone-list">
        {milestones.map((milestone) => (
          <div className={`milestone-item milestone-item--${milestone.state}`} key={milestone.key}>
            <span className="milestone-item__index">{milestone.order_hint}</span>
            <div>
              <strong>{milestone.title}</strong>
              <span>{milestone.subtask_count} 个子任务 · {milestone.completed_count} 已完成 · {milestone.planned_count} 计划中</span>
            </div>
            <span className="mono-label">{milestone.state}</span>
          </div>
        ))}
      </div>
    </section>
  );
}
