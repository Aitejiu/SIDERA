import { useQueries, useQuery } from "@tanstack/react-query";
import { AlertTriangle, BookOpen, RefreshCcw } from "lucide-react";
import { Link } from "react-router-dom";

import {
  getWorkContextByRootTask,
  listTaskKnowledgeSubmissions,
  taskKnowledgeSubmissionsQueryKey,
  workContextByRootTaskQueryKey,
} from "./api";
import type { ContextPackageKnowledgeRef, KnowledgeSubmission, WorkContextKnowledgeRef } from "./types";
import { getKnowledgePage, knowledgeRevisionQueryKey, listKnowledgeCandidates, knowledgeCandidatesQueryKey } from "../knowledge/api";
import type { KnowledgeCandidateSummary, KnowledgeRevision } from "../knowledge/types";

const REVIEW_STATUSES = new Set(["pending", "changes_requested", "stale"]);

export function TaskKnowledgeSummary({
  workspaceId,
  rootTaskId,
  submissionTaskId = rootTaskId,
  sourceSubtaskId,
}: {
  workspaceId: string;
  rootTaskId: string;
  submissionTaskId?: string;
  sourceSubtaskId?: string | null;
}) {
  const contextQuery = useQuery({
    queryKey: workContextByRootTaskQueryKey(workspaceId, rootTaskId),
    queryFn: ({ signal }) => getWorkContextByRootTask(workspaceId, rootTaskId, signal),
    enabled: Boolean(workspaceId && rootTaskId),
    retry: false,
  });
  const submissionQuery = useQuery({
    queryKey: taskKnowledgeSubmissionsQueryKey(workspaceId, submissionTaskId),
    queryFn: ({ signal }) => listTaskKnowledgeSubmissions(workspaceId, submissionTaskId, signal),
    enabled: Boolean(workspaceId && submissionTaskId),
    retry: false,
  });
  const candidateQuery = useQuery({
    queryKey: knowledgeCandidatesQueryKey(workspaceId, {
      source_task_id: rootTaskId,
      source_subtask_id: sourceSubtaskId ?? undefined,
    }),
    queryFn: ({ signal }) => listKnowledgeCandidates(workspaceId, {
      sourceTaskId: rootTaskId,
      ...(sourceSubtaskId ? { sourceSubtaskId } : {}),
      limit: 50,
      signal,
    }),
    enabled: Boolean(workspaceId && rootTaskId),
    retry: false,
  });

  const knowledgeRefs = contextQuery.data?.current_revision.snapshot.knowledge_refs ?? [];
  const refQueries = useQueries({
    queries: knowledgeRefs.slice(0, 3).map((ref) => ({
      queryKey: knowledgeRevisionQueryKey(workspaceId, ref.page_id, ref.revision_id),
      queryFn: ({ signal }: { signal: AbortSignal }) => getKnowledgePage(
        workspaceId,
        ref.page_id,
        ref.revision_id,
        signal,
      ),
      enabled: Boolean(workspaceId && ref.page_id && ref.revision_id),
      retry: false,
    })),
  });

  const titles = new Map<string, string>();
  knowledgeRefs.slice(0, 3).forEach((ref, index) => {
    const revision = refQueries[index]?.data as KnowledgeRevision | undefined;
    if (revision !== undefined) {
      titles.set(refKey(ref), revision.title);
    }
  });
  const candidates = candidateQuery.data?.items ?? [];
  const reviewCandidates = candidates.filter((candidate) => REVIEW_STATUSES.has(candidate.status));
  const submissions = latestSubmissions(submissionQuery.data?.items ?? []);
  const linkedCount = knowledgeRefs.length;
  const candidateCounts = {
    pending: countStatus(candidates, "pending"),
    changes_requested: countStatus(candidates, "changes_requested"),
    stale: countStatus(candidates, "stale"),
  };
  const inboxHref = knowledgeInboxHref(workspaceId, rootTaskId, sourceSubtaskId);

  return (
    <section className="task-knowledge-summary" aria-label="知识上下文">
      <div className="task-knowledge-summary__heading">
        <div>
          <span className="eyebrow">KNOWLEDGE CONTEXT</span>
          <h2>知识上下文</h2>
        </div>
        <Link className="quiet-button" to={inboxHref}>
          <BookOpen size={14} aria-hidden="true" />查看全部知识候选
        </Link>
      </div>

      <div className="task-knowledge-summary__stats">
        <div><span>固定引用</span><strong>{contextQuery.isError ? "--" : `${linkedCount} 个固定引用`}</strong></div>
        <div><span>待审核</span><strong>{candidateQuery.isError ? "--" : `待审核 ${candidateCounts.pending}`}</strong></div>
        <div><span>需修改</span><strong>{candidateQuery.isError ? "--" : `需修改 ${candidateCounts.changes_requested}`}</strong></div>
        <div><span>已过期</span><strong>{candidateQuery.isError ? "--" : `已过期 ${candidateCounts.stale}`}</strong></div>
      </div>

      {contextQuery.isError ? (
        <div className="task-knowledge-summary__notice task-knowledge-summary__notice--error" role="alert">
          <AlertTriangle size={14} aria-hidden="true" />
          <span>工作上下文暂不可用，固定引用无法读取。</span>
          <button className="quiet-button" type="button" onClick={() => void contextQuery.refetch()}>
            <RefreshCcw size={13} aria-hidden="true" />重试读取上下文
          </button>
        </div>
      ) : null}

      {contextQuery.isPending ? <p className="task-knowledge-summary__muted" role="status">读取固定知识引用</p> : null}
      {!contextQuery.isPending && !contextQuery.isError && knowledgeRefs.length === 0 ? (
        <p className="task-knowledge-summary__muted">当前没有固定知识引用</p>
      ) : null}
      {knowledgeRefs.length > 0 ? (
        <ul className="task-knowledge-summary__refs">
          {knowledgeRefs.slice(0, 3).map((ref) => (
            <li key={refKey(ref)}>
              <strong>{titles.get(refKey(ref)) ?? `知识页 ${shortId(ref.page_id)}`}</strong>
              <span>R{revisionNumber(ref)} · {ref.section_key} · {shortHash(ref.revision_hash)}</span>
            </li>
          ))}
          {knowledgeRefs.length > 3 ? <li className="task-knowledge-summary__more">还有 {knowledgeRefs.length - 3} 个固定引用</li> : null}
        </ul>
      ) : null}

      {candidateQuery.isError ? (
        <div className="task-knowledge-summary__notice" role="status">
          <span>知识服务暂不可用，候选统计未更新。</span>
          <button className="quiet-button" type="button" onClick={() => void candidateQuery.refetch()}>重试知识审核</button>
        </div>
      ) : reviewCandidates.length > 0 ? (
        <div className="task-knowledge-summary__candidates">
          <span className="eyebrow">REVIEW SIGNALS</span>
          {reviewCandidates.slice(0, 3).map((candidate) => (
            <Link
              key={candidate.candidate_id}
              to={`/workspaces/${workspaceId}/knowledge/candidates/${encodeURIComponent(candidate.candidate_id)}`}
            >
              <strong>{candidate.proposed_title}</strong>
              <span>{candidateStatusLabel(candidate.status)} · {candidateOperationLabel(candidate.operation)}</span>
            </Link>
          ))}
        </div>
      ) : null}

      {submissionQuery.isError ? (
        <div className="task-knowledge-summary__notice" role="status">
          <span>知识投递状态暂不可用。</span>
          <button className="quiet-button" type="button" onClick={() => void submissionQuery.refetch()}>重试读取投递</button>
        </div>
      ) : submissions.length > 0 ? (
        <div className="task-knowledge-summary__delivery" role="status">
          <span className="eyebrow">DELIVERY</span>
          {submissions.map((submission) => (
            <span key={submission.submission_id}>
              <span>{submissionLabel(submission.status)} · 第 {submission.attempt} 次尝试</span>
              {submission.error_code ? <small> · {submission.error_code}</small> : null}
            </span>
          ))}
        </div>
      ) : null}
    </section>
  );
}

