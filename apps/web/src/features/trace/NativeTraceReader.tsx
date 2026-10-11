import { ArrowLeft, ChevronDown, ChevronRight, CircleDot, Radio, Search, X } from "lucide-react";
import { useLayoutEffect, useMemo, useRef, useState } from "react";

import type { NativeRunEvent } from "./types";

export function NativeTraceReader({
  events, selectedSequence, onSelect, onLoadNext, hasNext, isLoadingNext,
  followTail, onFollowTail, onPauseFollow,
}: {
  events: NativeRunEvent[];
  selectedSequence: number | null;
  onSelect: (event: NativeRunEvent) => void;
  onLoadNext: () => void;
  hasNext: boolean;
  isLoadingNext: boolean;
  followTail: boolean;
  onFollowTail: () => void;
  onPauseFollow: () => void;
}) {
  const [search, setSearch] = useState("");
  const [kind, setKind] = useState("");
  const [detailOpen, setDetailOpen] = useState(false);
  const detailRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const selectedButton = useRef<HTMLElement | null>(null);
  const query = search.trim().toLocaleLowerCase();
  const filtered = useMemo(() => events.filter((event) => (kind === "" || event.kind === kind)
    && (query === "" || `${event.sequence} ${event.kind} ${JSON.stringify(event.data)}`.toLocaleLowerCase().includes(query))),
  [events, kind, query]);
  const selected = filtered.find((event) => event.sequence === selectedSequence) ?? null;
  const kinds = [...new Set(events.map((event) => event.kind))];
  const isFiltered = kind !== "" || query !== "";
  const latestSequence = events.at(-1)?.sequence;
  const selectedEventSequence = selected?.sequence;

  useLayoutEffect(() => {
    if (detailRef.current) detailRef.current.scrollTop = 0;
  }, [selectedEventSequence]);
  useLayoutEffect(() => {
    if (followTail && !isFiltered && listRef.current) listRef.current.scrollTop = listRef.current.scrollHeight;
  }, [followTail, isFiltered, latestSequence]);
  useLayoutEffect(() => {
    if (detailOpen && window.matchMedia?.("(max-width: 767px)").matches) detailRef.current?.focus();
  }, [detailOpen]);

  return <div className="trace-reader" data-detail-open={detailOpen}>
    <div className="trace-reader__list">
      <div className="trace-reader__filters">
        <label className="trace-search"><Search size={15} aria-hidden="true" />
          <input type="search" aria-label="搜索已加载记录" placeholder="搜索已加载记录" value={search}
            onChange={(event) => { setSearch(event.target.value); onPauseFollow(); }} />
        </label>
        <select aria-label="记录类型" value={kind} onChange={(event) => { setKind(event.target.value); onPauseFollow(); }}>
          <option value="">全部类型</option>
          {kinds.map((value) => <option key={value} value={value}>{value}</option>)}
        </select>
        {isFiltered ? <button className="icon-button" type="button" aria-label="清除筛选" title="清除筛选"
          onClick={() => { setSearch(""); setKind(""); }}><X size={15} aria-hidden="true" /></button> : null}
      </div>
      <section className="trace-ledger trace-ledger--compact" aria-label="原始 Run 记录">
        <div className="section-heading trace-ledger__heading">
          <h2>{isFiltered ? `${filtered.length} / ${events.length}` : events.length} 条已加载记录</h2>
          <div className="trace-ledger__actions">
            {hasNext ? <button className="quiet-button" type="button" onClick={onLoadNext} disabled={isLoadingNext}>
              <ChevronDown size={14} aria-hidden="true" />{isLoadingNext ? "读取中" : "后续事件"}
            </button> : null}
            {!followTail || isFiltered ? <button className="quiet-button" type="button"
              onClick={() => { setSearch(""); setKind(""); onFollowTail(); }}>
              <Radio size={14} aria-hidden="true" />跟随尾部
            </button> : null}
          </div>
        </div>
        <div className="trace-ledger__scroll" ref={listRef} onScroll={(event) => {
          const element = event.currentTarget;
          if (followTail && element.scrollTop + element.clientHeight < element.scrollHeight - 24) onPauseFollow();
        }}>
          {filtered.length === 0 ? <div className="trace-ledger__empty" role="status">
            {isFiltered ? "已加载记录中没有匹配项" : "暂无轨迹记录"}
          </div> : null}
          <div className="trace-ledger__spacer">
            {filtered.map((event) => <button
              key={event.sequence} type="button"
              className={`trace-row${selected?.sequence === event.sequence ? " is-selected" : ""}`}
              style={{ position: "relative", height: 56 }}
              aria-current={selected?.sequence === event.sequence ? "true" : undefined}
              onClick={() => {
                selectedButton.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
                onSelect(event); setDetailOpen(true);
              }}>
              <span className="trace-row__kind trace-kind--other"><CircleDot size={16} aria-hidden="true" /></span>
              <span className="trace-row__main"><strong title={event.kind}>{event.kind}</strong>
                <span>{typeof event.data.text === "string" ? event.data.text : JSON.stringify(event.data)}</span>
              </span>
              <span className="trace-row__meta"><span>#{event.sequence}</span>
                <time dateTime={event.occurred_at} title={event.occurred_at}>{formatTime(event.occurred_at)}</time>
              </span>
              <ChevronRight className="trace-row__arrow" size={15} aria-hidden="true" />
            </button>)}
          </div>
        </div>
      </section>
    </div>
    <div className="trace-reader__detail" ref={detailRef} tabIndex={-1} aria-label="所选记录">
      <button className="quiet-button trace-reader__back" type="button" onClick={() => {
        setDetailOpen(false); requestAnimationFrame(() => selectedButton.current?.focus());
      }}><ArrowLeft size={16} aria-hidden="true" />返回记录</button>
      <NativeTraceDetails key={selected?.sequence ?? "none"} event={selected} />
    </div>
  </div>;
}

function NativeTraceDetails({ event }: { event: NativeRunEvent | null }) {
  if (event === null) return <section className="trace-details trace-details--empty" aria-label="轨迹详情">选择一条记录查看详情</section>;
  return <section className="trace-details" aria-label="轨迹详情">
    <header className="trace-details__heading">
      <div className="trace-details__identity"><span className="trace-kind trace-kind--other">
        <CircleDot size={14} aria-hidden="true" />{event.kind}</span><span className="trace-details__sequence">#{event.sequence}</span></div>
      <h2>{event.kind}</h2>
      <div className="trace-details__meta"><time dateTime={event.occurred_at}>{new Date(event.occurred_at).toLocaleString("zh-CN")}</time></div>
    </header>
    <dl className="trace-facts trace-content">
      <div><dt>Operation</dt><dd><code>{event.operation_id ?? "未提供"}</code></dd></div>
      <div><dt>Response</dt><dd><code>{event.response_id ?? "未提供"}</code></dd></div>
    </dl>
    <details className="trace-technical">
      <summary>公开数据</summary>
      <section className="trace-content trace-value" aria-label="公开数据">
        <pre tabIndex={0}><code>{JSON.stringify(event.data, null, 2)}</code></pre>
      </section>
    </details>
  </section>;
}

function formatTime(value: string): string {
  return new Date(value).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
