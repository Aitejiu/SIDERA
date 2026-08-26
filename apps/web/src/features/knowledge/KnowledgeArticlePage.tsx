import { GitCompareArrows, PencilLine, RefreshCw } from "lucide-react";
import { useMemo, useState, type FormEvent } from "react";
import { Link, useParams, useSearchParams, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { ApiProblem, createCommandIdentity } from "@/shared/api/client";

import {
  buildSubmitKnowledgeCandidateCommand,
  getKnowledgeDiff,
  getKnowledgePage,
  knowledgePageQueryKey,
  knowledgeRevisionQueryKey,
  submitKnowledgeCandidate,
} from "./api";
import { KnowledgeDiff } from "./KnowledgeDiff";
import { KnowledgeSourceList } from "./KnowledgeSourceList";
import { SafeMarkdown } from "./SafeMarkdown";
import type {
  KnowledgePageDetail,
  KnowledgeRevision,
  KnowledgeRevisionSummary,
  KnowledgeSource,
  SubmitCandidatePayload,
} from "./types";

const DEFAULT_WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";

export function KnowledgeArticlePage() {
  const workspaceId = useParams<{ workspaceId: string }>().workspaceId ?? DEFAULT_WORKSPACE_ID;
  const pageId = useParams<{ pageId: string }>().pageId ?? "";
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const selectedRevisionId = searchParams.get("revision_id");
  const [leftRevisionId, setLeftRevisionId] = useState("");
  const [rightRevisionId, setRightRevisionId] = useState("");
  const [diffSelection, setDiffSelection] = useState<{ left: string; right: string } | null>(null);
  const [editing, setEditing] = useState(false);
  const [editDraft, setEditDraft] = useState<EditDraft | null>(null);
  const [editError, setEditError] = useState<string | null>(null);
  const [submittingEdit, setSubmittingEdit] = useState(false);

  const pageQuery = useQuery({
    queryKey: knowledgePageQueryKey(workspaceId, pageId),
    queryFn: ({ signal }) => getKnowledgePage(workspaceId, pageId, signal),
    enabled: workspaceId.length > 0 && pageId.length > 0,
    retry: false,
  });
  const exactRevisionQuery = useQuery({
    queryKey: knowledgeRevisionQueryKey(workspaceId, pageId, selectedRevisionId ?? "none"),
    queryFn: ({ signal }) => getKnowledgePage(workspaceId, pageId, selectedRevisionId ?? "", signal),
    enabled: workspaceId.length > 0 && pageId.length > 0 && selectedRevisionId !== null,
    retry: false,
  });
  const page = pageQuery.data;
  const revision = exactRevisionQuery.data ?? page?.current_revision ?? null;
  const revisionHistory = useMemo(
    () => mergeRevisionSummaries(page, revision),
    [page, revision],
  );

  const diffQuery = useQuery({
    queryKey: ["knowledge", workspaceId, "diff", pageId, diffSelection?.left, diffSelection?.right],
    queryFn: ({ signal }) => getKnowledgeDiff(
      workspaceId,
      pageId,
      diffSelection?.left ?? "",
      diffSelection?.right ?? "",
      signal,
    ),
    enabled: diffSelection !== null
      && diffSelection.left.length > 0
      && diffSelection.right.length > 0
      && diffSelection.left !== diffSelection.right,
    retry: false,
  });

  function selectRevision(revisionId: string) {
    const next = new URLSearchParams(searchParams);
    if (revisionId === page?.current_revision?.revision_id) {
      next.delete("revision_id");
    } else {
      next.set("revision_id", revisionId);
    }
    setSearchParams(next);
    setDiffSelection(null);
  }

  function startEdit() {
    if (page === undefined || page.current_revision === null) return;
    const base = page.current_revision;
    setEditDraft({
      title: base.title,
      slug: page.summary.slug,
      summary: base.summary,
      tags: base.tags.join(", "),
      body: base.sections.map((section) => section.body_markdown).join("\n\n"),
    });
    setEditError(null);
    setEditing(true);
  }

  async function submitEdit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const current = page?.current_revision;
    if (page === undefined || current === null || current === undefined || editDraft === null) return;
    if (!editDraft.title.trim() || !editDraft.slug.trim() || !editDraft.body.trim()) {
      setEditError("标题、slug 和正文不能为空");
      return;
    }
    setSubmittingEdit(true);
    setEditError(null);
    const candidateId = createCommandIdentity().commandId;
    const payload: SubmitCandidatePayload = {
      candidate_schema_version: 1,
      candidate_id: candidateId,
      operation: "revise_page",
      target_page_id: page.summary.page_id,
      expected_revision_id: current.revision_id,
      expected_revision_hash: current.content_hash,
      title: editDraft.title.trim(),
      slug: editDraft.slug.trim(),
      summary: editDraft.summary.trim(),
      tags: parseTags(editDraft.tags),
      sections: current.sections.map((section, index) => ({
        section_key: section.section_key,
        heading: section.heading,
        summary: section.summary,
        body_markdown: index === 0 ? editDraft.body.trim() : section.body_markdown,
        ordinal: section.ordinal,
      })),
      relations: current.relations.map((relation) => ({
        relation_key: relation.relation_key,
        relation_kind: relation.relation_kind as "relates_to" | "depends_on" | "supersedes" | "derived_from" | "implements",
        target_page_id: relation.target_page_id,
        target_revision_id: relation.target_revision_id,
      })),
      sources: current.sources.map(toSourcePayload),
    };
    try {
      await submitKnowledgeCandidate(buildSubmitKnowledgeCandidateCommand({ workspaceId, payload }));
      navigate(`/workspaces/${workspaceId}/knowledge/candidates/${encodeURIComponent(candidateId)}`);
    } catch (error) {
      setEditError(error instanceof ApiProblem ? error.detail : "修改候选提交失败，请稍后重试");
    } finally {
      setSubmittingEdit(false);
    }
  }

  if (pageQuery.isPending || (selectedRevisionId !== null && exactRevisionQuery.isPending)) {
    return <KnowledgeArticleState label="正在读取知识页" />;
  }
  if (pageQuery.isError || exactRevisionQuery.isError) {
    return (
      <KnowledgeArticleState
        label="无法读取知识页"
        actionLabel="重试读取知识页"
        onAction={() => {
          void pageQuery.refetch();
          if (selectedRevisionId !== null) void exactRevisionQuery.refetch();
        }}
      />
    );
  }
  if (page === undefined || revision === null) {
    return <KnowledgeArticleState label="知识页没有可显示的 revision" />;
  }

  const currentRevisionId = page.current_revision?.revision_id;
  const canEdit = currentRevisionId !== undefined && revision.revision_id === currentRevisionId;
  const defaultRightRevisionId = selectedRevisionId ?? revisionHistory[0]?.revision_id ?? "";
  const defaultLeftRevisionId = revisionHistory[1]?.revision_id ?? defaultRightRevisionId;
  const effectiveRightRevisionId = rightRevisionId || defaultRightRevisionId;
  const effectiveLeftRevisionId = leftRevisionId || defaultLeftRevisionId;
  return (
    <section className="knowledge-article-page" aria-labelledby="knowledge-article-title">
      <div className="breadcrumb">
        <Link to={`/workspaces/${workspaceId}/knowledge`}>工作区 / 知识</Link> / {page.summary.slug}
      </div>
      <header className="knowledge-detail-heading">
        <div>
          <span className="eyebrow">KNOWLEDGE ARTICLE · REV {revision.revision_number}</span>
          <h1 id="knowledge-article-title">{revision.title}</h1>
          <p>{revision.summary || "无摘要"}</p>
        </div>
        <div className="knowledge-detail-heading__actions">
          <span className={`knowledge-status knowledge-status--${page.summary.status}`}>
            {pageStatusLabel(page.summary.status)}
          </span>
          <button
            className="quiet-button"
            type="button"
            onClick={startEdit}
            disabled={!canEdit}
            title={canEdit ? "基于当前 revision 生成修改候选" : "历史 revision 只读"}
          >
            <PencilLine size={14} aria-hidden="true" />基于当前版本提出修改
          </button>
          <button
            className="icon-button"
            type="button"
            onClick={() => {
              void pageQuery.refetch();
              if (selectedRevisionId !== null) void exactRevisionQuery.refetch();
            }}
            aria-label="刷新知识页"
            title="刷新知识页"
          >
            <RefreshCw size={16} aria-hidden="true" />
          </button>
        </div>
      </header>

      <div className="knowledge-article-layout">
        <main className="knowledge-article-body">
          {editing && editDraft !== null ? (
            <EditCandidateForm
              draft={editDraft}
              disabled={submittingEdit}
              error={editError}
              onChange={setEditDraft}
              onCancel={() => setEditing(false)}
              onSubmit={(event) => void submitEdit(event)}
            />
          ) : null}
          {revision.sections.map((section) => (
            <article className="knowledge-article-section" key={section.section_key}>
              <div className="knowledge-article-section__heading">
                <span className="eyebrow">SECTION {String(section.ordinal + 1).padStart(2, "0")}</span>
                <h2>{section.heading}</h2>
                {section.summary ? <p>{section.summary}</p> : null}
              </div>
              <SafeMarkdown source={section.body_markdown} />
            </article>
          ))}
        </main>

        <aside className="knowledge-inspector" aria-label="知识页 inspector">
          <InspectorRevision
            revision={revision}
            currentRevisionId={currentRevisionId}
            history={revisionHistory}
            selectedRevisionId={revision.revision_id}
            onSelectRevision={selectRevision}
          />
          <KnowledgeSourceList sources={revision.sources} />
          <RelationsList relations={revision.relations.map((relation) => ({
            key: relation.relation_key,
            kind: relation.relation_kind,
            target: relation.target_page_id,
          }))} />
          <DiffSelector
            revisions={revisionHistory}
            leftRevisionId={effectiveLeftRevisionId}
            rightRevisionId={effectiveRightRevisionId}
            onLeftChange={setLeftRevisionId}
            onRightChange={setRightRevisionId}
            onCompare={() => setDiffSelection({ left: effectiveLeftRevisionId, right: effectiveRightRevisionId })}
            diff={diffQuery.data}
            diffLoading={diffQuery.isPending}
            diffError={diffQuery.isError}
          />
        </aside>
      </div>
    </section>
  );
}

