import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { ArrowLeft, RefreshCcw } from "lucide-react";
import { useMemo } from "react";
import { Link, useParams } from "react-router-dom";

import { AsyncState } from "@/shared/ui/AsyncState";
import { StatusMark } from "@/shared/ui/StatusMark";

import { getTaskGraph, taskGraphQueryKey } from "../tasks/api";
import {
  getWorkerItemWorkspace,
  listWorkerItemActivity,
  workerActivityQueryKey,
  workerWorkspaceQueryKey,
} from "./api";
import { WorkerSidebar } from "./WorkerSidebar";
import { WorkerTimeline } from "./WorkerTimeline";
import type { WorkerActivity, WorkerWorkspace } from "./types";

export function WorkerItemPage() {
  const { workspaceId = "", workerItemId = "" } = useParams<{
    workspaceId: string;
    workerItemId: string;
  }>();
  const workspaceQuery = useQuery({
    queryKey: workerWorkspaceQueryKey(workspaceId, workerItemId),
    queryFn: ({ signal }) => getWorkerItemWorkspace(workspaceId, workerItemId, signal),
    enabled: Boolean(workspaceId && workerItemId),
  });
  const rootTaskId = workspaceQuery.data?.worker_item.root_task_id ?? "";
  const taskGraphQuery = useQuery({
    queryKey: taskGraphQueryKey(workspaceId, rootTaskId),
    queryFn: ({ signal }) => getTaskGraph(workspaceId, rootTaskId, signal),
    enabled: rootTaskId.length > 0,
  });
  const activityQuery = useInfiniteQuery({
    queryKey: workerActivityQueryKey(workspaceId, workerItemId),
    initialPageParam: undefined as string | undefined,
    queryFn: ({ signal, pageParam }) => listWorkerItemActivity(
      workspaceId,
      workerItemId,
      pageParam === undefined
        ? { signal }
        : { after: pageParam, signal },
    ),
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    enabled: Boolean(workspaceId && workerItemId),
  });
  const activity = useMemo(() => flattenActivities(activityQuery.data?.pages), [activityQuery.data?.pages]);

  return (
    <section className="worker-item-page" aria-labelledby="worker-item-title">
      <AsyncState
        isLoading={workspaceQuery.isPending}
        isError={workspaceQuery.isError}
        errorMessage="无法读取 WorkerItem 工作区"
        retryLabel="重试读取 WorkerItem"
        onRetry={() => void workspaceQuery.refetch()}
      >
        {workspaceQuery.data ? (
          <WorkerItemContent
            workspace={workspaceQuery.data}
            activity={activity.items}
            hasMore={activity.nextCursor !== null}
            isLoadingMore={activityQuery.isFetchingNextPage}
            isActivityLoading={activityQuery.isPending}
            isActivityError={activityQuery.isError}
            taskTitle={taskGraphQuery.data?.overview.title ?? "任务"}
            subtaskTitle={taskGraphQuery.data?.nodes.find(
              (node) => node.subtask_key === workspaceQuery.data?.current_placement.subtask_key,
            )?.title ?? workspaceQuery.data.current_placement.subtask_key}
            onLoadMore={() => void activityQuery.fetchNextPage()}
            onRetryActivity={() => void activityQuery.refetch()}
          />
        ) : null}
      </AsyncState>
    </section>
  );
}

function WorkerItemContent({
  workspace,
  activity,
  hasMore,
  isLoadingMore,
  isActivityLoading,
  isActivityError,
  taskTitle,
  subtaskTitle,
  onLoadMore,
  onRetryActivity,
}: {
  workspace: WorkerWorkspace;
  activity: readonly WorkerActivity[];
  hasMore: boolean;
  isLoadingMore: boolean;
  isActivityLoading: boolean;
  isActivityError: boolean;
  taskTitle: string;
  subtaskTitle: string;
  onLoadMore: () => void;
  onRetryActivity: () => void;
}) {
  const workerStatus = statusFor(workspace.worker_item.status);
  return (
    <>
      <div className="breadcrumb worker-item-page__breadcrumb">
        <Link to={`/workspaces/${workspace.workspace_id}/tasks`}><ArrowLeft size={13} aria-hidden="true" />任务</Link>
        <span aria-hidden="true">/</span>
        <span>{taskTitle} / {subtaskTitle}</span>
      </div>
      <header className="worker-item-page__heading">
        <div>
          <span className="eyebrow">WORKERITEM WORKSPACE · {workspace.worker_item.stable_key}</span>
          <h1 id="worker-item-title">{workspace.worker_item.title}</h1>
          <p>{workspace.worker_item.responsibility}</p>
        </div>
        <div className="worker-item-page__meta">
          <StatusMark kind={workerStatus.kind} label={workerStatus.label} />
          <span className="mono-label">CONTEXT V{workspace.worker_item.context_version}</span>
          <span className="mono-label">CURSOR {workspace.last_change_cursor}</span>
        </div>
      </header>

      <div className="worker-item-page__layout">
        <main className="worker-item-page__main">
          <div className="worker-item-page__scope">
            <span className="eyebrow">LONG-LIVED WORKSPACE</span>
            <strong>{workspace.worker_item.responsibility}</strong>
            <span>Execution / Session 会在这里连续呈现，历史上下文不会因换代丢失。</span>
          </div>
          {isActivityError ? (
            <div className="worker-item-page__activity-error" role="alert">
              <RefreshCcw size={15} aria-hidden="true" />
              <span>活动时间线暂不可用</span>
              <button className="quiet-button" type="button" onClick={onRetryActivity}>重试读取活动</button>
            </div>
          ) : null}
          {isActivityLoading ? (
            <div className="worker-item-page__activity-loading" role="status">读取 WorkerItem 活动</div>
          ) : (
            <WorkerTimeline
              activities={activity}
              hasMore={hasMore}
              isLoadingMore={isLoadingMore}
              onLoadMore={onLoadMore}
            />
          )}
        </main>
        <WorkerSidebar workspace={workspace} taskTitle={taskTitle} subtaskTitle={subtaskTitle} />
      </div>
    </>
  );
}

function flattenActivities(
  pages: readonly { items: readonly WorkerActivity[]; next_cursor: string | null }[] | undefined,
): { items: WorkerActivity[]; nextCursor: string | null } {
  const items: WorkerActivity[] = [];
  const seen = new Set<string>();
  for (const page of pages ?? []) {
    for (const activity of page.items) {
      if (seen.has(activity.activity_id)) continue;
      seen.add(activity.activity_id);
      items.push(activity);
    }
  }
  return {
    items,
    nextCursor: pages?.at(-1)?.next_cursor ?? null,
  };
}

function statusFor(status: string): { kind: "planned" | "running" | "completed" | "failed" | "stale"; label: string } {
  if (status === "running" || status === "assigned" || status === "queued" || status === "retrying") {
    return { kind: "running", label: "执行中" };
  }
  if (status === "succeeded" || status === "completed") return { kind: "completed", label: "已完成" };
  if (status === "failed") return { kind: "failed", label: "失败" };
  if (status === "blocked" || status === "waiting_user" || status === "waiting_dependency" || status === "retired") {
    return { kind: "stale", label: status === "retired" ? "只读" : "需要处理" };
  }
  return { kind: "planned", label: "计划中" };
}
