import { useEffect, useMemo, useRef } from "react";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { ChevronDown, Clock3, History, MessageCircle, SlidersHorizontal } from "lucide-react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";

import { AsyncState } from "@/shared/ui/AsyncState";
import { useTaskDetailsState } from "@/app/WorkspaceViewState";
import { useWorkspaceStreamOptional } from "@/app/WorkspaceStreamProvider";
import { StatusMark, type StatusMarkKind } from "@/shared/ui/StatusMark";
import { AttentionPanel, type NativeAttentionPanelAction } from "@/features/attention/AttentionPanel";
import { NativeAttentionRecovery } from "@/features/attention/NativeAttentionRecovery";
import { useNativeAttentionAcknowledgement } from "@/features/attention/useNativeAttentionAcknowledgement";
import { CollaborationSummary } from "@/features/collaboration/CollaborationSummary";

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
import { TaskKnowledgeSummary } from "../context/TaskKnowledgeSummary";
import { PlanConvergenceStrip } from "./PlanConvergenceStrip";
import { PlanRevisionAttention } from "./PlanRevisionAttention";
import { TaskResultPanel } from "@/features/results/TaskResultPanel";
import type { TaskActivity as TaskActivityProjection, TaskGraph as TaskGraphProjection } from "./types";

export function TaskDetailPage() {
  const { workspaceId, rootTaskId } = useParams<{ workspaceId: string; rootTaskId: string }>();
  const resolvedWorkspaceId = workspaceId ?? "";
  const resolvedRootTaskId = rootTaskId ?? "";
  const stream = useWorkspaceStreamOptional();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const viewMode = searchParams.get("view") === "dag"
    ? "dag"
    : searchParams.get("view") === "results"
      ? "results"
      : "board";

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
  const attentionAction = useNativeAttentionAcknowledgement({
    workspaceId: resolvedWorkspaceId, rootTaskId: resolvedRootTaskId,
    onRefresh: async () => {
      await Promise.all([graphQuery.refetch({ throwOnError: true }), rootTaskQuery.refetch({ throwOnError: true })]);
    },
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

  useEffect(() => {
    if (graphQuery.data !== undefined && stream !== null) {
      stream.seedSnapshotCursor(graphQuery.data.overview.last_change_cursor);
    }
  }, [graphQuery.data, stream]);

  const setViewMode = (nextMode: "board" | "dag" | "results") => {
    const nextParams = new URLSearchParams(searchParams);
    if (nextMode === "dag") nextParams.set("view", "dag");
    else if (nextMode === "results") nextParams.set("view", "results");
    else nextParams.delete("view");
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
      <NativeAttentionRecovery workspaceId={resolvedWorkspaceId} actions={attentionAction} />
      <AsyncState
        isLoading={graphQuery.isPending}
        isError={graphQuery.isError}
        errorMessage="无法读取任务图"
        retryLabel="重试读取任务图"
        onRetry={() => void graphQuery.refetch()}
      >
        {graphQuery.data ? (
          <TaskDeliveryContent
            key={`${resolvedWorkspaceId}:${resolvedRootTaskId}`}
            graph={graphQuery.data}
            activity={activity}
            activityError={activityQuery.isError}
            activityLoading={activityQuery.isPending}
            activityLoadingMore={activityQuery.isFetchingNextPage}
            onLoadMore={() => void activityQuery.fetchNextPage()}
            onRetryActivity={() => void activityQuery.refetch()}
            onOpenSubtask={openSubtask}
            rootTaskVersion={rootTaskQuery.data?.version}
            nativeAttentionAction={attentionAction.authority?.kind === "legacy" ? undefined : attentionAction}
            viewMode={viewMode}
            onViewModeChange={setViewMode}
            workspaceId={resolvedWorkspaceId}
            rootTaskId={resolvedRootTaskId}
            onAcknowledged={attentionAction.authority?.kind === "legacy" ? () => {
              void graphQuery.refetch();
              void rootTaskQuery.refetch();
            } : undefined}
            onRefreshAttention={async () => {
              await Promise.all([graphQuery.refetch(), rootTaskQuery.refetch()]);
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
  nativeAttentionAction,
  viewMode,
  onViewModeChange,
  workspaceId,
  rootTaskId,
  onAcknowledged,
  onRefreshAttention,
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
  nativeAttentionAction: NativeAttentionPanelAction | undefined;
  viewMode: "board" | "dag" | "results";
  onViewModeChange: (mode: "board" | "dag" | "results") => void;
  workspaceId: string;
  rootTaskId: string;
  onAcknowledged: (() => void) | undefined;
  onRefreshAttention: () => Promise<void>;
}) {
  const detailsRef = useRef<HTMLDetailsElement>(null);
  const [detailsOpen, setDetailsOpen] = useTaskDetailsState(rootTaskId);
  const status = taskStatus(graph);
  const { overview } = graph;
  const collaborationNeedsAttention = Boolean(overview.collaboration?.attention_required
    || overview.collaboration?.attention_subtasks?.length
    || overview.collaboration?.blocked_subtask_count
    || overview.collaboration?.blocked_subtasks?.length);
  const convergenceNeedsAttention = Boolean(overview.convergence?.confirmation_blocked
    || overview.convergence?.has_active_fence
    || overview.convergence?.has_unknown
    || overview.convergence?.unknown_count
    || overview.convergence?.blocked_count
    || ["blocked_unknown", "degraded"].includes(overview.convergence?.status ?? ""));
  const schedulingWaits = graph.overview.attention_items.filter(isSchedulingWait);
  const blockedSubtasks = graph.nodes
    .filter((node) => node.requires_operator || node.attention_worker_item_count > 0 || ["blocked", "failed", "waiting_user"].includes(node.state))
    .map((node) => ({
      key: node.subtask_key,
      title: node.title,
      detail: node.waiting_details_summary || node.current_action || node.waiting_reason || null,
    }));
  const collaboration = (
    <CollaborationSummary
      summary={overview.collaboration}
      rootTaskTitle={overview.title}
      onOpenSubtask={onOpenSubtask}
    />
  );
  const convergence = <PlanConvergenceStrip convergence={overview.convergence} blockedItems={blockedSubtasks} />;

  const toggleDetails = () => {
    const details = detailsRef.current;
    if (!details) return;
    details.open = !details.open;
    if (details.open) details.querySelector("summary")?.focus();
  };

  return (
    <>
      <header className="task-detail-heading">
        <div className="task-detail-heading__identity">
          <h1 id="task-detail-title">{graph.overview.title}</h1>
          <p>{graph.overview.goal_summary}</p>
          <div className="task-detail-progress" role="group" aria-label="任务进度">
            <StatusMark kind={status.kind} label={status.label} />
            <span>已完成 <strong>{overview.completed_subtask_count} / {overview.subtask_count}</strong> 子任务</span>
            <span>执行中 <strong>{overview.running_subtask_count}</strong></span>
            {overview.failed_subtask_count > 0 ? <span className="task-detail-progress__failed">失败 <strong>{overview.failed_subtask_count}</strong></span> : null}
          </div>
        </div>
        <div className="task-detail-heading__meta">
          <Link
            className="quiet-button task-detail-heading__history"
            to={`/workspaces/${workspaceId}/tasks/${rootTaskId}/plan/${graph.overview.generation}`}
            aria-label="计划历史"
            title="计划历史"
          >
            <History size={16} aria-hidden="true" />
          </Link>
          <Link
            className="quiet-button"
            to={`/workspaces/${workspaceId}/chat?context_ref=task:${rootTaskId}`}
          >
            <MessageCircle size={14} aria-hidden="true" />与 Main Agent 讨论
          </Link>
          <button className="quiet-button" type="button" aria-label="查看任务详情" aria-controls="task-detail-information" aria-expanded={detailsOpen} onClick={toggleDetails}>
            <SlidersHorizontal size={14} aria-hidden="true" />详情
          </button>
        </div>
      </header>

      <div className="task-detail-main">
        <div className="task-detail-attention" data-workspace-scroll="task-attention" role="region" aria-label="任务待处理事项" tabIndex={0}>
          <PlanRevisionAttention
            attention={graph.overview.revision_attention}
            rootTaskTitle={graph.overview.title}
          />
          <AttentionPanel
            workspaceId={workspaceId}
            rootTaskId={rootTaskId}
            rootTaskVersion={rootTaskVersion}
            nativeAction={nativeAttentionAction}
            rootTaskTitle={graph.overview.title}
            items={graph.overview.attention_items}
            onAcknowledged={onAcknowledged}
            onRefresh={onRefreshAttention}
            compact
          />
          {collaborationNeedsAttention ? collaboration : null}
          {convergenceNeedsAttention ? convergence : null}
          <SchedulingWaitSummary items={schedulingWaits} />
        </div>
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
          <button
            className={viewMode === "results" ? "is-active" : ""}
            type="button"
            role="tab"
            aria-selected={viewMode === "results"}
            onClick={() => onViewModeChange("results")}
          >
            成果
          </button>
        </div>
        {viewMode === "board" ? <TaskBoard nodes={graph.nodes} onOpen={onOpenSubtask} /> : null}
        {viewMode === "dag" ? <TaskGraph graph={graph} onOpen={onOpenSubtask} /> : null}
        {viewMode === "results" ? <TaskResultPanel workspaceId={workspaceId} rootTaskId={rootTaskId} /> : null}
        <details id="task-detail-information" className="task-details" ref={detailsRef} open={detailsOpen} onToggle={(event) => setDetailsOpen(event.currentTarget.open)}>
          <summary><ChevronDown size={16} aria-hidden="true" /><span>任务详情</span></summary>
          <div className="task-details__content">
            <div className="task-details__identity mono-label">GEN {overview.generation} · CURSOR {overview.last_change_cursor}</div>
            <ExecutionHorizon graph={graph} />
            <MilestoneTrack milestones={graph.milestones} />
            {!convergenceNeedsAttention ? convergence : null}
            {!collaborationNeedsAttention ? collaboration : null}
            <TaskKnowledgeSummary workspaceId={workspaceId} rootTaskId={rootTaskId} />
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
        </details>
      </div>
    </>
  );
}

type TaskAttentionItem = TaskGraphProjection["overview"]["attention_items"][number];

function SchedulingWaitSummary({ items }: { items: readonly TaskAttentionItem[] }) {
  if (items.length === 0) {
    return null;
  }
  return (
    <section className="scheduling-wait-panel" aria-labelledby="scheduling-wait-title">
      <div className="section-heading">
        <div>
          <span className="eyebrow">SCHEDULER</span>
          <h2 id="scheduling-wait-title">调度等待</h2>
        </div>
        <span className="mono-label">{items.length} 个子任务</span>
      </div>
      <div className="scheduling-wait-list">
        {items.map((item) => (
          <article className="scheduling-wait-item" key={item.key}>
            <div className="scheduling-wait-item__heading">
              <strong>{item.subtask_title}</strong>
              <span className="scheduling-wait-item__tag">调度等待</span>
            </div>
            <p>{item.waiting_details_summary || item.reason}</p>
            <div className="scheduling-wait-item__meta">
              <span>
                <Clock3 size={13} aria-hidden="true" />
                阻塞于 {formatBlockedSince(item.blocked_since)}
              </span>
              {item.requires_operator ? <strong>需要我处理</strong> : <span>等待执行节点恢复</span>}
            </div>
          </article>
        ))}
      </div>
    </section>
  );
}

function isSchedulingWait(item: TaskAttentionItem): boolean {
  return Boolean(item.waiting_details_summary) || item.key.startsWith("scheduling_block:");
}

function formatBlockedSince(value: string | null | undefined): string {
  if (!value) {
    return "时间未知";
  }
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) {
    return "时间未知";
  }
  return new Date(timestamp).toLocaleString("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function taskStatus(graph: TaskGraphProjection): { kind: StatusMarkKind; label: string } {
  if (graph.overview.failed_subtask_count > 0) {
    return { kind: "failed", label: "有失败子任务" };
  }
  if (
    graph.overview.attention_items.length > 0
    || (graph.overview.collaboration?.attention_subtasks?.length ?? 0) > 0
    || graph.overview.revision_attention !== null
    && graph.overview.revision_attention !== undefined
  ) {
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
