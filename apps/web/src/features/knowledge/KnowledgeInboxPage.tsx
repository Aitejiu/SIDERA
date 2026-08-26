import { RefreshCw, Search, X } from "lucide-react";
import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { listKnowledgeCandidates, knowledgeCandidatesQueryKey } from "./api";
import type { KnowledgeCandidateSummary } from "./types";
import { KnowledgeSectionTabs, boundedCount } from "./KnowledgeSectionTabs";

const DEFAULT_WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const REVIEW_STATUSES = new Set(["pending", "changes_requested", "stale"]);

type CandidateStatusFilter = "attention" | "all" | "pending" | "changes_requested" | "stale";

export function KnowledgeInboxPage() {
  const workspaceId = useParams<{ workspaceId: string }>().workspaceId ?? DEFAULT_WORKSPACE_ID;
  const [status, setStatus] = useState<CandidateStatusFilter>("attention");
  const [search, setSearch] = useState("");
  const [sourceTask, setSourceTask] = useState("");
  const query = useQuery({
    queryKey: knowledgeCandidatesQueryKey(workspaceId, { limit: "100" }),
    queryFn: ({ signal }) => listKnowledgeCandidates(workspaceId, { limit: 100, signal }),
    enabled: workspaceId.length > 0,
    retry: false,
  });
  const candidates = query.data?.items;
  const pendingCount = candidates?.filter((candidate) => candidate.status === "pending").length ?? 0;
  const visibleCandidates = useMemo(() => {
    const items = candidates ?? [];
    const normalizedSearch = search.trim().toLowerCase();
    const normalizedTask = sourceTask.trim().toLowerCase();
    return items.filter((candidate) => {
      if (status === "attention" && !REVIEW_STATUSES.has(candidate.status)) return false;
      if (status !== "attention" && status !== "all" && candidate.status !== status) return false;
      if (normalizedSearch && !candidateMatches(candidate, normalizedSearch)) return false;
      if (normalizedTask && !(candidate.task_id ?? "").toLowerCase().includes(normalizedTask)) return false;
      return true;
    });
  }, [candidates, search, sourceTask, status]);

  const clearFilters = () => {
    setStatus("attention");
    setSearch("");
    setSourceTask("");
  };

  return (
    <section className="knowledge-page" aria-labelledby="knowledge-inbox-title">
      <div className="breadcrumb">工作区 / 知识 / 收件箱</div>
      <div className="page-heading">
        <div>
          <span className="eyebrow">KNOWLEDGE REVIEW QUEUE</span>
          <h1 id="knowledge-inbox-title">知识收件箱</h1>
        </div>
        <button className="icon-button" type="button" onClick={() => void query.refetch()} aria-label="刷新知识候选" title="刷新知识候选">
          <RefreshCw size={16} aria-hidden="true" />
        </button>
      </div>

      <KnowledgeSectionTabs pendingCount={pendingCount} />
      <div className="knowledge-toolbar knowledge-inbox-toolbar" aria-label="知识候选筛选">
        <label className="knowledge-search-field">
          <span className="sr-only">搜索候选</span>
          <Search size={16} aria-hidden="true" />
          <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索候选标题或 worker" aria-label="搜索候选" />
        </label>
        <div className="knowledge-filter-fields">
          <label className="knowledge-filter-field">
            <span>状态</span>
            <select value={status} onChange={(event) => setStatus(event.target.value as CandidateStatusFilter)} aria-label="候选状态筛选">
              <option value="attention">待处理</option>
              <option value="all">全部状态</option>
              <option value="pending">待审核</option>
              <option value="changes_requested">需修改</option>
              <option value="stale">已过期</option>
            </select>
          </label>
          <label className="knowledge-filter-field knowledge-filter-field--wide">
            <span>来源任务</span>
            <input value={sourceTask} onChange={(event) => setSourceTask(event.target.value)} placeholder="任务 ID" aria-label="来源任务筛选" />
          </label>
          <button
            className="icon-button knowledge-filter-clear"
            type="button"
            onClick={clearFilters}
            disabled={status === "attention" && search === "" && sourceTask === ""}
            aria-label="清除候选筛选"
            title="清除候选筛选"
          >
            <X size={16} aria-hidden="true" />
          </button>
        </div>
      </div>

      <div className="knowledge-results-line" aria-live="polite">
        <span className="mono-label">REVIEW QUEUE · {visibleCandidates.length} 项</span>
        <span className="knowledge-pending-count">待审核 {boundedCount(pendingCount)}</span>
      </div>

      {query.isPending ? <KnowledgeCandidateSkeleton /> : null}
      {query.isError ? (
        <div className="async-state async-state--error knowledge-async-state" role="alert">
          <span className="async-state__mark" aria-hidden="true">!</span>
          <div><strong>无法读取知识候选</strong><p>知识服务暂时没有返回审核队列。</p></div>
          <button className="quiet-button" type="button" onClick={() => void query.refetch()}>重试读取候选</button>
        </div>
      ) : null}
      {!query.isPending && !query.isError && visibleCandidates.length === 0 ? (
        <div className="knowledge-empty" role="status">
          <span className="eyebrow">QUEUE CLEAR</span>
          <strong>当前没有匹配的候选</strong>
        </div>
      ) : null}
      {!query.isPending && !query.isError && visibleCandidates.length > 0 ? (
        <CandidateTable workspaceId={workspaceId} candidates={visibleCandidates} />
      ) : null}
    </section>
  );
}

