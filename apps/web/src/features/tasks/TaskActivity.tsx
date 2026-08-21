import { ArrowDown, Clock3 } from "lucide-react";

import type { TaskActivity as TaskActivityProjection } from "./types";

export function TaskActivity({
  activity,
  onLoadMore,
  isLoadingMore = false,
  isLoading = false,
  isError = false,
  onRetry,
}: {
  activity: TaskActivityProjection | undefined;
  onLoadMore?: () => void;
  isLoadingMore?: boolean;
  isLoading?: boolean;
  isError?: boolean;
  onRetry?: () => void;
}) {
  if (activity === undefined && isError) {
    return (
      <section className="activity-panel activity-panel--error" aria-labelledby="activity-title">
        <div className="section-heading">
          <div>
            <span className="eyebrow">TASK ACTIVITY</span>
            <h2 id="activity-title">动态</h2>
          </div>
        </div>
        <div className="activity-error" role="alert">
          <span>无法读取任务动态。</span>
          <button className="quiet-button" type="button" onClick={onRetry}>重试读取动态</button>
        </div>
      </section>
    );
  }
  if (activity === undefined || isLoading) {
    return <div className="activity-panel" role="status">正在读取任务动态</div>;
  }
  return (
    <section className="activity-panel" aria-labelledby="activity-title">
      <div className="section-heading">
        <div>
          <span className="eyebrow">TASK ACTIVITY</span>
          <h2 id="activity-title">动态</h2>
        </div>
        <span className="mono-label">CURSOR {activity.last_change_cursor}</span>
      </div>
      <div className="activity-list">
        {activity.items.map((item) => (
          <div className="activity-item" key={`${item.source_type}:${item.source_id}:${item.occurred_at}`}>
            <Clock3 size={14} aria-hidden="true" />
            <div>
              <strong>{item.title}</strong>
              <span>{item.summary}</span>
            </div>
            <span className="mono-label">{formatTime(item.occurred_at)}</span>
          </div>
        ))}
      </div>
      {activity.next_cursor !== null ? (
        <button className="load-older-button" type="button" onClick={onLoadMore} disabled={isLoadingMore}>
          <ArrowDown size={14} aria-hidden="true" />
          {isLoadingMore ? "正在加载" : "加载更多活动"}
        </button>
      ) : null}
    </section>
  );
}

function formatTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}
