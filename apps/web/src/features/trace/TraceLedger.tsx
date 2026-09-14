import { ChevronRight, ChevronUp, Radio } from "lucide-react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { memo, useLayoutEffect, useMemo, useRef } from "react";
import Markdown from "react-markdown";

import type { WorkerTraceEvent } from "./types";
import { formatTraceDuration, traceCategory, traceIsError, traceStatus } from "./tracePresentation";

export function TraceLedger({
  title = "Worker 轨迹",
  events,
  selectedSequence,
  onSelect,
  onLoadOlder,
  hasOlder,
  isLoadingOlder,
  followTail,
  onFollowTail,
  onPauseFollow,
  compact = false,
  emptyMessage = "暂无轨迹记录",
}: {
  title?: string;
  events: WorkerTraceEvent[];
  selectedSequence: number | null;
  onSelect: (event: WorkerTraceEvent) => void;
  onLoadOlder: () => void;
  hasOlder: boolean;
  isLoadingOlder: boolean;
  followTail: boolean;
  onFollowTail: () => void;
  onPauseFollow: () => void;
  compact?: boolean;
  emptyMessage?: string;
}) {
  const rowHeight = compact ? 56 : 76;
  const parentRef = useRef<HTMLDivElement>(null);
  const pendingPrependRef = useRef<{ scrollTop: number; scrollHeight: number; count: number } | null>(null);
  // TanStack Virtual exposes an intentionally stateful virtualizer instance.
  // eslint-disable-next-line react-hooks/incompatible-library
  const virtualizer = useVirtualizer({
    count: events.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => rowHeight,
    getItemKey: (index) => events[index]?.sequence ?? index,
    overscan: 12,
  });
  const virtualItems = virtualizer.getVirtualItems();
  const visibleItems = useMemo(
    () => virtualItems.length > 0
      ? virtualItems
      : events.slice(0, 120).map((_, index) => ({ index, start: index * rowHeight, size: rowHeight })),
    [events, virtualItems, rowHeight],
  );

  useLayoutEffect(() => {
    const pending = pendingPrependRef.current;
    const scrollElement = parentRef.current;
    if (pending === null || scrollElement === null || events.length <= pending.count) {
      return;
    }
    const heightDelta = scrollElement.scrollHeight - pending.scrollHeight;
    scrollElement.scrollTop = pending.scrollTop + heightDelta;
    pendingPrependRef.current = null;
  }, [events.length]);

  const latestSequence = events.at(-1)?.sequence;
  useLayoutEffect(() => {
    const element = parentRef.current;
    if (compact && followTail && element !== null && pendingPrependRef.current === null) {
      element.scrollTop = element.scrollHeight;
    }
  }, [compact, followTail, latestSequence]);

  const loadOlder = () => {
    const scrollElement = parentRef.current;
    if (scrollElement !== null) {
      pendingPrependRef.current = {
        scrollTop: scrollElement.scrollTop,
        scrollHeight: scrollElement.scrollHeight,
        count: events.length,
      };
    }
    onPauseFollow();
    onLoadOlder();
  };

  return (
    <section className={`trace-ledger${compact ? " trace-ledger--compact" : ""}`} aria-label={title}>
      <div className="section-heading trace-ledger__heading">
        <div>
          <h2 id="trace-ledger-title">{title}</h2>
        </div>
        <div className="trace-ledger__actions">
          {hasOlder ? (
            <button className="quiet-button" type="button" onClick={loadOlder} disabled={isLoadingOlder}>
              <ChevronUp size={14} aria-hidden="true" />{isLoadingOlder ? "读取中" : "更早事件"}
            </button>
          ) : null}
          {!followTail ? (
            <button className="quiet-button" type="button" onClick={onFollowTail}>
              <Radio size={14} aria-hidden="true" />跟随尾部
            </button>
          ) : null}
        </div>
      </div>
      <div
        className="trace-ledger__scroll"
        ref={parentRef}
        onScroll={(event) => {
          const element = event.currentTarget;
          if (followTail && element.scrollTop + element.clientHeight < element.scrollHeight - 24) {
            onPauseFollow();
          }
        }}
      >
        {events.length === 0 ? <div className="trace-ledger__empty" role="status">{emptyMessage}</div> : null}
        <div className="trace-ledger__spacer" style={{ height: virtualizer.getTotalSize() || events.length * rowHeight }}>
          {visibleItems.map((item) => {
            const event = events[item.index];
            if (event === undefined) return null;
            const category = traceCategory(event.category);
            const Icon = category.icon;
            const isError = traceIsError(event.status, event.category);
            return (
              <button
                className={`trace-row${selectedSequence === event.sequence ? " is-selected" : ""}${isError ? " trace-row--error" : ""}`}
                key={`${event.sequence}-${event.occurred_at}`}
                type="button"
                onClick={() => onSelect(event)}
                style={{ transform: `translateY(${item.start}px)`, height: rowHeight }}
                aria-current={selectedSequence === event.sequence ? "true" : undefined}
              >
                {compact ? <span className={`trace-row__kind trace-kind--${category.tone}`} title={category.label} aria-label={category.label}><Icon size={16} aria-hidden="true" /></span>
                  : <span className="trace-row__sequence">#{event.sequence}</span>}
                <span className="trace-row__main">
                  <strong title={event.title}>{event.title}</strong>
                  <span title={event.summary}>{compact ? <TracePreview text={event.summary} /> : event.summary}</span>
                </span>
                <span className="trace-row__meta">
                  <span className={isError ? "trace-event-error" : ""}>{traceStatus(event.status)}</span>
                  <span>{event.duration_ms === null ? `#${event.sequence}` : formatTraceDuration(event.duration_ms)}</span>
                </span>
                <ChevronRight className="trace-row__arrow" size={15} aria-hidden="true" />
              </button>
            );
          })}
        </div>
      </div>
    </section>
  );
}

const TracePreview = memo(function TracePreview({ text }: { text: string }) {
  return <Markdown allowedElements={[]} unwrapDisallowed skipHtml>{text}</Markdown>;
});
