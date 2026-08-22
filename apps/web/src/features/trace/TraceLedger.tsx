import { ChevronDown, ChevronUp, Radio } from "lucide-react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { useLayoutEffect, useMemo, useRef } from "react";

import type { WorkerTraceEvent } from "./types";

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
}) {
  const parentRef = useRef<HTMLDivElement>(null);
  const pendingPrependRef = useRef<{ scrollTop: number; scrollHeight: number; count: number } | null>(null);
  // TanStack Virtual exposes an intentionally stateful virtualizer instance.
  // eslint-disable-next-line react-hooks/incompatible-library
  const virtualizer = useVirtualizer({
    count: events.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 76,
    overscan: 12,
  });
  const virtualItems = virtualizer.getVirtualItems();
  const visibleItems = useMemo(
    () => virtualItems.length > 0
      ? virtualItems
      : events.slice(0, 120).map((_, index) => ({ index, start: index * 76, size: 76 })),
    [events, virtualItems],
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
    <section className="trace-ledger" aria-labelledby="trace-ledger-title">
      <div className="section-heading trace-ledger__heading">
        <div>
          <span className="eyebrow">TURN / STEP LEDGER</span>
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
        <div className="trace-ledger__spacer" style={{ height: virtualizer.getTotalSize() || events.length * 76 }}>
          {visibleItems.map((item) => {
            const event = events[item.index];
            if (event === undefined) return null;
            return (
              <button
                className={`trace-row${selectedSequence === event.sequence ? " is-selected" : ""}`}
                key={`${event.sequence}-${event.occurred_at}`}
                type="button"
                onClick={() => onSelect(event)}
                style={{ transform: `translateY(${item.start}px)` }}
                aria-current={selectedSequence === event.sequence ? "true" : undefined}
              >
                <span className="trace-row__sequence">#{event.sequence}</span>
                <span className="trace-row__main">
                  <strong>{event.title}</strong>
                  <span>{event.summary}</span>
                </span>
                <span className="trace-row__meta">
                  <span className={`trace-category trace-category--${event.category}`}>{event.category}</span>
                  <span>{event.duration_ms === null ? "--" : `${event.duration_ms} ms`}</span>
                </span>
                <ChevronDown className="trace-row__arrow" size={15} aria-hidden="true" />
              </button>
            );
          })}
        </div>
      </div>
    </section>
  );
}
