import {
  Boxes,
  Check,
  CircleDashed,
  Clock3,
  Mail,
  MessageCircle,
  PackageOpen,
  Radio,
  Sparkles,
} from "lucide-react";
import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { useWorkspaceStreamOptional } from "@/app/WorkspaceStreamProvider";
import {
  listTaskOverviews,
  taskOverviewQueryKey,
  type TaskOverview,
} from "@/features/tasks/api";
import {
  listPlanningRequests,
  planningRequestsQueryKey,
} from "@/features/planning/api";
import {
  planningNotificationFromRequest,
  type WorkspaceNotification,
} from "@/features/attention/notifications";
import { AsyncState } from "@/shared/ui/AsyncState";

const DEFAULT_WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";

export function InboxPage() {
  const stream = useWorkspaceStreamOptional();
  const workspaceId = useParams<{ workspaceId: string }>().workspaceId ?? DEFAULT_WORKSPACE_ID;
  const planningAttentionQuery = useQuery({
    queryKey: planningRequestsQueryKey(workspaceId, true),
    queryFn: ({ signal }) => listPlanningRequests(workspaceId, { requiresAttention: true, signal }),
    enabled: workspaceId.length > 0,
  });
  const notices = useMemo(
    () => mergeNotifications(
      stream?.notices ?? [],
      (planningAttentionQuery.data?.items ?? [])
        .map((request) => planningNotificationFromRequest(request))
        .filter((notice): notice is WorkspaceNotification => notice !== null),
    ),
    [planningAttentionQuery.data?.items, stream?.notices],
  );
  const [filter, setFilter] = useState<"action" | "progress" | "archived">("action");
  const visibleNotices = useMemo(
    () =>
      notices.filter((notice) => {
        if (filter === "action") {
          return isActionNotice(notice);
        }
        if (filter === "progress") {
          return !isActionNotice(notice);
        }
        return false;
      }),
    [filter, notices],
  );

  return (
    <WorkspacePageShell
      eyebrow="INBOX / PROJECTION"
      title="信箱"
      summary=""
      icon={<Mail size={18} aria-hidden="true" />}
    >
      <div className="workspace-segmented" role="tablist" aria-label="信箱分类">
        <InboxTab
          active={filter === "action"}
          onClick={() => setFilter("action")}
          label="需要处理"
          count={
            notices.filter(isActionNotice).length
          }
        />
        <InboxTab
          active={filter === "progress"}
          onClick={() => setFilter("progress")}
          label="进度通报"
          count={
            notices.filter((notice) => !isActionNotice(notice)).length
          }
        />
        <InboxTab
          active={filter === "archived"}
          onClick={() => setFilter("archived")}
          label="已归档"
        />
      </div>
      {filter === "archived" ? (
        <EmptyWorkspaceState icon={<PackageOpen size={18} />} title="暂无已归档事项" />
      ) : planningAttentionQuery.isError ? (
        <div className="workspace-empty workspace-empty--error" role="alert">
          <div className="workspace-empty__glyph"><Radio size={18} /></div>
          <strong>无法恢复规划信箱</strong>
          <span>控制面暂时没有返回待处理的 PlanningRequest。</span>
          <button className="quiet-button" type="button" onClick={() => void planningAttentionQuery.refetch()}>
            重试读取
          </button>
        </div>
      ) : visibleNotices.length === 0 ? (
        <EmptyWorkspaceState
          icon={<CircleDashed size={18} />}
          title={filter === "action" ? "当前没有需要处理的事项" : "当前没有新的进度通报"}
        />
      ) : (
        <div className="inbox-list">
          {visibleNotices.map((notice) => (
            <article className="inbox-item" key={notice.id}>
              <div className="inbox-item__marker">
                <StatusIcon kind={notice.kind} />
              </div>
              <div className="inbox-item__body">
                <strong>{notice.title}</strong>
                <span>{notice.summary}</span>
                <span className="mono-label">
                  {notice.cursor === null ? `REQUEST V${notice.resourceVersion ?? "?"}` : `CURSOR ${notice.cursor}`}
                </span>
              </div>
              <div className="inbox-item__actions">
                <Link className="quiet-button" to={notice.href}>
                  查看任务
                </Link>
              </div>
            </article>
          ))}
        </div>
      )}
    </WorkspacePageShell>
  );
}

export function ChatPage() {
  return (
    <WorkspacePageShell
      eyebrow="MAIN AGENT / CHAT"
      title="聊天"
      icon={<MessageCircle size={18} aria-hidden="true" />}
    >
      <div className="workspace-empty workspace-empty--chat">
        <div className="workspace-empty__glyph">
          <MessageCircle size={20} aria-hidden="true" />
        </div>
        <strong>Main Agent 对话入口</strong>
        <span>聊天通道将在主 agent 接入后启用。</span>
        <span className="mono-label">R5 · MAIN AGENT GATEWAY</span>
      </div>
    </WorkspacePageShell>
  );
}

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
  icon: React.ReactNode;
  summary?: string;
  children: React.ReactNode;
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

function InboxTab({
  active,
  label,
  count,
  onClick,
}: {
  active: boolean;
  label: string;
  count?: number;
  onClick: () => void;
}) {
  return (
    <button
      className={`workspace-segment${active ? " is-active" : ""}`}
      type="button"
      role="tab"
      aria-selected={active}
      onClick={onClick}
    >
      {label}
      {count ? <span>{count}</span> : null}
    </button>
  );
}

function EmptyWorkspaceState({ icon, title }: { icon: React.ReactNode; title: string }) {
  return (
    <div className="workspace-empty" role="status">
      <div className="workspace-empty__glyph">{icon}</div>
      <strong>{title}</strong>
    </div>
  );
}

function StatusIcon({ kind }: { kind: string }) {
  if (kind === "waiting_for_user" || kind === "planning_clarification") {
    return <CircleDashed size={16} aria-hidden="true" />;
  }
  if (kind === "blocked" || kind === "planning_blocked" || kind === "planning_conflicted") {
    return <Radio size={16} aria-hidden="true" />;
  }
  return <Check size={16} aria-hidden="true" />;
}

function isActionNotice(notice: WorkspaceNotification): boolean {
  return notice.kind === "waiting_for_user"
    || notice.kind === "blocked"
    || notice.kind === "planning_clarification"
    || notice.kind === "planning_proposal"
    || notice.kind === "planning_conflicted"
    || notice.kind === "planning_blocked";
}

function mergeNotifications(
  streamNotices: readonly WorkspaceNotification[],
  persistedNotices: readonly WorkspaceNotification[],
): WorkspaceNotification[] {
  const merged = new Map<string, WorkspaceNotification>();
  const persistedPlanningResources = new Set(
    persistedNotices
      .filter((notice) => isPlanningNotice(notice) && notice.resourceId !== null)
      .map((notice) => notice.resourceId),
  );
  const candidates = [
    ...persistedNotices,
    ...streamNotices.filter(
      (notice) => !isPlanningNotice(notice) || !persistedPlanningResources.has(notice.resourceId),
    ),
  ];
  for (const notice of candidates) {
    const key = `${notice.kind}:${notice.resourceId ?? notice.id}`;
    if (!merged.has(key)) {
      merged.set(key, notice);
    }
  }
  return [...merged.values()];
}

function isPlanningNotice(notice: WorkspaceNotification): boolean {
  return notice.kind.startsWith("planning_");
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
  icon: React.ReactNode;
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