function InspectorRevision({
  revision,
  currentRevisionId,
  history,
  selectedRevisionId,
  onSelectRevision,
}: {
  revision: KnowledgeRevision;
  currentRevisionId: string | undefined;
  history: KnowledgeRevisionSummary[];
  selectedRevisionId: string;
  onSelectRevision: (revisionId: string) => void;
}) {
  return (
    <section className="knowledge-inspector-section" aria-labelledby="knowledge-revision-title">
      <div className="knowledge-inspector-heading">
        <span className="eyebrow">IMMUTABLE REVISION</span>
        <h2 id="knowledge-revision-title">当前版本</h2>
      </div>
      <dl className="knowledge-facts">
        <div><dt>Revision</dt><dd>REV {revision.revision_number}</dd></div>
        <div><dt>状态</dt><dd>{currentRevisionId === selectedRevisionId ? "当前" : "历史只读"}</dd></div>
        <div><dt>Hash</dt><dd><code className="knowledge-hash">{revision.content_hash}</code></dd></div>
        <div><dt>创建者</dt><dd>{revision.created_by}</dd></div>
        <div><dt>变更原因</dt><dd>{revision.change_reason || "未记录"}</dd></div>
      </dl>
      <div className="knowledge-history">
        <span className="eyebrow">HISTORY</span>
        {history.map((item) => (
          <button
            className={`knowledge-history__item${item.revision_id === selectedRevisionId ? " is-active" : ""}`}
            type="button"
            key={item.revision_id}
            onClick={() => onSelectRevision(item.revision_id)}
          >
            <span>REV {item.revision_number}</span>
            <span>{item.change_reason || item.created_by}</span>
          </button>
        ))}
      </div>
    </section>
  );
}

