import { AlertTriangle, Check, CircleX, RefreshCw, Undo2 } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { ApiProblem } from "@/shared/api/client";

import {
  buildReviewKnowledgeCandidateCommand,
  buildWithdrawKnowledgeCandidateCommand,
  getKnowledgeCandidate,
  knowledgeCandidateQueryKey,
  submitKnowledgeCommand,
} from "./api";
import { KnowledgeDiff } from "./KnowledgeDiff";
import { KnowledgeSourceList } from "./KnowledgeSourceList";
import { SafeMarkdown } from "./SafeMarkdown";
import type {
  KnowledgeCandidateDetail,
  KnowledgeProposedSnapshot,
  ReviewCandidatePayload,
  WithdrawCandidatePayload,
} from "./types";

const DEFAULT_WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
type ReviewDecision = ReviewCandidatePayload["decision"];

export function KnowledgeCandidatePage() {
  const workspaceId = useParams<{ workspaceId: string }>().workspaceId ?? DEFAULT_WORKSPACE_ID;
  const candidateId = useParams<{ candidateId: string }>().candidateId ?? "";
  const navigate = useNavigate();
  const [decision, setDecision] = useState<ReviewDecision | null>(null);
  const [reason, setReason] = useState("");
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionSuccess, setActionSuccess] = useState<string | null>(null);
  const [staleConflict, setStaleConflict] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const candidateQuery = useQuery({
    queryKey: knowledgeCandidateQueryKey(workspaceId, candidateId),
    queryFn: ({ signal }) => getKnowledgeCandidate(workspaceId, candidateId, signal),
    enabled: workspaceId.length > 0 && candidateId.length > 0,
    retry: false,
  });

  function beginDecision(nextDecision: ReviewDecision) {
    if (nextDecision === "reject" && !window.confirm("确认拒绝此知识候选？")) {
      return;
    }
    setDecision(nextDecision);
    setActionError(nextDecision === "request_changes" ? "请先填写修改原因" : null);
    setActionSuccess(null);
  }

  function beginWithdraw() {
    if (!window.confirm("确认撤回此知识候选？")) return;
    setDecision(null);
    setActionError(null);
    setActionSuccess(null);
    void submitWithdraw();
  }

  async function submitReview(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (decision === null || candidateQuery.data === undefined) return;
    if (reason.trim() === "") {
      setActionError("请先填写修改原因");
      return;
    }
    const detail = candidateQuery.data;
    const payload: ReviewCandidatePayload = {
      candidate_id: detail.summary.candidate_id,
      decision,
      reason: reason.trim(),
      expected_candidate_version: detail.summary.version,
      observed_candidate_hash: detail.summary.candidate_hash,
      observed_target_revision_id: detail.summary.expected_revision_id,
      observed_target_revision_hash: detail.summary.expected_revision_hash,
    };
    setSubmitting(true);
    setActionError(null);
    setActionSuccess(null);
    setStaleConflict(false);
    try {
      const response = await submitKnowledgeCommand(
        buildReviewKnowledgeCandidateCommand({ workspaceId, payload }),
      );
      const result = commandResult(response);
      if (decision === "accept" && result.status === "accepted") {
        const pageId = stringValue(result.page_id) ?? detail.summary.target_page_id;
        const revisionId = stringValue(result.revision_id);
        if (pageId !== null && revisionId !== null) {
          navigate(`/workspaces/${workspaceId}/knowledge/pages/${encodeURIComponent(pageId)}?revision_id=${encodeURIComponent(revisionId)}`);
          return;
        }
      }
      setActionSuccess("审核命令已提交");
    } catch (error) {
      await handleActionError(error);
    } finally {
      setSubmitting(false);
    }
  }

  async function submitWithdraw() {
    const detail = candidateQuery.data;
    if (detail === undefined) return;
    const payload: WithdrawCandidatePayload = {
      candidate_id: detail.summary.candidate_id,
      expected_candidate_version: detail.summary.version,
    };
    setSubmitting(true);
    try {
      await submitKnowledgeCommand(
        buildWithdrawKnowledgeCandidateCommand({ workspaceId, payload }),
      );
      setActionSuccess("撤回命令已提交");
    } catch (error) {
      await handleActionError(error);
    } finally {
      setSubmitting(false);
    }
  }

  async function handleActionError(error: unknown) {
    if (error instanceof ApiProblem && error.status === 409) {
      setStaleConflict(true);
      setActionError("候选或目标版本已变化，已停止提交。请刷新详情后再操作。");
      await candidateQuery.refetch();
      return;
    }
    setActionError(error instanceof ApiProblem ? error.detail : "审核命令提交失败，请稍后重试");
  }

  if (candidateQuery.isPending) return <CandidateState label="正在读取知识候选" />;
  if (candidateQuery.isError) {
    return (
      <CandidateState
        label="无法读取知识候选"
        actionLabel="重试读取候选"
        onAction={() => void candidateQuery.refetch()}
      />
    );
  }
  if (candidateQuery.data === undefined) return <CandidateState label="知识候选不存在" />;

  const detail = candidateQuery.data;
  const summary = detail.summary;
  const snapshot = normalizeSnapshot(detail.proposed_snapshot, summary);
  const reviewable = summary.status === "pending" && !detail.target_is_stale;
  return (
    <section className="knowledge-candidate-page" aria-labelledby="knowledge-candidate-title">
      <div className="breadcrumb">
        <Link to={`/workspaces/${workspaceId}/knowledge/inbox`}>工作区 / 知识 / 收件箱</Link> / {summary.candidate_id.slice(0, 8)}
      </div>
      <header className="knowledge-detail-heading">
        <div>
          <span className="eyebrow">KNOWLEDGE CANDIDATE · {operationLabel(summary.operation)}</span>
          <h1 id="knowledge-candidate-title">{summary.proposed_title}</h1>
          <div className="knowledge-candidate-context">
            <span>任务 {shortId(summary.task_id)} / 子任务 {shortId(summary.subtask_id)}</span>
            <span>{summary.producer_id}{summary.execution_id ? ` · 执行 ${shortId(summary.execution_id)}` : ""}</span>
          </div>
        </div>
        <div className="knowledge-detail-heading__actions">
          <span className={`knowledge-status knowledge-status--${summary.status}`}>
            {candidateStatusLabel(summary.status)}
          </span>
          <button className="icon-button" type="button" onClick={() => void candidateQuery.refetch()} aria-label="刷新候选" title="刷新候选">
            <RefreshCw size={16} aria-hidden="true" />
          </button>
        </div>
      </header>

      {detail.target_is_stale ? (
        <div className="knowledge-stale-banner" role="alert">
          <AlertTriangle size={16} aria-hidden="true" />
          <span>目标页面已经变化，这个候选基于旧 revision，审核前必须重新生成候选。</span>
        </div>
      ) : null}
      {actionError ? (
        <div className="knowledge-action-error" role="alert">
          <CircleX size={16} aria-hidden="true" />
          <span>{actionError}</span>
          {staleConflict ? <span className="mono-label">未自动重试</span> : null}
        </div>
      ) : null}
      {actionSuccess ? <div className="knowledge-action-success" role="status">{actionSuccess}</div> : null}

      <div className="knowledge-candidate-layout">
        <main className="knowledge-candidate-main">
          <section className="knowledge-candidate-section" aria-labelledby="knowledge-proposal-title">
            <div className="knowledge-section-heading">
              <div><span className="eyebrow">PROPOSED FULL SNAPSHOT</span><h2 id="knowledge-proposal-title">提议内容</h2></div>
              <span className="mono-label">HASH {summary.candidate_hash.slice(0, 12)}</span>
            </div>
            <dl className="knowledge-proposal-meta">
              <div><dt>标题</dt><dd>{snapshot.title}</dd></div>
              <div><dt>slug</dt><dd>{snapshot.slug}</dd></div>
              <div><dt>摘要</dt><dd>{snapshot.summary || "无摘要"}</dd></div>
              <div><dt>标签</dt><dd>{snapshot.tags.length > 0 ? snapshot.tags.join(" · ") : "无标签"}</dd></div>
            </dl>
            {snapshot.sections.map((section) => (
              <article className="knowledge-candidate-section__body" key={section.section_key}>
                <span className="eyebrow">SECTION {section.section_key}</span>
                <h3>{section.heading}</h3>
                <SafeMarkdown source={section.body_markdown} />
              </article>
            ))}
          </section>

          <section className="knowledge-candidate-section" aria-labelledby="knowledge-candidate-diff-title">
            <div className="knowledge-section-heading">
              <div><span className="eyebrow">STRUCTURED CHANGESET</span><h2 id="knowledge-candidate-diff-title">结构化 diff</h2></div>
            </div>
            {detail.diff ? <KnowledgeDiff diff={detail.diff} /> : <p className="knowledge-inspector-empty">这是新页面候选，没有目标 revision diff。</p>}
          </section>
        </main>

        <aside className="knowledge-inspector knowledge-candidate-inspector">
          <CandidateTarget detail={detail} />
          <KnowledgeSourceList sources={detail.sources} />
          <CandidateProvenance detail={detail} />
          <ReviewActions
            reviewable={reviewable}
            decision={decision}
            reason={reason}
            submitting={submitting}
            onBegin={beginDecision}
            onWithdraw={beginWithdraw}
            onReasonChange={setReason}
            onSubmit={(event) => void submitReview(event)}
          />
        </aside>
      </div>
    </section>
  );
}

