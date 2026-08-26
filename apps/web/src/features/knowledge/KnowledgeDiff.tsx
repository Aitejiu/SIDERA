import type { KnowledgeRevisionDiff } from "./types";

export function KnowledgeDiff({ diff }: { diff: KnowledgeRevisionDiff }) {
  const changedCount = diff.sections_changed.length;
  return (
    <section className="knowledge-diff" aria-label="Revision diff">
      <div className="knowledge-diff__summary">
        <span>{diff.title_changed ? "标题已变更" : "标题未变更"}</span>
        <span>{diff.summary_changed ? "摘要已变更" : "摘要未变更"}</span>
        <span>{diff.tags_added.length + diff.tags_removed.length} 个标签变化</span>
        <span>{changedCount} 个 section 变化</span>
      </div>
      {diff.tags_added.length > 0 || diff.tags_removed.length > 0 ? (
        <div className="knowledge-diff__tag-changes">
          {diff.tags_added.map((tag) => <span key={`added-${tag}`}>新增 {tag}</span>)}
          {diff.tags_removed.map((tag) => <span key={`removed-${tag}`}>移除 {tag}</span>)}
        </div>
      ) : null}
      {diff.sections_added.length > 0 || diff.sections_removed.length > 0 ? (
        <div className="knowledge-diff__section-changes">
          {diff.sections_added.map((section) => <span key={`added-${section}`}>新增 section {section}</span>)}
          {diff.sections_removed.map((section) => <span key={`removed-${section}`}>移除 section {section}</span>)}
        </div>
      ) : null}
      <pre className="knowledge-diff__body">{diff.body_unified_diff || "没有正文变化"}</pre>
      {diff.truncated ? <p className="knowledge-inspector-note">Diff 已按服务端限制截断。</p> : null}
    </section>
  );
}