function RelationsList({ relations }: { relations: Array<{ key: string; kind: string; target: string }> }) {
  return (
    <section className="knowledge-inspector-section" aria-labelledby="knowledge-relations-title">
      <div className="knowledge-inspector-heading">
        <span className="eyebrow">GRAPH EDGES</span>
        <h2 id="knowledge-relations-title">关系</h2>
      </div>
      {relations.length === 0 ? <p className="knowledge-inspector-empty">无关系记录</p> : (
        <ul className="knowledge-relation-list">
          {relations.map((relation) => (
            <li key={relation.key}>
              <strong>{relation.kind}</strong>
              <code>{relation.target}</code>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function DiffSelector({
  revisions,
  leftRevisionId,
  rightRevisionId,
  onLeftChange,
  onRightChange,
  onCompare,
  diff,
  diffLoading,
  diffError,
}: {
  revisions: KnowledgeRevisionSummary[];
  leftRevisionId: string;
  rightRevisionId: string;
  onLeftChange: (value: string) => void;
  onRightChange: (value: string) => void;
  onCompare: () => void;
  diff: import("./types").KnowledgeRevisionDiff | undefined;
  diffLoading: boolean;
  diffError: boolean;
}) {
  return (
    <section className="knowledge-inspector-section" aria-labelledby="knowledge-diff-title">
      <div className="knowledge-inspector-heading">
        <span className="eyebrow">REVISION COMPARISON</span>
        <h2 id="knowledge-diff-title">比较版本</h2>
      </div>
      <div className="knowledge-diff-controls">
        <label>
          <span>左侧</span>
          <select value={leftRevisionId} onChange={(event) => onLeftChange(event.target.value)} aria-label="对比左侧 revision">
            {revisions.map((item) => <option key={item.revision_id} value={item.revision_id}>REV {item.revision_number}</option>)}
          </select>
        </label>
        <label>
          <span>右侧</span>
          <select value={rightRevisionId} onChange={(event) => onRightChange(event.target.value)} aria-label="对比右侧 revision">
            {revisions.map((item) => <option key={item.revision_id} value={item.revision_id}>REV {item.revision_number}</option>)}
          </select>
        </label>
        <button
          className="quiet-button"
          type="button"
          onClick={onCompare}
          disabled={!leftRevisionId || !rightRevisionId || leftRevisionId === rightRevisionId}
        >
          <GitCompareArrows size={14} aria-hidden="true" />查看 diff
        </button>
      </div>
      {diffLoading ? <p className="knowledge-inspector-note">正在读取 diff</p> : null}
      {diffError ? <p className="knowledge-form-error" role="alert">无法读取 diff</p> : null}
      {diff ? <KnowledgeDiff diff={diff} /> : null}
    </section>
  );
}

function EditCandidateForm({
  draft,
  disabled,
  error,
  onChange,
  onCancel,
  onSubmit,
}: {
  draft: EditDraft;
  disabled: boolean;
  error: string | null;
  onChange: (draft: EditDraft) => void;
  onCancel: () => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
}) {
  return (
    <form className="knowledge-edit-form" onSubmit={onSubmit}>
      <div className="knowledge-article-section__heading">
        <span className="eyebrow">CANDIDATE ONLY</span>
        <h2>提出修改候选</h2>
        <p>当前页面保持不变，提交后进入收件箱审核。</p>
      </div>
      <label className="knowledge-field"><span>标题</span><input value={draft.title} onChange={(event) => onChange({ ...draft, title: event.target.value })} /></label>
      <label className="knowledge-field"><span>slug</span><input value={draft.slug} onChange={(event) => onChange({ ...draft, slug: event.target.value })} /></label>
      <label className="knowledge-field"><span>摘要</span><textarea value={draft.summary} onChange={(event) => onChange({ ...draft, summary: event.target.value })} rows={3} /></label>
      <label className="knowledge-field"><span>标签</span><input value={draft.tags} onChange={(event) => onChange({ ...draft, tags: event.target.value })} /></label>
      <label className="knowledge-field"><span>正文（首个 section）</span><textarea value={draft.body} onChange={(event) => onChange({ ...draft, body: event.target.value })} rows={12} /></label>
      {error ? <p className="knowledge-form-error" role="alert">{error}</p> : null}
      <div className="knowledge-edit-form__actions">
        <button className="quiet-button" type="button" onClick={onCancel} disabled={disabled}>取消</button>
        <button className="primary-button" type="submit" disabled={disabled}>{disabled ? "提交中" : "提交修改候选"}</button>
      </div>
    </form>
  );
}

type EditDraft = {
  title: string;
  slug: string;
  summary: string;
  tags: string;
  body: string;
};

function KnowledgeArticleState({
  label,
  actionLabel,
  onAction,
}: {
  label: string;
  actionLabel?: string;
  onAction?: () => void;
}) {
  return (
    <section className="knowledge-detail-state" role={actionLabel ? "alert" : "status"}>
      <span className="eyebrow">KNOWLEDGE ARTICLE</span>
      <strong>{label}</strong>
      {actionLabel && onAction ? <button className="quiet-button" type="button" onClick={onAction}>{actionLabel}</button> : null}
    </section>
  );
}

function mergeRevisionSummaries(
  page: KnowledgePageDetail | undefined,
  selected: KnowledgeRevision | null,
): KnowledgeRevisionSummary[] {
  const values = [
    ...(page?.revision_history ?? []),
    ...(page?.current_revision ? [page.current_revision] : []),
    ...(selected ? [selected] : []),
  ];
  const byId = new Map(values.map((item) => [item.revision_id, item]));
  return [...byId.values()].sort((left, right) => right.revision_number - left.revision_number);
}

function toSourcePayload(source: KnowledgeSource) {
  const sourceType = isSourceType(source.source_type) ? source.source_type : "manual";
  return {
    source_id: source.source_id,
    source_type: sourceType,
    label: source.label,
    uri: source.uri,
    workspace_relative_path: source.workspace_relative_path,
    git_revision: source.git_revision,
    digest: source.digest,
    metadata: source.metadata,
  };
}

function isSourceType(value: string): value is "manual" | "markdown_file" | "task" | "artifact" | "evidence" | "external_url" {
  return ["manual", "markdown_file", "task", "artifact", "evidence", "external_url"].includes(value);
}

function parseTags(value: string): string[] {
  return [...new Set(value.split(",").map((tag) => tag.trim()).filter(Boolean))];
}

function pageStatusLabel(value: string): string {
  switch (value) {
    case "active": return "生效";
    case "archived": return "归档";
    default: return value;
  }
}
