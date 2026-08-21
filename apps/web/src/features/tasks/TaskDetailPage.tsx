import { useEffect, useMemo, useState } from "react";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { Link, useParams, useSearchParams } from "react-router-dom";

import { AsyncState } from "@/shared/ui/AsyncState";

import {
  getTaskActivity,
  getTaskDetail,
  getTaskGraph,
  taskActivityQueryKey,
  taskDetailQueryKey,
  taskGraphQueryKey,
} from "./api";
import { ExecutionHorizon } from "./ExecutionHorizon";
import { MilestoneTrack } from "./MilestoneTrack";
import { TaskActivity } from "./TaskActivity";
import { TaskGraph } from "./TaskGraph";
import { TaskInspector } from "./TaskInspector";
import { TaskLedger } from "./TaskLedger";

export function TaskDetailPage() {
  const { workspaceId, rootTaskId } = useParams<{ workspaceId: string; rootTaskId: string }>();
  const resolvedWorkspaceId = workspaceId ?? "";
  const resolvedRootTaskId = rootTaskId ?? "";
  const [searchParams, setSearchParams] = useSearchParams();
  const selectedFromUrl = searchParams.get("workItem");
  const [viewMode, setViewMode] = useState<"both" | "dag" | "ledger">(() =>
    typeof window !== "undefined" && window.innerWidth < 640 ? "ledger" : "both",
  );

  const graphQuery = useQuery({
    queryKey: taskGraphQueryKey(resolvedWorkspaceId, resolvedRootTaskId),
    queryFn: ({ signal }) => getTaskGraph(resolvedWorkspaceId, resolvedRootTaskId, signal),
    enabled: resolvedWorkspaceId.length > 0 && resolvedRootTaskId.length > 0,
  });
  const activityQuery = useInfiniteQuery({
    queryKey: taskActivityQueryKey(resolvedWorkspaceId, resolvedRootTaskId),
    initialPageParam: undefined as string | undefined,
    queryFn: ({ signal, pageParam }) => getTaskActivity(
      resolvedWorkspaceId,
      resolvedRootTaskId,
      pageParam === undefined ? { signal } : { after: pageParam, signal },
    ),
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    enabled: resolvedWorkspaceId.length > 0 && resolvedRootTaskId.length > 0,
  });
  const activity = useMemo(() => {
    const pages = activityQuery.data?.pages ?? [];
    const [firstPage] = pages;
    if (!firstPage) {
      return undefined;
    }
    return {
      ...firstPage,
      items: pages.flatMap((page) => page.items),
      next_cursor: pages.at(-1)?.next_cursor ?? null,
    };
  }, [activityQuery.data?.pages]);
  const preferredWorkItemKey = useMemo(() => {
    const nodes = graphQuery.data?.nodes;
    if (!nodes || nodes.length === 0) {
      return null;
    }
    if (selectedFromUrl !== null && nodes.some((node) => node.work_item_key === selectedFromUrl)) {
      return selectedFromUrl;
    }
    return nodes.find((node) => node.execution_horizon && node.state !== "planned")?.work_item_key ?? nodes[0]?.work_item_key ?? null;
  }, [graphQuery.data?.nodes, selectedFromUrl]);
  const selectedWorkItemKey = preferredWorkItemKey;
  const detailQuery = useQuery({
    queryKey: selectedWorkItemKey
      ? taskDetailQueryKey(resolvedWorkspaceId, resolvedRootTaskId, selectedWorkItemKey)
      : ["task-detail", resolvedWorkspaceId, resolvedRootTaskId, "none"],
    queryFn: ({ signal }) => getTaskDetail(resolvedWorkspaceId, resolvedRootTaskId, selectedWorkItemKey ?? "", signal),
    enabled: selectedWorkItemKey !== null,
  });

  useEffect(() => {
    if (preferredWorkItemKey === null || selectedFromUrl === preferredWorkItemKey) {
      return;
    }
    setSearchParams({ workItem: preferredWorkItemKey }, { replace: true });
  }, [preferredWorkItemKey, selectedFromUrl, setSearchParams]);

  const selectWorkItem = (workItemKey: string) => {
    setSearchParams({ workItem: workItemKey });
  };

  return (
    <section className="task-detail-page" aria-labelledby="task-detail-title">
      <div className="breadcrumb">
        <Link to={`/workspaces/${resolvedWorkspaceId}/tasks`}>任务</Link> / {graphQuery.data?.overview.title ?? "任务详情"}
      </div>
      <AsyncState
        isLoading={graphQuery.isPending}
        isError={graphQuery.isError}
        errorMessage="无法读取任务图"
        retryLabel="重试读取任务图"
        onRetry={() => void graphQuery.refetch()}
      >
        {graphQuery.data ? (
          <>
            <div className="task-detail-heading">
              <div>
                <span className="eyebrow">TASK GRAPH · GEN {graphQuery.data.overview.generation}</span>
                <h1 id="task-detail-title">{graphQuery.data.overview.title}</h1>
                <p>{graphQuery.data.overview.goal_summary}</p>
              </div>
              <span className="mono-label">CURSOR {graphQuery.data.overview.last_change_cursor}</span>
            </div>

            <div className="task-detail-layout">
              <div className="task-detail-main">
                <MilestoneTrack milestones={graphQuery.data.milestones} />
                <ExecutionHorizon graph={graphQuery.data} />
                <div className="graph-ledger-switch" role="tablist" aria-label="图与任务表视图">
                  <button className={viewMode === "dag" ? "is-active" : ""} type="button" role="tab" aria-selected={viewMode === "dag"} onClick={() => setViewMode("dag")}>DAG</button>
                  <button className={viewMode === "ledger" ? "is-active" : ""} type="button" role="tab" aria-selected={viewMode === "ledger"} onClick={() => setViewMode("ledger")}>任务表</button>
                  <button className={viewMode === "both" ? "is-active" : ""} type="button" role="tab" aria-selected={viewMode === "both"} onClick={() => setViewMode("both")}>全部</button>
                </div>
                {viewMode !== "ledger" ? <TaskGraph graph={graphQuery.data} selectedWorkItemKey={selectedWorkItemKey} onSelect={selectWorkItem} /> : null}
                {viewMode !== "dag" ? <TaskLedger nodes={graphQuery.data.nodes} selectedWorkItemKey={selectedWorkItemKey} onSelect={selectWorkItem} /> : null}
                <TaskActivity
                  activity={activity}
                  onLoadMore={() => void activityQuery.fetchNextPage()}
                  isLoadingMore={activityQuery.isFetchingNextPage}
                  isLoading={activityQuery.isPending}
                  isError={activityQuery.isError}
                  onRetry={() => void activityQuery.refetch()}
                />
              </div>
              <TaskInspector
                detail={detailQuery.data}
                isLoading={detailQuery.isPending}
                isError={detailQuery.isError}
                workspaceId={resolvedWorkspaceId}
                rootTaskId={resolvedRootTaskId}
                onRetry={() => void detailQuery.refetch()}
              />
            </div>
          </>
        ) : null}
      </AsyncState>
    </section>
  );
}
