import { FileArchive, Info, Timer, Wrench } from "lucide-react";
import { useState } from "react";

import { ProviderExtensions } from "./providerExtensions";
import type { WorkerTraceEvent } from "./types";

type TraceDetailTab = "overview" | "input" | "output" | "timing" | "usage" | "artifacts";

export function TraceDetails({
  event,
  onArtifactSelect,
}: {
  event: WorkerTraceEvent | null;
  onArtifactSelect: (artifactId: string) => void;
}) {
  const [tab, setTab] = useState<TraceDetailTab>("overview");
  if (event === null) {
    return <section className="trace-details trace-details--empty" aria-label="轨迹详情"><span>选择一条轨迹查看详情</span></section>;
  }
  const tabs: { key: TraceDetailTab; label: string }[] = [
    { key: "overview", label: "Overview" },
    { key: "input", label: "Input" },
    { key: "output", label: "Output" },
    { key: "timing", label: "Timing" },
    { key: "usage", label: "Usage" },
    { key: "artifacts", label: "Artifacts" },
  ];
  return (
    <section className="trace-details" aria-labelledby="trace-details-title">
      <div className="section-heading">
        <div>
          <span className="eyebrow">EVENT #{event.sequence}</span>
          <h2 id="trace-details-title">{event.title}</h2>
        </div>
        <span className="trace-details__category">{event.category}</span>
      </div>
      <div className="trace-tabs" role="tablist" aria-label="轨迹事件详情">
        {tabs.map((item) => (
          <button
            key={item.key}
            className={tab === item.key ? "is-active" : ""}
            type="button"
            role="tab"
            aria-selected={tab === item.key}
            onClick={() => setTab(item.key)}
          >
            {tabIcon(item.key)}{item.label}
          </button>
        ))}
      </div>
      <div className="trace-details__body">
        {tab === "overview" ? <Overview event={event} /> : null}
        {tab === "input" ? <SafeValue label="Input" value={event.summary} /> : null}
        {tab === "output" ? <SafeValue label="Output" value={event.summary} /> : null}
        {tab === "timing" ? <Timing event={event} /> : null}
        {tab === "usage" ? <Usage event={event} /> : null}
        {tab === "artifacts" ? (
          <div className="artifact-ref-list">
            {event.artifact_refs.length === 0 ? <span className="muted-copy">没有关联 Artifact</span> : null}
            {event.artifact_refs.map((artifactId) => (
              <button className="artifact-ref" key={artifactId} type="button" onClick={() => onArtifactSelect(artifactId)}>
                <FileArchive size={14} aria-hidden="true" />{artifactId}
              </button>
            ))}
          </div>
        ) : null}
      </div>
      <ProviderExtensions extensions={event.provider_extensions} />
    </section>
  );
}

function Overview({ event }: { event: WorkerTraceEvent }) {
  return (
    <div className="trace-overview">
      <p>{event.summary}</p>
      <dl className="trace-facts">
        <div><dt>Status</dt><dd>{event.status}</dd></div>
        <div><dt>Occurred</dt><dd>{formatTimestamp(event.occurred_at)}</dd></div>
        <div><dt>WorkItem</dt><dd>{event.work_item_key ?? "未提供"}</dd></div>
        <div><dt>Payload ref</dt><dd>{event.provider_payload_ref ?? "未提供"}</dd></div>
      </dl>
    </div>
  );
}

function Timing({ event }: { event: WorkerTraceEvent }) {
  return <SafeValue label="Duration" value={event.duration_ms === null ? "未提供" : `${event.duration_ms} ms`} />;
}

function Usage({ event }: { event: WorkerTraceEvent }) {
  return <SafeValue label="Usage" value={event.usage ?? "未提供"} />;
}

function SafeValue({ label, value }: { label: string; value: unknown }) {
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  return <div className="trace-value"><span className="eyebrow">{label}</span><pre>{text}</pre></div>;
}

function tabIcon(tab: TraceDetailTab) {
  if (tab === "timing") return <Timer size={13} aria-hidden="true" />;
  if (tab === "artifacts") return <FileArchive size={13} aria-hidden="true" />;
  if (tab === "output" || tab === "input") return <Wrench size={13} aria-hidden="true" />;
  return <Info size={13} aria-hidden="true" />;
}

function formatTimestamp(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN");
}
