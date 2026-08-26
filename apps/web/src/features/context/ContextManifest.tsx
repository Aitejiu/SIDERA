import type {
  ContextPackage,
  ContextPackageKnowledgeRef,
  ContextPackageReference,
} from "./types";

export function ContextManifest({
  contextPackage,
  knowledgeTitles,
}: {
  contextPackage: ContextPackage;
  knowledgeTitles: ReadonlyMap<string, string>;
}) {
  return (
    <section className="context-manifest" aria-labelledby="context-manifest-title">
      <div className="section-heading">
        <div>
          <span className="eyebrow">EXACT MANIFEST</span>
          <h2 id="context-manifest-title">Manifest</h2>
        </div>
        <span className="mono-label">BODY NOT LOADED</span>
      </div>

      <ManifestKnowledgeRefs refs={contextPackage.knowledge_refs} titles={knowledgeTitles} />
      <ManifestReferenceGroup
        label="ARTIFACT REFS"
        refs={contextPackage.artifact_refs}
        emptyLabel="没有固定 Artifact 引用"
      />
      <ManifestReferenceGroup
        label="EVIDENCE REFS"
        refs={contextPackage.evidence_refs}
        emptyLabel="没有固定 Evidence 引用"
      />
    </section>
  );
}

function ManifestKnowledgeRefs({
  refs,
  titles,
}: {
  refs: readonly ContextPackageKnowledgeRef[];
  titles: ReadonlyMap<string, string>;
}) {
  return (
    <div className="context-manifest__group">
      <div className="context-manifest__group-heading">
        <span className="eyebrow">KNOWLEDGE SECTIONS</span>
        <strong>{refs.length} 个固定 section</strong>
      </div>
      {refs.length === 0 ? <p className="muted-copy">没有固定知识引用</p> : null}
      {refs.length > 0 ? (
        <ol className="context-manifest__list">
          {refs.map((ref) => {
            const key = knowledgeRefKey(ref);
            return (
              <li key={key} className="context-manifest__item">
                <div className="context-manifest__item-heading">
                  <strong>{titles.get(key) ?? `知识页 ${shortId(ref.page_id)}`}</strong>
                  <code>{ref.section_key}</code>
                </div>
                <div className="context-manifest__item-meta">
                  <span>revision {shortId(ref.revision_id)}</span>
                  <span>section hash {shortHash(ref.section_hash)}</span>
                  <span>trust <strong>{ref.trust_level}</strong></span>
                </div>
                <div className="context-manifest__item-meta">
                  <span>source {formatSourceIds(ref.source_ids)}</span>
                  <span>reason {ref.selection_reason}</span>
                  <span>revision hash {shortHash(ref.revision_hash)}</span>
                </div>
              </li>
            );
          })}
        </ol>
      ) : null}
    </div>
  );
}

function ManifestReferenceGroup({
  label,
  refs,
  emptyLabel,
}: {
  label: string;
  refs: readonly ContextPackageReference[];
  emptyLabel: string;
}) {
  return (
    <div className="context-manifest__group">
      <div className="context-manifest__group-heading">
        <span className="eyebrow">{label}</span>
        <strong>{refs.length} 个引用</strong>
      </div>
      {refs.length === 0 ? <p className="muted-copy">{emptyLabel}</p> : null}
      {refs.length > 0 ? (
        <ol className="context-manifest__refs">
          {refs.map((ref) => (
            <li key={`${ref.ref_type}:${ref.ref_id}`}>
              <code>{ref.ref_type}</code>
              <span>{ref.ref_id}</span>
            </li>
          ))}
        </ol>
      ) : null}
    </div>
  );
}

export function knowledgeRefKey(ref: Pick<ContextPackageKnowledgeRef, "page_id" | "revision_id" | "section_key">): string {
  return `${ref.page_id}:${ref.revision_id}:${ref.section_key}`;
}

function formatSourceIds(sourceIds: readonly string[]): string {
  if (sourceIds.length === 0) return "未记录";
  return sourceIds.map(shortId).join(", ");
}

function shortId(value: string): string {
  return value.slice(0, 8);
}

function shortHash(value: string): string {
  return value.slice(0, 8);
}
