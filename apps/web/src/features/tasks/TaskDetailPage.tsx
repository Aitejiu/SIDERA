import { useEffect, useMemo, useState } from "react";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { History } from "lucide-react";
import { Link, useParams, useSearchParams } from "react-router-dom";

import { AsyncState } from "@/shared/ui/AsyncState";
import { AttentionPanel } from "@/features/attention/AttentionPanel";

import {
  getTaskActivity,
  getTaskDetail,
  getTaskGraph,
  getTask,
  taskActivityQueryKey,
  taskDetailQueryKey,
  taskGraphQueryKey,
  taskQueryKey,
} from "./api";
import { ExecutionHorizon } from "./ExecutionHorizon";
import { MilestoneTrack } from "./MilestoneTrack";
import { TaskActivity } from "./TaskActivity";
import { TaskBoard } from "./TaskBoard";
import { TaskGraph } from "./TaskGraph";
import { TaskInspector } from "./TaskInspector";
import { TaskLedger } from "./TaskLedger";

export function TaskDetailPage() {
  const { workspaceId, rootTaskId } = useParams<{ workspaceId: string; rootTaskId: string }>();
  const resolvedWorkspaceId = workspaceId ?? "";
  const resolvedRootTaskId = rootTaskId ?? "";
  const [searchParams, setSearchParams] = useSearchParams();
  const selectedFromUrl = searchParams.get("subtask");
  const [viewMode, setViewMode] = useState<"board" | "dag" | "ledger">("board");

  const graphQuery = useQuery({
    queryKey: taskGraphQueryKey(resolvedWorkspaceId, resolvedRootTaskId),
    queryFn: ({ signal }) => getTaskGraph(resolvedWorkspaceId, resolvedRootTaskId, signal),
    enabled: resolvedWorkspaceId.length > 0 && resolvedRootTaskId.length > 0,
  });
  const rootTaskQuery = useQuery({
    queryKey: taskQueryKey(resolvedWorkspaceId, resolvedRootTaskId),
    queryFn: ({ signal }) => getTask(resolvedWorkspaceId, resolvedRootTaskId, signal),
    enabled: resolvedWorkspaceId.length > 0
      && resolvedRootTaskId.length > 0
      && (graphQuery.data?.overview.attention_items.length ?? 0) > 0,
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
    if (selectedFromUrl !== null && nodes.some((node) => node.subtask_key === selectedFromUrl)) {
      return selectedFromUrl;
    }
    return nodes.find((node) => node.state !== "planned" || node.worker_item_count > 0)?.subtask_key ?? nodes[0]?.subtask_key ?? null;
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
    setSearchParams({ subtask: preferredWorkItemKey }, { replace: true });
  }, [preferredWorkItemKey, selectedFromUrl, setSearchParams]);

  const selectWorkItem = (workItemKey: string) => {
    setSearchParams({ subtask: workItemKey });
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
              <div className="task-detail-heading__meta">
                <Link className="quiet-button" to={`/workspaces/${resolvedWorkspaceId}/tasks/${resolvedRootTaskId}/plan/${graphQuery.data.overview.generation}`}>
                  <History size={14} aria-hidden="true" />计划历史
                </Link>
                <span className="mono-label">CURSOR {graphQuery.data.overview.last_change_cursor}</span>
              </div>
            </div>

            <div className="task-detail-layout">
              <div className="task-detail-main">
                <MilestoneTrack milestones={graphQuery.data.milestones} />
                <AttentionPanel
                  workspaceId={resolvedWorkspaceId}
                  rootTaskId={resolvedRootTaskId}
                  rootTaskVersion={rootTaskQuery.data?.version}
                  rootTaskTitle={graphQuery.data.overview.title}
                  items={graphQuery.data.overview.attention_items}
                  onAcknowledged={() => {
                    void graphQuery.refetch();
                    void rootTaskQuery.refetch();
                  }}
                />
                <ExecutionHorizon graph={graphQuery.data} />
                <div className="graph-ledger-switch" role="tablist" aria-label="任务状态视图">
                  <button className={viewMode === "board" ? "is-active" : ""} type="button" role="tab" aria-selected={viewMode === "board"} onClick={() => setViewMode("board")}>看板</button>
                  <button className={viewMode === "dag" ? "is-active" : ""} type="button" role="tab" aria-selected={viewMode === "dag"} onClick={() => setViewMode("dag")}>DAG</button>
                  <button className={viewMode === "ledger" ? "is-active" : ""} type="button" role="tab" aria-selected={viewMode === "ledger"} onClick={() => setViewMode("ledger")}>任务表</button>
                </div>
                {viewMode === "board" ? <TaskBoard nodes={graphQuery.data.nodes} selectedWorkItemKey={selectedWorkItemKey} onSelect={selectWorkItem} /> : null}
                {viewMode === "dag" ? <TaskGraph graph={graphQuery.data} selectedWorkItemKey={selectedWorkItemKey} onSelect={selectWorkItem} /> : null}
                {viewMode === "ledger" ? <TaskLedger nodes={graphQuery.data.nodes} selectedWorkItemKey={selectedWorkItemKey} onSelect={selectWorkItem} /> : null}
                <TaskActivity
                  activity={activity}
                  workspaceId={resolvedWorkspaceId}
                  rootTaskId={resolvedRootTaskId}
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
