import { FileArchive } from "lucide-react";

import { SafeMarkdown } from "../knowledge/SafeMarkdown";
import { ProviderExtensions } from "./providerExtensions";
import { formatTraceDuration, traceCategory, traceIsError, traceStatus } from "./tracePresentation";
import type { WorkerTraceEvent } from "./types";

export function TraceDetails({ event, onArtifactSelect }: {
  event: WorkerTraceEvent | null;
  onArtifactSelect: (artifactId: string) => void;
}) {
  if (event === null) {
    return <section className="trace-details trace-details--empty" aria-label="轨迹详情">选择一条记录查看详情</section>;
  }
  const category = traceCategory(event.category);
  const Icon = category.icon;
  const isTool = event.category === "tool" || event.category === "subtool";
  const extensions = event.provider_extensions;
  return (
    <section className="trace-details" aria-label="轨迹详情">
      <header className="trace-details__heading">
        <div className="trace-details__identity">
          <span className={`trace-kind trace-kind--${category.tone}`}><Icon size={14} aria-hidden="true" />{category.label}</span>
          <span className="trace-details__sequence">#{event.sequence}</span>
        </div>
        <h2 id="trace-details-title">{event.title}</h2>
        <div className="trace-details__meta">
          <span className={traceIsError(event.status, event.category) ? "trace-event-error" : ""}>{traceStatus(event.status)}</span>
          <time dateTime={event.occurred_at}>{formatTimestamp(event.occurred_at)}</time>
          {event.duration_ms !== null ? <span>{formatTraceDuration(event.duration_ms)}</span> : null}
        </div>
      </header>
      <section className="trace-content" aria-label="摘要">
        <h3>摘要</h3>
        {event.summary ? <SafeMarkdown source={event.summary} allowImages={false} /> : <span className="muted-copy">未提供</span>}
      </section>
      {isTool ? <>
        <TraceValue label="输入" value={extensions.rawInput} />
        <TraceValue label="输出" value={extensions.rawOutput} />
      </> : null}
      {event.artifact_refs.length > 0 ? (
        <section className="trace-content" aria-label="关联产物">
          <h3>关联产物</h3>
          <div className="artifact-ref-list">{event.artifact_refs.map((artifactId) => (
            <button className="artifact-ref" key={artifactId} type="button" onClick={() => onArtifactSelect(artifactId)}>
              <FileArchive size={14} aria-hidden="true" />{artifactId}
            </button>
          ))}</div>
        </section>
      ) : null}
      <details className="trace-technical" aria-label="技术详情">
        <summary>技术详情</summary>
        <dl className="trace-facts">
          <div><dt>事件类型</dt><dd>{event.category}</dd></div>
          <div><dt>原始状态</dt><dd>{event.status}</dd></div>
          <div><dt>耗时</dt><dd>{formatTraceDuration(event.duration_ms)}</dd></div>
          <div><dt>WorkItem</dt><dd>{event.work_item_key ?? "未提供"}</dd></div>
          <div><dt>Payload ref</dt><dd>{event.provider_payload_ref ?? "未提供"}</dd></div>
        </dl>
        <TraceValue label="用量" value={event.usage} />
        {Object.keys(extensions).length > 0 ? <ProviderExtensions extensions={extensions} /> : null}
      </details>
    </section>
  );
}

function TraceValue({ label, value }: { label: string; value: unknown }) {
  return <section className="trace-content trace-value" aria-label={label}>
    <h3>{label}</h3>
    {value === null || value === undefined ? <span className="muted-copy">未提供</span>
      : <pre tabIndex={0}><code>{typeof value === "string" ? value : JSON.stringify(value, null, 2)}</code></pre>}
  </section>;
}

function formatTimestamp(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN");
}