function CandidateTarget({ detail }: { detail: KnowledgeCandidateDetail }) {
  const expected = detail.summary.expected_revision_id;
  return (
    <section className="knowledge-inspector-section" aria-labelledby="knowledge-target-title">
      <div className="knowledge-inspector-heading">
        <span className="eyebrow">TARGET FENCE</span>
        <h2 id="knowledge-target-title">目标版本</h2>
      </div>
      <dl className="knowledge-facts">
        <div><dt>操作</dt><dd>{operationLabel(detail.summary.operation)}</dd></div>
        <div><dt>Candidate version</dt><dd>{detail.summary.version}</dd></div>
        <div><dt>目标页面</dt><dd><code>{detail.summary.target_page_id ?? "新页面"}</code></dd></div>
        <div><dt>目标 revision</dt><dd>{expected ? `当前目标版本 REV ${detail.current_target_revision?.revision_number ?? "?"}` : "无目标 revision"}</dd></div>
        <div><dt>状态</dt><dd>{detail.target_is_stale ? "已过期" : "版本一致"}</dd></div>
      </dl>
      {detail.current_target_revision ? <code className="knowledge-hash">{detail.current_target_revision.content_hash}</code> : null}
    </section>
  );
}

function CandidateProvenance({ detail }: { detail: KnowledgeCandidateDetail }) {
  const provenance = detail.provenance;
  return (
    <section className="knowledge-inspector-section" aria-labelledby="knowledge-provenance-title">
      <div className="knowledge-inspector-heading">
        <span className="eyebrow">PROVENANCE</span>
        <h2 id="knowledge-provenance-title">产生上下文</h2>
      </div>
      <dl className="knowledge-facts">
        <div><dt>Producer</dt><dd>{stringValue(provenance.producer_id) ?? "未知"}</dd></div>
        <div><dt>WorkerItem</dt><dd><code>{shortId(stringValue(provenance.worker_item_id))}</code></dd></div>
        <div><dt>Execution</dt><dd><code>{shortId(stringValue(provenance.execution_id))}</code></dd></div>
        <div><dt>Candidate hash</dt><dd><code className="knowledge-hash">{detail.summary.candidate_hash}</code></dd></div>
      </dl>
    </section>
  );
}

