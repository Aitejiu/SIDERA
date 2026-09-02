import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { ArrowLeft, History, Radio } from "lucide-react";
import { useMemo } from "react";
import { Link, useParams } from "react-router-dom";

import { AsyncState } from "@/shared/ui/AsyncState";
import { StatusMark } from "@/shared/ui/StatusMark";

import {
  collaborationDecisionsQueryKey,
  collaborationDetailQueryKey,
  getCollaborationDecisions,
  getCollaborationDetail,
} from "./api";
import { collaborationStatus, formatStrategy } from "./CollaborationSummary";
import { DecisionHistory } from "./DecisionHistory";
import type { CollaborationDecision, CollaborationDetail } from "./types";

export function CollaborationDetailPage() {
  const {
    workspaceId = "",
    rootTaskId = "",
  } = useParams<{ workspaceId: string; rootTaskId: string }>();

  const detailQuery = useQuery({
    queryKey: collaborationDetailQueryKey(workspaceId, rootTaskId),
    queryFn: ({ signal }) => getCollaborationDetail(workspaceId, rootTaskId, signal),
    enabled: Boolean(workspaceId && rootTaskId),
  });
  const decisionsQuery = useInfiniteQuery({
    queryKey: collaborationDecisionsQueryKey(workspaceId, rootTaskId),
    initialPageParam: undefined as string | undefined,
    queryFn: ({ signal, pageParam }) => getCollaborationDecisions(
      workspaceId,
      rootTaskId,
      {
        ...(pageParam === undefined ? {} : { cursor: pageParam }),
        signal,
      },
    ),
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    enabled: Boolean(workspaceId && rootTaskId),
  });
  const decisions = useMemo(
    () => decisionsQuery.data?.pages.flatMap((page) => page.items) ?? [],
    [decisionsQuery.data?.pages],
  );

  return (
    <section className="collaboration-detail-page" aria-labelledby="collaboration-detail-title">
      <AsyncState
        isLoading={detailQuery.isPending}
        isError={detailQuery.isError}
        errorMessage="无法读取协同记录"
        retryLabel="重试读取协同记录"
        onRetry={() => void detailQuery.refetch()}
      >
        {detailQuery.data ? (
          <CollaborationDetailContent
            detail={detailQuery.data}
            decisions={decisions}
            decisionsLoading={decisionsQuery.isPending}
            decisionsError={decisionsQuery.isError}
            hasNextPage={decisionsQuery.hasNextPage === true}
            isLoadingNextPage={decisionsQuery.isFetchingNextPage}
            onLoadMore={() => void decisionsQuery.fetchNextPage()}
            onRetryDecisions={() => void decisionsQuery.refetch()}
            workspaceId={workspaceId}
            rootTaskId={rootTaskId}
          />
        ) : null}
      </AsyncState>
    </section>
  );
}

function CollaborationDetailContent({
  detail,
  decisions,
  decisionsLoading,
  decisionsError,
  hasNextPage,
  isLoadingNextPage,
  onLoadMore,
  onRetryDecisions,
  workspaceId,
  rootTaskId,
}: {
  detail: CollaborationDetail;
  decisions: readonly CollaborationDecision[];
  decisionsLoading: boolean;
  decisionsError: boolean;
  hasNextPage: boolean;
  isLoadingNextPage: boolean;
  onLoadMore: () => void;
  onRetryDecisions: () => void;
  workspaceId: string;
  rootTaskId: string;
}) {
  const status = collaborationStatus(detail.status);
  return (
    <>
      <div className="breadcrumb collaboration-detail-page__breadcrumb">
        <Link to={`/workspaces/${workspaceId}/tasks`}><ArrowLeft size={13} aria-hidden="true" />任务</Link>
        <span aria-hidden="true"> / </span>
        <Link to={`/workspaces/${workspaceId}/tasks/${rootTaskId}`}>返回任务</Link>
        <span aria-hidden="true"> / 协同记录</span>
      </div>
      <header className="collaboration-detail-heading">
        <div>
          <span className="eyebrow">COLLABORATION LOG · GEN {detail.plan_generation}</span>
          <h1 id="collaboration-detail-title">协同记录</h1>
          <p>当前计划代的调度判断与放行结果。</p>
        </div>
        <div className="collaboration-detail-heading__meta">
          <StatusMark kind={status.kind} label={status.label} />
          <span className="mono-label">策略 · {formatStrategy(detail.strategy_label)}</span>
          <span className="mono-label">{detail.provider}</span>
        </div>
      </header>

      <div className="collaboration-detail-facts" aria-label="协同概览">
        <DetailFact label="已放行" value={detail.released_count} />
        <DetailFact label="执行中" value={detail.running_count} />
        <DetailFact label="等待中" value={detail.waiting_count} />
        <DetailFact label="已完成" value={detail.completed_count} />
      </div>

      <div className="collaboration-detail-context">
        <span><History size={14} aria-hidden="true" />计划代 {detail.plan_generation}</span>
        <span><Radio size={14} aria-hidden="true" />{detail.interface_version}</span>
        <span>{detail.config_summary}</span>
        {detail.latest_decision_summary ? <strong>{detail.latest_decision_summary}</strong> : null}
      </div>

      <DecisionHistory
        decisions={decisions}
        isLoading={decisionsLoading}
        isError={decisionsError}
        hasNextPage={hasNextPage}
        isLoadingNextPage={isLoadingNextPage}
        onLoadMore={onLoadMore}
        onRetry={onRetryDecisions}
      />
    </>
  );
}

function DetailFact({ label, value }: { label: string; value: number }) {
  return (
    <div className="collaboration-detail-fact">
      <span className="eyebrow">{label}</span>
      <strong>{value}</strong>
    </div>
  );
}
