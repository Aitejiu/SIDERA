import type { KnowledgeSource } from "./types";

export function KnowledgeSourceList({
  sources,
  heading = "来源",
}: {
  sources: KnowledgeSource[];
  heading?: string;
}) {
  return (
    <section className="knowledge-inspector-section" aria-labelledby="knowledge-sources-title">
      <div className="knowledge-inspector-heading">
        <span className="eyebrow">PROVENANCE</span>
        <h2 id="knowledge-sources-title">{heading}</h2>
      </div>
      {sources.length === 0 ? (
        <p className="knowledge-inspector-empty">无来源记录</p>
      ) : (
        <ul className="knowledge-source-list">
          {sources.map((source) => (
            <li key={source.source_id} className="knowledge-source-list__item">
              <div className="knowledge-source-list__topline">
                <strong>{source.label}</strong>
                <span className={`knowledge-availability knowledge-availability--${source.availability}`}>
                  {availabilityLabel(source.availability)}
                </span>
              </div>
              <span className="knowledge-secondary-meta">
                {source.source_type}
                {source.workspace_relative_path ? ` · ${source.workspace_relative_path}` : ""}
              </span>
              {source.safe_reason_code ? (
                <span className="knowledge-inspector-note">{source.safe_reason_code}</span>
              ) : null}
              {source.digest ? <code className="knowledge-hash">{source.digest}</code> : null}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function availabilityLabel(value: string): string {
  switch (value) {
    case "available":
      return "可用";
    case "unavailable":
      return "不可用";
    default:
      return value;
  }
}