function ReviewActions({
  reviewable,
  decision,
  reason,
  submitting,
  onBegin,
  onWithdraw,
  onReasonChange,
  onSubmit,
}: {
  reviewable: boolean;
  decision: ReviewDecision | null;
  reason: string;
  submitting: boolean;
  onBegin: (decision: ReviewDecision) => void;
  onWithdraw: () => void;
  onReasonChange: (value: string) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
}) {
  return (
    <section className="knowledge-inspector-section knowledge-review-actions" aria-label="审核操作">
      <div className="knowledge-inspector-heading">
        <span className="eyebrow">OWNER REVIEW</span>
        <h2>审核操作</h2>
      </div>
      <div className="knowledge-review-buttons">
        <button className="primary-button" type="button" disabled={!reviewable || submitting} onClick={() => onBegin("accept")}>
          <Check size={14} aria-hidden="true" />接受候选
        </button>
        <button className="quiet-button" type="button" disabled={!reviewable || submitting} onClick={() => onBegin("request_changes")}>
          请求修改
        </button>
        <button className="quiet-button quiet-button--danger" type="button" disabled={!reviewable || submitting} onClick={() => onBegin("reject")}>
          <CircleX size={14} aria-hidden="true" />拒绝候选
        </button>
        <button className="quiet-button" type="button" disabled={!reviewable || submitting} onClick={onWithdraw}>
          <Undo2 size={14} aria-hidden="true" />撤回候选
        </button>
      </div>
      {decision !== null ? (
        <form className="knowledge-review-form" onSubmit={onSubmit}>
          <label className="knowledge-field">
            <span>审核原因</span>
            <textarea value={reason} onChange={(event) => onReasonChange(event.target.value)} rows={5} aria-label="审核原因" />
          </label>
          <button className="primary-button" type="submit" disabled={submitting}>
            {submitting ? "提交中" : reviewActionLabel(decision)}
          </button>
        </form>
      ) : null}
    </section>
  );
}

