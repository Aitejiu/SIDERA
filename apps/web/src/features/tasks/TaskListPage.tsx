import { Plus, RefreshCw } from "lucide-react";
import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";

import { useWorkspaceStreamOptional } from "@/app/WorkspaceStreamProvider";
import { AsyncState } from "@/shared/ui/AsyncState";

import { listTaskOverviews, taskOverviewQueryKey } from "./api";
import { TaskOverviewStrip } from "./TaskOverviewStrip";

export function TaskListPage() {
  const { workspaceId } = useParams<{ workspaceId: string }>();
  const resolvedWorkspaceId = workspaceId ?? "";
  const stream = useWorkspaceStreamOptional();
  const query = useQuery({
    queryKey: taskOverviewQueryKey(resolvedWorkspaceId),
    queryFn: ({ signal }) => listTaskOverviews(resolvedWorkspaceId, signal),
    enabled: resolvedWorkspaceId.length > 0,
  });

  useEffect(() => {
    if (!query.data || stream === null) {
      return;
    }
    const cursor = query.data.reduce(
      (highest, overview) => Math.max(highest, overview.last_change_cursor),
      0,
    );
    stream.seedSnapshotCursor(cursor);
  }, [query.data, stream]);

  const retryLabel = query.isError ? "重试加载任务" : "刷新任务";
  return (
    <section className="task-list-page" aria-labelledby="workspace-title">
      <div className="breadcrumb">工作区 / 任务</div>
      <div className="page-heading">
        <div>
          <span className="eyebrow">TASK FIRST CONTROL ROOM</span>
          <h1 id="workspace-title">任务</h1>
        </div>
        <div className="page-heading__actions">
          <Link
            className="icon-button"
            to={`/workspaces/${resolvedWorkspaceId}/planning/new`}
            aria-label="新建任务"
            title="新建任务"
          >
            <Plus size={16} aria-hidden="true" />
          </Link>
          <button
            className="icon-button"
            type="button"
            onClick={() => void query.refetch()}
            aria-label="刷新任务列表"
            title="刷新任务列表"
          >
            <RefreshCw size={16} aria-hidden="true" />
          </button>
        </div>
      </div>

      <div className="task-list-intro">
        <span className="mono-label">CANONICAL SNAPSHOT · CURSOR {highestCursor(query.data)}</span>
      </div>

      <AsyncState
        isLoading={query.isPending}
        isError={query.isError}
        isEmpty={query.data?.length === 0}
        errorMessage="无法读取任务快照"
        retryLabel={retryLabel}
        onRetry={() => void query.refetch()}
      >
        <div className="task-list-header" aria-hidden="true">
          <span>任务</span>
          <span>计划</span>
          <span>物化</span>
          <span>调度</span>
          <span>下一节点</span>
          <span>需要我</span>
        </div>
        <div className="task-list" aria-label="任务列表">
          {query.data?.map((overview) => (
            <TaskOverviewStrip
              key={overview.root_task_id}
              workspaceId={resolvedWorkspaceId}
              overview={overview}
            />
          ))}
        </div>
      </AsyncState>
    </section>
  );
}

function highestCursor(data: readonly { last_change_cursor: number }[] | undefined): number {
  return data?.reduce(
    (highest, overview) => Math.max(highest, overview.last_change_cursor),
    0,
  ) ?? 0;
}