function CandidateTable({ workspaceId, candidates }: { workspaceId: string; candidates: KnowledgeCandidateSummary[] }) {
  return (
    <div className="knowledge-table knowledge-candidate-table" role="table" aria-label="知识候选列表">
      <div className="knowledge-table__header" role="row" aria-hidden="true">
        <span>候选</span><span>动作</span><span>来源任务</span><span>状态</span><span>产生时间</span>
      </div>
      <div className="knowledge-table__body">
        {candidates.map((candidate) => (
          <Link
            className="knowledge-table__row"
            key={candidate.candidate_id}
            to={`/workspaces/${workspaceId}/knowledge/candidates/${encodeURIComponent(candidate.candidate_id)}`}
            aria-label={`打开候选 ${candidate.proposed_title}`}
          >
            <span className="knowledge-table__cell knowledge-table__cell--identity" data-label="候选">
              <strong>{candidate.proposed_title}</strong>
              <span>{candidate.proposed_slug}</span>
              <span className="mono-label">{candidate.candidate_id.slice(0, 8)} · V{candidate.version}</span>
            </span>
            <span className="knowledge-table__cell" data-label="动作">{candidateOperationLabel(candidate.operation)}</span>
            <span className="knowledge-table__cell knowledge-table__cell--task" data-label="来源任务">
              <span className="knowledge-breadcrumb">任务 {shortId(candidate.task_id)} / 子任务 {shortId(candidate.subtask_id)}</span>
              <span className="knowledge-secondary-meta">{candidate.producer_id}{candidate.execution_id ? ` · 执行 ${shortId(candidate.execution_id)}` : ""}</span>
            </span>
            <span className="knowledge-table__cell" data-label="状态">
              <span className={`knowledge-status knowledge-status--${candidate.status}`}>{candidateStatusLabel(candidate.status)}</span>
            </span>
            <span className="knowledge-table__cell knowledge-table__cell--updated" data-label="产生时间">{formatKnowledgeDate(candidate.created_at)}</span>
          </Link>
        ))}
      </div>
    </div>
  );
}

function KnowledgeCandidateSkeleton() {
  return (
    <div className="knowledge-table knowledge-table--loading" aria-label="正在加载知识候选">
      <div className="knowledge-table__header" aria-hidden="true"><span>候选</span><span>动作</span><span>来源任务</span><span>状态</span><span>产生时间</span></div>
      <div className="knowledge-table__body">{Array.from({ length: 3 }, (_, index) => <div className="knowledge-table__row knowledge-table__row--skeleton" data-testid="knowledge-candidate-skeleton" key={index} aria-hidden="true"><span /><span /><span /><span /><span /></div>)}</div>
    </div>
  );
}

function candidateMatches(candidate: KnowledgeCandidateSummary, query: string): boolean {
  return [candidate.proposed_title, candidate.proposed_slug, candidate.producer_id, candidate.status]
    .some((value) => value.toLowerCase().includes(query));
}

function candidateOperationLabel(operation: string): string {
  return operation === "revise_page" ? "修改页面" : operation === "create_page" ? "新页面" : operation;
}

function candidateStatusLabel(status: string): string {
  if (status === "pending") return "待审核";
  if (status === "changes_requested") return "需修改";
  if (status === "stale") return "已过期";
  if (status === "accepted") return "已接受";
  if (status === "rejected") return "已拒绝";
  return status || "未知";
}

function shortId(value: string | null): string {
  return value === null ? "未绑定" : value.slice(0, 8);
}

function formatKnowledgeDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "未知";
  return date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}