function CandidateState({ label, actionLabel, onAction }: { label: string; actionLabel?: string; onAction?: () => void }) {
  return (
    <section className="knowledge-detail-state" role={actionLabel ? "alert" : "status"}>
      <span className="eyebrow">KNOWLEDGE CANDIDATE</span>
      <strong>{label}</strong>
      {actionLabel && onAction ? <button className="quiet-button" type="button" onClick={onAction}>{actionLabel}</button> : null}
    </section>
  );
}

function normalizeSnapshot(value: Record<string, unknown>, summary: KnowledgeCandidateDetail["summary"]): KnowledgeProposedSnapshot {
  const sections = Array.isArray(value.sections) ? value.sections.flatMap((item, index) => {
    if (!isRecord(item)) return [];
    return [{
      section_key: stringValue(item.section_key) ?? `section-${index + 1}`,
      heading: stringValue(item.heading) ?? stringValue(item.section_key) ?? `Section ${index + 1}`,
      summary: stringValue(item.summary) ?? "",
      body_markdown: stringValue(item.body_markdown) ?? "",
      ordinal: numberValue(item.ordinal) ?? index,
      section_hash: stringValue(item.section_hash) ?? "",
      content_byte_count: numberValue(item.content_byte_count) ?? 0,
    }];
  }) : [];
  return {
    title: stringValue(value.title) ?? summary.proposed_title,
    slug: stringValue(value.slug) ?? summary.proposed_slug,
    summary: stringValue(value.summary) ?? "",
    tags: Array.isArray(value.tags) ? value.tags.filter((item): item is string => typeof item === "string") : [],
    sections,
    relations: [],
    source_refs: [],
  };
}

function commandResult(response: unknown): Record<string, unknown> {
  if (isRecord(response) && isRecord(response.result)) {
    return response.result;
  }
  return isRecord(response) ? response : {};
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function numberValue(value: unknown): number | null {
  return typeof value === "number" ? value : null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function shortId(value: string | null): string {
  return value === null ? "未关联" : value.slice(0, 8);
}

function operationLabel(value: string): string {
  return value === "create_page" ? "新页面" : "修改页面";
}

function candidateStatusLabel(value: string): string {
  switch (value) {
    case "pending": return "待审核";
    case "accepted": return "已接受";
    case "rejected": return "已拒绝";
    case "changes_requested": return "已请求修改";
    case "stale": return "已过期";
    case "withdrawn": return "已撤回";
    default: return value;
  }
}

function reviewActionLabel(decision: ReviewDecision): string {
  switch (decision) {
    case "accept": return "提交接受";
    case "reject": return "提交拒绝";
    case "request_changes": return "提交请求修改";
  }
}
