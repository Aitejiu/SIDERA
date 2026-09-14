import { ArrowLeft, Search, X } from "lucide-react";
import { useLayoutEffect, useMemo, useRef, useState, type ComponentProps } from "react";

import { ArtifactViewer } from "./ArtifactViewer";
import { TraceDetails } from "./TraceDetails";
import { TraceLedger } from "./TraceLedger";
import { traceCategory, traceIsError } from "./tracePresentation";

type Props = ComponentProps<typeof TraceLedger> & { workspaceId: string };

export function TraceReader({ workspaceId, events, selectedSequence, onSelect, ...ledgerProps }: Props) {
  const [search, setSearch] = useState("");
  const [category, setCategory] = useState("");
  const [detailOpen, setDetailOpen] = useState(false);
  const detailRef = useRef<HTMLDivElement>(null);
  const selectedButtonRef = useRef<HTMLElement | null>(null);
  const query = search.trim().toLocaleLowerCase();
  const filtered = useMemo(() => events.filter((event) => {
    const matchesType = category === "" || (category === "error" ? traceIsError(event.status, event.category) : event.category === category);
    return matchesType && (query === "" || `${event.sequence} ${event.title} ${event.summary} ${event.category}`.toLocaleLowerCase().includes(query));
  }), [category, events, query]);
  const selectedEvent = filtered.find((event) => event.sequence === selectedSequence) ?? null;
  const categories = [...new Set([...events.map((event) => event.category), ...(events.some((event) => traceIsError(event.status, event.category)) ? ["error"] : [])])];
  const isFiltered = query !== "" || category !== "";
  const selectedEventSequence = selectedEvent?.sequence;

  useLayoutEffect(() => {
    if (detailRef.current !== null) detailRef.current.scrollTop = 0;
  }, [selectedEventSequence]);

  useLayoutEffect(() => {
    if (detailOpen && window.matchMedia?.("(max-width: 767px)").matches) {
      detailRef.current?.focus();
    }
  }, [detailOpen]);

  return (
    <div className="trace-reader" data-detail-open={detailOpen}>
      <div className="trace-reader__list">
        <div className="trace-reader__filters">
          <label className="trace-search"><Search size={15} aria-hidden="true" />
            <input type="search" aria-label="搜索已加载记录" placeholder="搜索已加载记录" value={search} onChange={(event) => { setSearch(event.target.value); ledgerProps.onPauseFollow(); }} />
          </label>
          <select aria-label="记录类型" value={category} onChange={(event) => { setCategory(event.target.value); ledgerProps.onPauseFollow(); }}>
            <option value="">全部类型</option>
            {categories.map((value) => <option key={value} value={value}>{traceCategory(value).label}</option>)}
          </select>
          {isFiltered ? <button className="icon-button" type="button" aria-label="清除筛选" title="清除筛选" onClick={() => { setSearch(""); setCategory(""); }}><X size={15} aria-hidden="true" /></button> : null}
        </div>
        <TraceLedger
          {...ledgerProps}
          key={`${query}:${category}`}
          title={isFiltered ? `${filtered.length} / ${events.length} 条已加载记录` : `${events.length} 条已加载记录`}
          compact
          events={filtered}
          selectedSequence={selectedEvent?.sequence ?? null}
          followTail={ledgerProps.followTail && !isFiltered}
          onSelect={(event) => {
            selectedButtonRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
            onSelect(event);
            setDetailOpen(true);
          }}
          onFollowTail={() => { setSearch(""); setCategory(""); ledgerProps.onFollowTail(); }}
          emptyMessage={isFiltered ? "已加载记录中没有匹配项" : "暂无轨迹记录"}
        />
      </div>
      <div className="trace-reader__detail" ref={detailRef} tabIndex={-1} aria-label="所选记录">
        <button className="quiet-button trace-reader__back" type="button" onClick={() => {
          setDetailOpen(false);
          requestAnimationFrame(() => selectedButtonRef.current?.focus());
        }}><ArrowLeft size={16} aria-hidden="true" />返回记录</button>
        <SelectedRecord key={selectedEvent?.sequence ?? "none"} workspaceId={workspaceId} event={selectedEvent} />
      </div>
    </div>
  );
}

function SelectedRecord({ workspaceId, event }: { workspaceId: string; event: ComponentProps<typeof TraceDetails>["event"] }) {
  const [artifactId, setArtifactId] = useState<string | null>(null);
  return <>
    <TraceDetails event={event} onArtifactSelect={setArtifactId} />
    {artifactId !== null ? <ArtifactViewer workspaceId={workspaceId} artifactId={artifactId} /> : null}
  </>;
}
