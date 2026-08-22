import { ArrowLeft, GitCompareArrows, History } from "lucide-react";
import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { AsyncState } from "@/shared/ui/AsyncState";

import { listPlanGenerations, planGenerationsQueryKey } from "./api";

export function PlanGenerationPage() {
  const { workspaceId, rootTaskId, generation } = useParams<{
    workspaceId: string;
    rootTaskId: string;
    generation: string;
  }>();
  const resolvedWorkspaceId = workspaceId ?? "";
  const resolvedRootTaskId = rootTaskId ?? "";
  const selectedGeneration = Number(generation);
  const query = useQuery({
    queryKey: planGenerationsQueryKey(resolvedWorkspaceId, resolvedRootTaskId),
    queryFn: ({ signal }) => listPlanGenerations(resolvedWorkspaceId, resolvedRootTaskId, signal),
    enabled: resolvedWorkspaceId.length > 0 && resolvedRootTaskId.length > 0,
  });
  const currentGeneration = query.data?.[0]?.generation ?? selectedGeneration;
  const selected = query.data?.find((item) => item.generation === selectedGeneration);

  return (
    <section className="plan-page" aria-labelledby="plan-generation-title">
      <div className="breadcrumb">
        <Link to={`/workspaces/${resolvedWorkspaceId}/tasks/${resolvedRootTaskId}`}><ArrowLeft size={13} aria-hidden="true" />任务</Link>
        <span>/</span>计划历史
      </div>
      <div className="page-heading plan-page__heading">
        <div>
          <span className="eyebrow">IMMUTABLE PLAN HISTORY</span>
          <h1 id="plan-generation-title">计划 Generation {selectedGeneration}</h1>
          <p>历史快照只读保存，当前调度只接受最新已发布 Generation。</p>
        </div>
        <span className="mono-label">CURRENT GEN {currentGeneration}</span>
      </div>
      <AsyncState
        isLoading={query.isPending}
        isError={query.isError}
        errorMessage="无法读取计划历史"
        retryLabel="重试读取计划"
        onRetry={() => void query.refetch()}
      >
        {selected ? (
          <>
            <div className="plan-generation-summary">
              <div><span className="eyebrow">TITLE</span><strong>{selected.title}</strong></div>
              <div><span className="eyebrow">PUBLISHED BY</span><span>{selected.published_by}</span></div>
              <div><span className="eyebrow">WORK ITEMS</span><strong>{selected.work_items.length}</strong></div>
              <div><span className="eyebrow">WORKER SLOTS</span><strong>{selected.worker_slots.length}</strong></div>
            </div>
            <div className="plan-generation-list">
              {query.data?.map((item) => (
                <div className={`plan-generation-row${item.generation === selectedGeneration ? " is-selected" : ""}`} key={item.id}>
                  <Link to={`/workspaces/${resolvedWorkspaceId}/tasks/${resolvedRootTaskId}/plan/${item.generation}`}>
                    <History size={14} aria-hidden="true" />Generation {item.generation}
                  </Link>
                  <span>{item.title}</span>
                  <span className="mono-label">{item.work_items.length} WORK ITEMS</span>
                  {item.generation !== currentGeneration ? (
                    <Link className="quiet-button" to={`/workspaces/${resolvedWorkspaceId}/tasks/${resolvedRootTaskId}/compare/${item.generation}/${currentGeneration}`}>
                      <GitCompareArrows size={14} aria-hidden="true" />比较当前
                    </Link>
                  ) : <span className="plan-generation-current">当前</span>}
                </div>
              ))}
            </div>
          </>
        ) : <p className="muted-copy">找不到这个计划 Generation。</p>}
      </AsyncState>
    </section>
  );
}
