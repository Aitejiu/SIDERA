import { useMemo } from "react";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { History } from "lucide-react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";

import { AsyncState } from "@/shared/ui/AsyncState";
import { StatusMark, type StatusMarkKind } from "@/shared/ui/StatusMark";
import { AttentionPanel } from "@/features/attention/AttentionPanel";

import {
  getTask,
  getTaskActivity,
  getTaskGraph,
  taskActivityQueryKey,
  taskGraphQueryKey,
  taskQueryKey,
  TASK_ACTIVITY_PAGE_SIZE,
} from "./api";
import { ExecutionHorizon } from "./ExecutionHorizon";
import { MilestoneTrack } from "./MilestoneTrack";
import { TaskActivity } from "./TaskActivity";
import { TaskBoard } from "./TaskBoard";
import { TaskGraph } from "./TaskGraph";
import type { TaskActivity as TaskActivityProjection, TaskGraph as TaskGraphProjection } from "./types";

export function TaskDetailPage() {
  const { workspaceId, rootTaskId } = useParams<{ workspaceId: string; rootTaskId: string }>();
  const resolvedWorkspaceId = workspaceId ?? "";
  const resolvedRootTaskId = rootTaskId ?? "";
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const viewMode = searchParams.get("view") === "dag" ? "dag" : "board";

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
      pageParam === undefined
        ? { limit: TASK_ACTIVITY_PAGE_SIZE, signal }
        : { after: pageParam, limit: TASK_ACTIVITY_PAGE_SIZE, signal },
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

  const setViewMode = (nextMode: "board" | "dag") => {
    const nextParams = new URLSearchParams(searchParams);
    if (nextMode === "dag") {
      nextParams.set("view", "dag");
    } else {
      nextParams.delete("view");
    }
    setSearchParams(nextParams);
  };

  const openSubtask = (subtaskKey: string) => {
    navigate(
      `/workspaces/${encodeURIComponent(resolvedWorkspaceId)}/tasks/${encodeURIComponent(resolvedRootTaskId)}/subtasks/${encodeURIComponent(subtaskKey)}`,
    );
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
          <TaskDeliveryContent
            graph={graphQuery.data}
            activity={activity}
            activityError={activityQuery.isError}
            activityLoading={activityQuery.isPending}
            activityLoadingMore={activityQuery.isFetchingNextPage}
            onLoadMore={() => void activityQuery.fetchNextPage()}
            onRetryActivity={() => void activityQuery.refetch()}
            onOpenSubtask={openSubtask}
            rootTaskVersion={rootTaskQuery.data?.version}
            viewMode={viewMode}
            onViewModeChange={setViewMode}
            workspaceId={resolvedWorkspaceId}
            rootTaskId={resolvedRootTaskId}
            onAcknowledged={() => {
              void graphQuery.refetch();
              void rootTaskQuery.refetch();
            }}
          />
        ) : null}
      </AsyncState>
    </section>
  );
}

function TaskDeliveryContent({
  graph,
  activity,
  activityError,
  activityLoading,
  activityLoadingMore,
  onLoadMore,
  onRetryActivity,
  onOpenSubtask,
  rootTaskVersion,
  viewMode,
  onViewModeChange,
  workspaceId,
  rootTaskId,
  onAcknowledged,
}: {
  graph: TaskGraphProjection;
  activity: TaskActivityProjection | undefined;
  activityError: boolean;
  activityLoading: boolean;
  activityLoadingMore: boolean;
  onLoadMore: () => void;
  onRetryActivity: () => void;
  onOpenSubtask: (subtaskKey: string) => void;
  rootTaskVersion: number | undefined;
  viewMode: "board" | "dag";
  onViewModeChange: (mode: "board" | "dag") => void;
  workspaceId: string;
  rootTaskId: string;
  onAcknowledged: () => void;
}) {
  const status = taskStatus(graph);

  return (
    <>
      <header className="task-detail-heading">
        <div>
          <span className="eyebrow">TASK CONTROL ROOM · GEN {graph.overview.generation}</span>
          <h1 id="task-detail-title">{graph.overview.title}</h1>
          <p>{graph.overview.goal_summary}</p>
        </div>
        <div className="task-detail-heading__meta">
          <StatusMark kind={status.kind} label={status.label} />
          <Link
            className="quiet-button"
            to={`/workspaces/${workspaceId}/tasks/${rootTaskId}/plan/${graph.overview.generation}`}
          >
            <History size={14} aria-hidden="true" />计划历史
          </Link>
          <span className="mono-label">CURSOR {graph.overview.last_change_cursor}</span>
        </div>
      </header>

      <div className="task-detail-main">
        <AttentionPanel
          workspaceId={workspaceId}
          rootTaskId={rootTaskId}
          rootTaskVersion={rootTaskVersion}
          rootTaskTitle={graph.overview.title}
          items={graph.overview.attention_items}
          onAcknowledged={onAcknowledged}
        />
        <MilestoneTrack milestones={graph.milestones} />
        <ExecutionHorizon graph={graph} />
        <div className="task-view-switch" role="tablist" aria-label="子任务状态视图">
          <button
            className={viewMode === "board" ? "is-active" : ""}
            type="button"
            role="tab"
            aria-selected={viewMode === "board"}
            onClick={() => onViewModeChange("board")}
          >
            看板
          </button>
          <button
            className={viewMode === "dag" ? "is-active" : ""}
            type="button"
            role="tab"
            aria-selected={viewMode === "dag"}
            onClick={() => onViewModeChange("dag")}
          >
            DAG
          </button>
        </div>
        {viewMode === "board" ? <TaskBoard nodes={graph.nodes} onOpen={onOpenSubtask} /> : null}
        {viewMode === "dag" ? <TaskGraph graph={graph} onOpen={onOpenSubtask} /> : null}
        <TaskActivity
          activity={activity}
          workspaceId={workspaceId}
          rootTaskId={rootTaskId}
          onLoadMore={onLoadMore}
          isLoadingMore={activityLoadingMore}
          isLoading={activityLoading}
          isError={activityError}
          onRetry={onRetryActivity}
        />
      </div>
    </>
  );
}

function taskStatus(graph: TaskGraphProjection): { kind: StatusMarkKind; label: string } {
  if (graph.overview.failed_subtask_count > 0) {
    return { kind: "failed", label: "有失败子任务" };
  }
  if (graph.overview.attention_items.length > 0) {
    return { kind: "stale", label: "需要处理" };
  }
  if (graph.overview.completed_subtask_count === graph.overview.subtask_count && graph.overview.subtask_count > 0) {
    return { kind: "completed", label: "已完成" };
  }
  if (graph.overview.running_subtask_count > 0) {
    return { kind: "running", label: "执行中" };
  }
  return { kind: "planned", label: "计划中" };
}