function latestSubmissions(items: readonly KnowledgeSubmission[]): KnowledgeSubmission[] {
  return [...items].sort((left, right) => Date.parse(right.created_at) - Date.parse(left.created_at)).slice(0, 3);
}

function countStatus(items: readonly KnowledgeCandidateSummary[], status: string): number {
  return items.filter((item) => item.status === status).length;
}

function revisionNumber(ref: WorkContextKnowledgeRef | ContextPackageKnowledgeRef): string {
  return shortId(ref.revision_id);
}

function refKey(ref: WorkContextKnowledgeRef | ContextPackageKnowledgeRef): string {
  return `${ref.page_id}:${ref.revision_id}:${ref.section_key}`;
}

function shortId(value: string): string {
  return value.slice(0, 8);
}

function shortHash(value: string): string {
  return value.slice(0, 8);
}

function knowledgeInboxHref(workspaceId: string, rootTaskId: string, sourceSubtaskId?: string | null): string {
  const query = new URLSearchParams({ source_task_id: rootTaskId });
  if (sourceSubtaskId) query.set("source_subtask_id", sourceSubtaskId);
  return `/workspaces/${encodeURIComponent(workspaceId)}/knowledge/inbox?${query.toString()}`;
}

function candidateStatusLabel(status: string): string {
  if (status === "pending") return "待审核";
  if (status === "changes_requested") return "需修改";
  if (status === "stale") return "已过期";
  return status;
}

function candidateOperationLabel(operation: string): string {
  return operation === "revise_page" ? "修改页面" : operation === "create_page" ? "新页面" : operation;
}

function submissionLabel(status: string): string {
  if (status === "retry_wait") return "投递等待重试";
  if (status === "queued") return "投递排队中";
  if (status === "submitted" || status === "succeeded") return "投递已提交";
  if (status === "failed") return "投递失败";
  return `投递 ${status}`;
}
