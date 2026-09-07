import {
  ArrowRightLeft,
  CheckCircle2,
  CircleDot,
  FileCheck2,
  GitBranch,
  MessageSquareText,
  PackageCheck,
  RefreshCcw,
  SearchCode,
  ShieldAlert,
  Timer,
  TriangleAlert,
} from "lucide-react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { useEffect, useRef, useState } from "react";

import type { WorkerActivity } from "./types";

const ACTIVITY_ESTIMATED_SIZE = 104;
const BOUNDARY_ESTIMATED_SIZE = 76;

export function WorkerTimeline({
  activities,
  hasMore,
  isLoadingMore,
  onLoadMore,
  onOpenTrace,
}: {
  activities: readonly WorkerActivity[];
  hasMore: boolean;
  isLoadingMore: boolean;
  onLoadMore: () => void;
  onOpenTrace: (activity: WorkerActivity) => void;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const previousCountRef = useRef(activities.length);
  const [followTail, setFollowTail] = useState(true);
  const [hasNewActivity, setHasNewActivity] = useState(false);
  // TanStack Virtual exposes an intentionally stateful virtualizer instance.
  // eslint-disable-next-line react-hooks/incompatible-library
  const virtualizer = useVirtualizer({
    count: activities.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: (index) => isBoundary(activities[index]?.kind)
      ? BOUNDARY_ESTIMATED_SIZE
      : ACTIVITY_ESTIMATED_SIZE,
    overscan: 12,
  });
  const virtualItems = virtualizer.getVirtualItems();

  useEffect(() => {
    const previousCount = previousCountRef.current;
    if (activities.length > previousCount) {
      if (followTail) {
        virtualizer.scrollToIndex(activities.length - 1, { align: "end" });
      } else {
        setHasNewActivity(true);
      }
    }
    previousCountRef.current = activities.length;
  }, [activities.length, followTail, virtualizer]);

  const followLatest = () => {
    setFollowTail(true);
    setHasNewActivity(false);
    if (activities.length > 0) {
      virtualizer.scrollToIndex(activities.length - 1, { align: "end" });
    }
  };

  return (
    <section className="worker-timeline" aria-labelledby="worker-timeline-title">
      <div className="section-heading worker-timeline__heading">
        <div>
          <span className="eyebrow">WORKERITEM ACTIVITY</span>
          <h2 id="worker-timeline-title">持续工作记录</h2>
        </div>
        <div className="worker-timeline__actions">
          {hasMore ? (
            <button
              className="quiet-button"
              type="button"
              onClick={onLoadMore}
              disabled={isLoadingMore}
            >
              <Timer size={14} aria-hidden="true" />
              {isLoadingMore ? "读取中" : "加载更多活动"}
            </button>
          ) : null}
          {hasNewActivity ? (
            <button className="quiet-button" type="button" onClick={followLatest}>
              <CircleDot size={14} aria-hidden="true" />有新活动
            </button>
          ) : null}
        </div>
      </div>
      {activities.length === 0 ? (
        <div className="worker-timeline__empty" role="status">
          <MessageSquareText size={17} aria-hidden="true" />
          <strong>这个 WorkerItem 还没有活动</strong>
          <span>等待第一个 Agent 或 Session 记录</span>
        </div>
      ) : (
        <div
          className="worker-timeline__scroll"
          ref={scrollRef}
          tabIndex={0}
          role="region"
          aria-label="WorkerItem 活动时间线"
          onScroll={(event) => {
            const element = event.currentTarget;
            const atTail = element.scrollTop + element.clientHeight >= element.scrollHeight - 32;
            setFollowTail(atTail);
            if (atTail) {
              setHasNewActivity(false);
            }
          }}
        >
          <div
            className="worker-timeline__spacer"
            style={{ height: virtualizer.getTotalSize() || activities.length * ACTIVITY_ESTIMATED_SIZE }}
          >
            {(virtualItems.length > 0
              ? virtualItems
              : activities.slice(0, 120).map((_, index) => ({
                index,
                start: index * ACTIVITY_ESTIMATED_SIZE,
                size: ACTIVITY_ESTIMATED_SIZE,
              }))
            ).map((item) => {
              const activity = activities[item.index];
              if (activity === undefined) return null;
              return (
                <WorkerActivityRow
                  key={activity.activity_id}
                  activity={activity}
                  onOpenTrace={onOpenTrace}
                  style={{ transform: `translateY(${item.start}px)` }}
                />
              );
            })}
          </div>
        </div>
      )}
    </section>
  );
}

function WorkerActivityRow({
  activity,
  onOpenTrace,
  style,
}: {
  activity: WorkerActivity;
  onOpenTrace: (activity: WorkerActivity) => void;
  style: React.CSSProperties;
}) {
  const boundary = isBoundary(activity.kind);
  const variant = activityVariant(activity.kind);
  return (
    <article className={`worker-timeline-item worker-timeline-item--${variant}${boundary ? " worker-timeline-item--boundary" : ""}`} style={style}>
      <div className="worker-timeline-item__marker" aria-hidden="true">{activityIcon(activity.kind)}</div>
      <div className="worker-timeline-item__body">
        <div className="worker-timeline-item__topline">
          <span className="eyebrow">{activityLabel(activity.kind)}</span>
          <time dateTime={activity.occurred_at}>{formatTime(activity.occurred_at)}</time>
          {activity.execution_id ? <span className="mono-label">EXEC {shortId(activity.execution_id)}</span> : null}
          {activity.trace_descriptor ? (
            <button
              className="worker-timeline-item__trace"
              type="button"
              aria-label="查看 Agent Trace"
              title="查看 Agent Trace"
              onClick={() => onOpenTrace(activity)}
            >
              <SearchCode size={14} aria-hidden="true" />
            </button>
          ) : null}
        </div>
        <strong>{activity.title}</strong>
        {activity.summary !== activity.title ? <p>{activity.summary}</p> : null}
        {activity.body !== null && activity.body !== activity.summary ? (
          <blockquote>{activity.body}</blockquote>
        ) : null}
        {activity.delivery_status ? (
          <span className="worker-timeline-item__delivery">{deliveryLabel(activity.delivery_status)}</span>
        ) : null}
        {activity.artifact_ids.length > 0 ? (
          <span className="worker-timeline-item__attachment">
            <PackageCheck size={13} aria-hidden="true" />{activity.artifact_ids.length} 个关联产物
          </span>
        ) : null}
      </div>
    </article>
  );
}

function isBoundary(kind: string | undefined): boolean {
  return kind?.startsWith("session.") === true
    || kind?.startsWith("execution.") === true
    || kind === "plan.generation";
}

function activityVariant(kind: string): string {
  if (kind === "user_input") return "intervention";
  if (kind === "checkpoint") return "checkpoint";
  if (kind === "artifact" || kind === "evidence") return "delivery";
  if (kind.startsWith("handoff.")) return "handoff";
  if (kind === "error" || kind.startsWith("error.")) return "error";
  if (kind === "plan.recovery") return "error";
  if (kind.startsWith("plan.")) return "plan";
  if (isBoundary(kind)) return "boundary";
  return "step";
}

function activityLabel(kind: string): string {
  if (kind === "user_input") return "USER INTERVENTION";
  if (kind === "checkpoint") return "CHECKPOINT";
  if (kind === "artifact" || kind === "evidence") return "DELIVERY";
  if (kind.startsWith("handoff.")) return "WORKER HANDOFF";
  if (kind === "error" || kind.startsWith("error.")) return "LIFECYCLE / ERROR";
  if (kind === "plan.generation") return "PLAN GENERATION";
  if (kind === "plan.patch") return "CONTEXT PATCH";
  if (kind === "plan.continuation") return "CONTINUATION";
  if (kind === "plan.recovery") return "RECOVERY";
  if (isBoundary(kind)) return "SESSION / EXECUTION";
  return "WORKER STEP";
}

function activityIcon(kind: string) {
  if (kind === "user_input") return <MessageSquareText size={16} />;
  if (kind === "checkpoint") return <CheckCircle2 size={16} />;
  if (kind === "artifact") return <PackageCheck size={16} />;
  if (kind === "evidence") return <FileCheck2 size={16} />;
  if (kind.startsWith("handoff.")) return <ArrowRightLeft size={16} />;
  if (kind === "error" || kind.startsWith("error.")) return <TriangleAlert size={16} />;
  if (kind === "plan.generation") return <GitBranch size={16} />;
  if (kind === "plan.patch") return <RefreshCcw size={16} />;
  if (kind === "plan.continuation") return <GitBranch size={16} />;
  if (kind === "plan.recovery") return <TriangleAlert size={16} />;
  if (isBoundary(kind)) return <CircleDot size={16} />;
  return <ShieldAlert size={16} />;
}

function deliveryLabel(status: string): string {
  const labels: Record<string, string> = {
    queued: "已排队",
    delivered: "已投递",
    blocked: "已阻塞",
    canceled: "已取消",
    rejected: "已拒绝",
  };
  return labels[status] ?? status;
}

function formatTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleString("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
}

function shortId(value: string): string {
  return value.slice(0, 8);
}
