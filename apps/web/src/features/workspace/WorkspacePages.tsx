import {
  Boxes,
  Clock3,
  MessageCircle,
  PackageOpen,
  Radio,
  Sparkles,
} from "lucide-react";
import type { ReactNode } from "react";
import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import {
  listTaskOverviews,
  taskOverviewQueryKey,
  type TaskOverview,
} from "@/features/tasks/api";
import { AsyncState } from "@/shared/ui/AsyncState";

const DEFAULT_WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";

export function HistoryPage() {
  const workspaceId =
    useParams<{ workspaceId: string }>().workspaceId ?? DEFAULT_WORKSPACE_ID;
  const query = useQuery({
    queryKey: taskOverviewQueryKey(workspaceId),
    queryFn: ({ signal }) => listTaskOverviews(workspaceId, signal),
    enabled: workspaceId.length > 0,
  });
  return (
    <WorkspacePageShell
      eyebrow="TASK HISTORY"
      title="历史"
      icon={<Clock3 size={18} aria-hidden="true" />}
    >
      <AsyncState
        isLoading={query.isPending}
        isError={query.isError}
        isEmpty={query.data?.length === 0}
        errorMessage="无法读取任务历史"
        retryLabel="重试读取历史"
        onRetry={() => void query.refetch()}
      >
        <div className="history-list">
          {query.data?.map((task) => (
            <HistoryRow key={task.root_task_id} workspaceId={workspaceId} task={task} />
          ))}
        </div>
      </AsyncState>
    </WorkspacePageShell>
  );
}

export function CapabilitiesPage() {
  return (
    <WorkspacePageShell
      eyebrow="MODULES / CAPABILITIES"
      title="能力"
      icon={<Boxes size={18} aria-hidden="true" />}
    >
      <div className="capability-list">
        <CapabilityRow
          icon={<Radio size={15} />}
          title="控制面"
          value="SIDERA Control Plane"
          status="已连接"
        />
        <CapabilityRow
          icon={<Sparkles size={15} />}
          title="Worker Adapter"
          value="按工作区配置"
          status="可配置"
        />
        <CapabilityRow
          icon={<PackageOpen size={15} />}
          title="Workspace Provider"
          value="Git Worktree"
          status="可配置"
        />
        <CapabilityRow
          icon={<MessageCircle size={15} />}
          title="Main Agent"
          value="入口预留"
          status="R5"
        />
      </div>
    </WorkspacePageShell>
  );
}

function WorkspacePageShell({
  eyebrow,
  title,
  icon,
  summary,
  children,
}: {
  eyebrow: string;
  title: string;
  icon: ReactNode;
  summary?: string;
  children: ReactNode;
}) {
  return (
    <section className="workspace-page" aria-labelledby="workspace-page-title">
      <div className="breadcrumb">工作区 / {title}</div>
      <div className="workspace-page__heading">
        <div className="workspace-page__title">
          <span className="workspace-page__icon">{icon}</span>
          <div>
            <span className="eyebrow">{eyebrow}</span>
            <h1 id="workspace-page-title">{title}</h1>
          </div>
        </div>
        {summary ? <span className="mono-label">{summary}</span> : null}
      </div>
      {children}
    </section>
  );
}

function HistoryRow({
  workspaceId,
  task,
}: {
  workspaceId: string;
  task: TaskOverview;
}) {
  return (
    <Link
      className="history-row"
      to={`/workspaces/${workspaceId}/tasks/${task.root_task_id}`}
    >
      <div>
        <strong>{task.title}</strong>
        <span>{task.goal_summary}</span>
      </div>
      <span className="mono-label">
        GEN {task.generation} · {task.completed_subtask_count}/{task.planned_subtask_count} 完成
      </span>
    </Link>
  );
}

function CapabilityRow({
  icon,
  title,
  value,
  status,
}: {
  icon: ReactNode;
  title: string;
  value: string;
  status: string;
}) {
  return (
    <div className="capability-row">
      <span className="capability-row__icon">{icon}</span>
      <div>
        <strong>{title}</strong>
        <span>{value}</span>
      </div>
      <span className="mono-label">{status}</span>
    </div>
  );
}
