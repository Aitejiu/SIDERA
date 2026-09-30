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

import { getWorkspaceCapabilities, workspaceCapabilitiesQueryKey } from "./api";
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
  const workspaceId =
    useParams<{ workspaceId: string }>().workspaceId ?? DEFAULT_WORKSPACE_ID;
  const query = useQuery({
    queryKey: workspaceCapabilitiesQueryKey(workspaceId),
    queryFn: ({ signal }) => getWorkspaceCapabilities(workspaceId, signal),
    enabled: workspaceId.length > 0,
    staleTime: 15_000,
  });
  const snapshot = query.data;
  return (
    <WorkspacePageShell
      eyebrow="MODULES / CAPABILITIES"
      title="能力"
      icon={<Boxes size={18} aria-hidden="true" />}
    >
      {query.isPending ? <div className="capability-list" aria-label="正在读取能力状态" /> : null}
      {query.isError ? (
        <div className="async-state async-state--error" role="alert">
          <span className="async-state__mark" aria-hidden="true">!</span>
          <div><strong>无法读取能力状态</strong><p>控制面状态暂时不可用。</p></div>
          <button className="quiet-button" type="button" onClick={() => void query.refetch()}>重试读取能力</button>
        </div>
      ) : null}
      {snapshot ? <div className="capability-list">
        <CapabilityRow
          icon={<Radio size={15} />}
          title="控制面"
          value="SIDERA Control Plane"
          status={serviceStatusLabel(snapshot.controlPlane)}
        />
        <CapabilityRow
          icon={<Sparkles size={15} />}
          title="Worker Adapter"
          value={moduleValue(snapshot.workerAdapters, "按工作区配置")}
          status={moduleStatusLabel(snapshot.workerAdapters)}
        />
        <CapabilityRow
          icon={<PackageOpen size={15} />}
          title="Workspace Provider"
          value={moduleValue(snapshot.workspaceProviders, "按工作区配置")}
          status={moduleStatusLabel(snapshot.workspaceProviders)}
        />
        <CapabilityRow
          icon={<MessageCircle size={15} />}
          title="Main Agent"
          value={snapshot.mainAgent === "available" ? "已配置" : "配置不可用"}
          status={serviceStatusLabel(snapshot.mainAgent)}
        />
      </div> : null}
    </WorkspacePageShell>
  );
}

function serviceStatusLabel(status: "available" | "unavailable" | "unknown"): string {
  if (status === "available") return "已连接";
  if (status === "unavailable") return "不可用";
  return "状态未知";
}

function moduleStatusLabel(modules: Array<{ definition_lifecycle: string; instance_lifecycle: string }>): string {
  if (modules.length === 0) return "未发现";
  return modules.some((module) => module.definition_lifecycle === "active" && module.instance_lifecycle === "active")
    ? "可配置"
    : "不可用";
}

function moduleValue(
  modules: Array<{ provider: string }>,
  fallback: string,
): string {
  const providers = [...new Set(modules.map((module) => module.provider).filter(Boolean))];
  return providers.length > 0 ? providers.join(" / ") : fallback;
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
