import {
  ArrowRightLeft,
  Bot,
  CheckCircle2,
  CircleDot,
  FileCheck2,
  GitBranch,
  MessageSquareText,
  PackageCheck,
  RefreshCcw,
  SearchCode,
  SlidersHorizontal,
  Timer,
  TriangleAlert,
} from "lucide-react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { useId, useLayoutEffect, useMemo, useRef, useState } from "react";

import type { WorkerActivity } from "./types";

const ACTIVITY_ESTIMATED_SIZE = 104;
const BOUNDARY_ESTIMATED_SIZE = 56;

export function WorkerTimeline({
  activities,
  hasMore,
  isLoadingMore,
  onLoadMore,
  onOpenTrace,
  onOpenCurrentTrace,
  initialScrollTop = null,
  showSystemRecords: controlledSystemRecords,
  onSystemRecordsChange,
}: {
  activities: readonly WorkerActivity[];
  hasMore: boolean;
  isLoadingMore: boolean;
  onLoadMore: () => void;
  onOpenTrace: (activity: WorkerActivity) => void;
  onOpenCurrentTrace?: (() => void) | undefined;
  initialScrollTop?: number | null;
  showSystemRecords?: boolean;
  onSystemRecordsChange?: (show: boolean) => void;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [localSystemRecords, setLocalSystemRecords] = useState(false);
  const showSystemRecords = controlledSystemRecords ?? localSystemRecords;
  const visibleActivities = useMemo(() => showSystemRecords
    ? activities
    : activities.filter((activity) => !isRoutineLifecycle(activity.kind)), [activities, showSystemRecords]);
  const followTail = useRef(initialScrollTop === null);
  const initialized = useRef(false);
  const previousTail = useRef(visibleActivities.at(-1)?.activity_id);
  const previousFilter = useRef(showSystemRecords);
  const [hasNewActivity, setHasNewActivity] = useState(false);
  // TanStack Virtual exposes an intentionally stateful virtualizer instance.
  // eslint-disable-next-line react-hooks/incompatible-library
  const virtualizer = useVirtualizer({
    count: visibleActivities.length,
    getItemKey: (index) => visibleActivities[index]!.activity_id,
    // Preserve the visible item when older pages or measured row heights change.
    anchorTo: "end",
    getScrollElement: () => scrollRef.current,
    estimateSize: (index) => isRoutineLifecycle(visibleActivities[index]?.kind)
      ? BOUNDARY_ESTIMATED_SIZE
      : ACTIVITY_ESTIMATED_SIZE,
    overscan: 12,
  });
  const virtualItems = virtualizer.getVirtualItems();
  const totalSize = virtualizer.getTotalSize();

  useLayoutEffect(() => {
    const element = scrollRef.current;
    if (!element) return;
    const tail = visibleActivities.at(-1)?.activity_id;
    if (followTail.current) {
      element.scrollTop = element.scrollHeight;
    } else if (!initialized.current && initialScrollTop !== null) {
      element.scrollTop = initialScrollTop;
    } else if (previousFilter.current === showSystemRecords && previousTail.current !== tail) {
      setHasNewActivity(true);
    }
    initialized.current = true;
    previousTail.current = tail;
    previousFilter.current = showSystemRecords;
  }, [initialScrollTop, showSystemRecords, totalSize, visibleActivities]);

  const followLatest = () => {
    followTail.current = true;
    setHasNewActivity(false);
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  };

  return (
    <section className="worker-timeline" aria-labelledby="worker-timeline-title">
      <div className="section-heading worker-timeline__heading">
        <div>
          <h2 id="worker-timeline-title">工作记录</h2>
        </div>
        <div className="worker-timeline__actions">
          <button className="quiet-button" type="button" aria-pressed={showSystemRecords}
            onClick={() => {
              const next = !showSystemRecords;
              setLocalSystemRecords(next);
              onSystemRecordsChange?.(next);
            }}>
            <SlidersHorizontal size={14} aria-hidden="true" />系统记录
          </button>
          <button className="quiet-button" type="button" aria-label="查看当前 Agent Trace" title="查看当前 Agent Trace"
            onClick={onOpenCurrentTrace} disabled={!onOpenCurrentTrace}>
            <SearchCode size={14} aria-hidden="true" />Trace
          </button>
          {hasMore ? (
            <button
              className="quiet-button"
              type="button"
              onClick={() => {
                followTail.current = false;
                onLoadMore();
              }}
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
      {visibleActivities.length === 0 ? (
        <div className="worker-timeline__empty" role="status">
          <MessageSquareText size={17} aria-hidden="true" />
          <strong>还没有工作步骤</strong>
          <span>等待 Agent 更新进展</span>
        </div>
      ) : (
        <div
          className="worker-timeline__scroll"
          data-workspace-scroll="worker-timeline"
          ref={scrollRef}
          tabIndex={0}
          role="region"
          aria-label="WorkerItem 活动时间线"
          onScroll={(event) => {
            const element = event.currentTarget;
            const atTail = element.scrollTop + element.clientHeight >= element.scrollHeight - 32;
            followTail.current = atTail;
            if (atTail) {
              setHasNewActivity(false);
            }
          }}
        >
          <div
            className="worker-timeline__spacer"
            style={{ height: totalSize || visibleActivities.length * ACTIVITY_ESTIMATED_SIZE }}
          >
            {(virtualItems.length > 0
              ? virtualItems
              : visibleActivities.slice(0, 120).map((_, index) => ({
                index,
                start: index * ACTIVITY_ESTIMATED_SIZE,
                size: ACTIVITY_ESTIMATED_SIZE,
              }))
            ).map((item) => {
              const activity = visibleActivities[item.index];
              if (activity === undefined) return null;
              return (
                <WorkerActivityRow
                  key={activity.activity_id}
                  index={item.index}
                  measureRef={virtualizer.measureElement}
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
  index,
  measureRef,
  activity,
  onOpenTrace,
  style,
}: {
  index: number;
  measureRef: (element: HTMLElement | null) => void;
  activity: WorkerActivity;
  onOpenTrace: (activity: WorkerActivity) => void;
  style: React.CSSProperties;
}) {
  const titleId = useId();
  const boundary = isRoutineLifecycle(activity.kind);
  const variant = activityVariant(activity.kind);
  const isTransportTitle = (activity.kind === "assistant_summary" && activity.title === "assistant.summary")
    || (activity.kind === "tool" && activity.title === "tool.call");
  const isGenericIntervention = activity.kind === "user_input"
    && (activity.title === "用户介入" || activity.title === "user_input");
  const title = isGenericIntervention
    ? activity.body || activity.summary || activity.title
    : isTransportTitle
    ? activity.summary || activity.title
    : activity.title;
  const traceButton = activity.trace_descriptor ? (
    <button
      className="worker-timeline-item__trace"
      type="button"
      aria-label="查看 Agent Trace"
      title="查看 Agent Trace"
      onClick={() => onOpenTrace(activity)}
    >
      <SearchCode size={14} aria-hidden="true" />
    </button>
  ) : null;
  const message = <>
    {activity.summary !== title && !(isGenericIntervention && title.startsWith(activity.summary)) ? <p>{activity.summary}</p> : null}
    {activity.body !== null && activity.body !== activity.summary && activity.body !== title ? <blockquote>{activity.body}</blockquote> : null}
  </>;
  const receipts = <>
    {activity.delivery_status ? <span className="worker-timeline-item__delivery">{deliveryLabel(activity.delivery_status)}</span> : null}
    {activity.artifact_ids.length > 0 ? (
      <span className="worker-timeline-item__attachment">
        <PackageCheck size={13} aria-hidden="true" />{activity.artifact_ids.length} 个关联产物
      </span>
    ) : null}
  </>;
  return (
    <article ref={measureRef} data-index={index} aria-labelledby={titleId} className={`worker-timeline-item worker-timeline-item--${variant}${boundary ? " worker-timeline-item--boundary" : ""}`} style={style}>
      <div className="worker-timeline-item__marker" aria-hidden="true">{activityIcon(activity.kind)}</div>
      {boundary ? (
        <div className="worker-timeline-item__lifecycle">
          <details>
            <summary><strong id={titleId}>{activity.title}</strong></summary>
            <div className="worker-timeline-item__body">
              {activity.execution_id ? <span className="mono-label">EXEC {shortId(activity.execution_id)}</span> : null}
              <div className="worker-timeline-item__message">{message}</div>
              {receipts}
            </div>
          </details>
          <time dateTime={activity.occurred_at}>{formatTime(activity.occurred_at)}</time>
          {traceButton}
        </div>
      ) : <div className="worker-timeline-item__body">
        <div className="worker-timeline-item__topline">
          <span className="worker-timeline-item__category">{activityLabel(activity.kind)}</span>
          <time dateTime={activity.occurred_at}>{formatTime(activity.occurred_at)}</time>
          {traceButton}
        </div>
        <div className="worker-timeline-item__message">
          <strong id={titleId}>{title}</strong>
          {message}
        </div>
        {receipts}
      </div>}
    </article>
  );
}

function isRoutineLifecycle(kind: string | undefined): boolean {
  return kind === "session.started" || kind === "session.closed"
    || kind === "execution.created" || kind === "execution.running";
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
  if (kind === "user_input") return "你的介入";
  if (kind === "checkpoint") return "阶段结果";
  if (kind === "artifact" || kind === "evidence") return "交付记录";
  if (kind.startsWith("handoff.")) return "Agent 切换";
  if (kind === "error" || kind.startsWith("error.")) return "执行异常";
  if (kind === "plan.generation") return "计划调整";
  if (kind === "plan.patch") return "上下文更新";
  if (kind === "plan.continuation") return "工作续接";
  if (kind === "plan.recovery") return "异常恢复";
  if (isBoundary(kind)) return "执行记录";
  return "工作步骤";
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
  return <Bot size={16} />;
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
