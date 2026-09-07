import { useQuery } from "@tanstack/react-query";
import { ArrowDown, ArrowRight, ArrowUp, CircleAlert, Clock3, FileCheck2, GitBranch, List, MessageSquareText, PackageCheck } from "lucide-react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";

import { AsyncState } from "@/shared/ui/AsyncState";
import { StatusMark, type StatusMarkKind } from "@/shared/ui/StatusMark";

import { collaborationStrategyLabel } from "../collaboration/types";
import { TaskKnowledgeSummary } from "../context/TaskKnowledgeSummary";
import { PlanConvergenceStrip } from "../tasks/PlanConvergenceStrip";
import { getSubtaskWorkspace, subtaskWorkspaceQueryKey } from "./api";
import { WorkerBoard } from "./WorkerBoard";
import { WorkerGraph } from "./WorkerGraph";
import { WorkerSummary } from "./WorkerSummary";
import type { RelatedSubtask, SubtaskWorkspace } from "./types";

export function SubtaskPage() {
  const { workspaceId, rootTaskId, subtaskKey } = useParams<{
    workspaceId: string;
    rootTaskId: string;
    subtaskKey: string;
  }>();
  const resolvedWorkspaceId = workspaceId ?? "";
  const resolvedRootTaskId = rootTaskId ?? "";
  const resolvedSubtaskKey = subtaskKey ?? "";
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const viewMode = searchParams.get("view") === "graph" ? "graph" : "board";
  const workspaceQuery = useQuery({
    queryKey: subtaskWorkspaceQueryKey(
      resolvedWorkspaceId,
      resolvedRootTaskId,
      resolvedSubtaskKey,
    ),
    queryFn: ({ signal }) => getSubtaskWorkspace(
      resolvedWorkspaceId,
      resolvedRootTaskId,
      resolvedSubtaskKey,
      signal,
    ),
    enabled: resolvedWorkspaceId.length > 0
      && resolvedRootTaskId.length > 0
      && resolvedSubtaskKey.length > 0,
  });

  const setViewMode = (nextMode: "board" | "graph") => {
    const nextParams = new URLSearchParams(searchParams);
    if (nextMode === "graph") {
      nextParams.set("view", "graph");
    } else {
      nextParams.delete("view");
    }
    setSearchParams(nextParams);
  };

  const openWorker = (workerItemId: string) => {
    navigate(`/workspaces/${encodeURIComponent(resolvedWorkspaceId)}/worker-items/${encodeURIComponent(workerItemId)}`);
  };

  const openSubtask = (nextSubtaskKey: string) => {
    navigate(
      `/workspaces/${encodeURIComponent(resolvedWorkspaceId)}/tasks/${encodeURIComponent(resolvedRootTaskId)}/subtasks/${encodeURIComponent(nextSubtaskKey)}`,
    );
  };

  return (
    <section className="subtask-page" aria-labelledby="subtask-title">
      <AsyncState
        isLoading={workspaceQuery.isPending}
        isError={workspaceQuery.isError}
        errorMessage="无法读取子任务工作区"
        retryLabel="重试读取子任务"
        onRetry={() => void workspaceQuery.refetch()}
      >
        {workspaceQuery.data ? (
          <SubtaskContent
            workspace={workspaceQuery.data}
            viewMode={viewMode}
            onViewModeChange={setViewMode}
            onOpenWorker={openWorker}
            onOpenSubtask={openSubtask}
            workspaceId={resolvedWorkspaceId}
            rootTaskId={resolvedRootTaskId}
          />
        ) : null}
      </AsyncState>
    </section>
  );
}

function SubtaskContent({
  workspace,
  viewMode,
  onViewModeChange,
  onOpenWorker,
  onOpenSubtask,
  workspaceId,
  rootTaskId,
}: {
  workspace: SubtaskWorkspace;
  viewMode: "board" | "graph";
  onViewModeChange: (mode: "board" | "graph") => void;
  onOpenWorker: (workerItemId: string) => void;
  onOpenSubtask: (subtaskKey: string) => void;
  workspaceId: string;
  rootTaskId: string;
}) {
  const status = subtaskStatus(workspace.subtask.state);
  const decisionRequiredWorkerCount = workspace.worker_items.filter(
    (worker) => worker.attention_state === "decision_required",
  ).length;
  return (
    <>
      <div className="breadcrumb">
        <Link to={`/workspaces/${workspaceId}/tasks/${rootTaskId}`}>任务</Link>
        <span aria-hidden="true"> / </span>
        <span>{workspace.task_title}</span>
        <span aria-hidden="true"> / 子任务</span>
      </div>
      <header className="subtask-page__heading">
        <div>
          <span className="eyebrow">SUBTASK WORKSPACE · GEN {workspace.plan_generation}</span>
          <h1 id="subtask-title">{workspace.subtask.title}</h1>
          <p>{workspace.subtask.goal_summary}</p>
          <p className="subtask-page__current-action">
            <span className="eyebrow">当前动作</span>
            {workspace.subtask.current_action ?? "尚未开始"}
          </p>
        </div>
        <div className="subtask-page__meta">
          <StatusMark kind={status.kind} label={status.label} />
          {decisionRequiredWorkerCount > 0 ? (
            <span className="subtask-page__decision-count">
              <CircleAlert size={13} aria-hidden="true" />待你决定 {decisionRequiredWorkerCount} 个 WorkerItem
            </span>
          ) : null}
          <span className="mono-label">验收 {workspace.subtask.evidence_satisfied} / {workspace.subtask.evidence_required}</span>
          <span className="mono-label">CURSOR {workspace.last_change_cursor}</span>
        </div>
      </header>

      <SubtaskRelations
        upstream={workspace.upstream_subtasks}
        downstream={workspace.downstream_subtasks}
        onOpen={onOpenSubtask}
      />

      <SubtaskCollaborationStrip
        collaboration={workspace.collaboration}
        onOpenSubtask={onOpenSubtask}
      />

      <PlanConvergenceStrip
        label="分支收敛"
        convergence={workspace.convergence}
      />

      <TaskKnowledgeSummary
        workspaceId={workspaceId}
        rootTaskId={rootTaskId}
        submissionTaskId={workspace.subtask.task_id ?? rootTaskId}
        sourceSubtaskId={workspace.subtask.task_id}
      />

      {workspace.attention_items.length > 0 ? (
        <section className="subtask-attention" aria-labelledby="subtask-attention-title">
          <div className="section-heading">
            <div>
              <span className="eyebrow">ATTENTION</span>
              <h2 id="subtask-attention-title">需要处理</h2>
            </div>
            <span className="mono-label">{workspace.attention_items.length} ITEMS</span>
          </div>
          <div className="subtask-attention__list">
            {workspace.attention_items.map((item) => (
              <article className="subtask-attention__item" key={item.key}>
                <div className="subtask-attention__heading">
                  <strong>{isSchedulingWait(item) ? "调度等待" : item.worker_item_title ?? workspace.subtask.title}</strong>
                  {isSchedulingWait(item) ? <span className="subtask-attention__tag">需要处理</span> : null}
                </div>
                <span>{item.waiting_details_summary || item.reason}</span>
                {isSchedulingWait(item) ? (
                  <div className="subtask-attention__meta">
                    <span>
                      <Clock3 size={13} aria-hidden="true" />
                      阻塞于 {formatBlockedSince(item.blocked_since)}
                    </span>
                    {item.requires_operator ? <strong>需要我处理</strong> : <span>等待执行节点恢复</span>}
                    {item.worker_item_id ? (
                      <button
                        className="quiet-button"
                        type="button"
                        onClick={() => onOpenWorker(item.worker_item_id!)}
                      >
                        打开 WorkerItem 工作区
                      </button>
                    ) : null}
                  </div>
                ) : null}
              </article>
            ))}
          </div>
        </section>
      ) : null}

      <div className="subtask-page__layout">
        <div className="subtask-page__main">
          <div className="subtask-view-switch" role="tablist" aria-label="Worker 工作区视图">
            <button
              className={viewMode === "board" ? "is-active" : ""}
              type="button"
              role="tab"
              aria-selected={viewMode === "board"}
              onClick={() => onViewModeChange("board")}
            >
              <List size={14} aria-hidden="true" />列表
            </button>
            <button
              className={viewMode === "graph" ? "is-active" : ""}
              type="button"
              role="tab"
              aria-selected={viewMode === "graph"}
              onClick={() => onViewModeChange("graph")}
            >
              <GitBranch size={14} aria-hidden="true" />Graph
            </button>
          </div>
          {viewMode === "board" ? (
            <WorkerBoard workers={workspace.worker_items} onOpen={onOpenWorker} />
          ) : (
            <WorkerGraph workspace={workspace} onOpen={onOpenWorker} />
          )}
          <SubtaskActivity workspace={workspace} />
        </div>
        <WorkerSummary workspace={workspace} />
      </div>
    </>
  );
}

function SubtaskCollaborationStrip({
  collaboration,
  onOpenSubtask,
}: {
  collaboration: SubtaskWorkspace["collaboration"];
  onOpenSubtask: (subtaskKey: string) => void;
}) {
  if (collaboration === null || collaboration === undefined) {
    return null;
  }

  return (
    <section className="subtask-collaboration" aria-labelledby="subtask-collaboration-title">
      <div className="subtask-collaboration__heading">
        <div>
          <span className="eyebrow">COLLABORATION FAN-IN</span>
          <h2 id="subtask-collaboration-title">输入汇聚</h2>
        </div>
        <span className="mono-label">策略 · {collaborationStrategyLabel(collaboration.effective_strategy)}</span>
      </div>

      <div className="subtask-collaboration__facts" aria-label="子任务协同进度">
        <strong>输入就绪 {collaboration.satisfied_input_count} / {collaboration.required_input_count}</strong>
        <span><PackageCheck size={14} aria-hidden="true" />已放行 {collaboration.released_worker_item_count} 个 WorkerItem</span>
        <span><ArrowRight size={14} aria-hidden="true" />等待 {collaboration.waiting_worker_item_count} 个 WorkerItem</span>
      </div>

      {collaboration.waiting_upstream_subtasks.length > 0 ? (
        <div className="subtask-collaboration__group">
          <span className="eyebrow">等待上游</span>
          <div className="subtask-collaboration__waiting-list">
            {collaboration.waiting_upstream_subtasks.map((item) => (
              <button
                key={item.subtask_key}
                type="button"
                onClick={() => onOpenSubtask(item.subtask_key)}
                aria-label={`打开上游子任务：${item.title}`}
              >
                {item.title} <ArrowRight size={13} aria-hidden="true" />
              </button>
            ))}
          </div>
        </div>
      ) : null}

      {collaboration.missing_artifact_refs.length > 0 || collaboration.missing_evidence_refs.length > 0 ? (
        <div className="subtask-collaboration__missing" aria-label="缺失协同输入">
          {collaboration.missing_artifact_refs.map((reference) => (
            <span key={`artifact:${reference}`}><PackageCheck size={13} aria-hidden="true" />缺少 Artifact · {reference}</span>
          ))}
          {collaboration.missing_evidence_refs.map((reference) => (
            <span key={`evidence:${reference}`}><FileCheck2 size={13} aria-hidden="true" />缺少 Evidence · {reference}</span>
          ))}
        </div>
      ) : (
        <span className="subtask-collaboration__ready">当前没有缺失的 Artifact 或 Evidence。</span>
      )}
    </section>
  );
}

function isSchedulingWait(
  item: SubtaskWorkspace["attention_items"][number],
): boolean {
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

function SubtaskRelations({
  upstream,
  downstream,
  onOpen,
}: {
  upstream: readonly RelatedSubtask[];
  downstream: readonly RelatedSubtask[];
  onOpen: (subtaskKey: string) => void;
}) {
  return (
    <nav className="subtask-relations" aria-label="子任务依赖关系">
      <div className="subtask-relation-group">
        <span className="eyebrow"><ArrowUp size={13} aria-hidden="true" />上游</span>
        <div>
          {upstream.length === 0 ? <span className="muted-copy">无</span> : null}
          {upstream.map((item) => (
            <button key={item.subtask_key} type="button" onClick={() => onOpen(item.subtask_key)}>
              {item.title}
            </button>
          ))}
        </div>
      </div>
      <div className="subtask-relation-group">
        <span className="eyebrow"><ArrowDown size={13} aria-hidden="true" />下游</span>
        <div>
          {downstream.length === 0 ? <span className="muted-copy">无</span> : null}
          {downstream.map((item) => (
            <button key={item.subtask_key} type="button" onClick={() => onOpen(item.subtask_key)}>
              {item.title}
            </button>
          ))}
        </div>
      </div>
    </nav>
  );
}

function SubtaskActivity({ workspace }: { workspace: SubtaskWorkspace }) {
  return (
    <section className="subtask-activity" aria-labelledby="subtask-activity-title">
      <div className="section-heading">
        <div>
          <span className="eyebrow">SUBTASK ACTIVITY</span>
          <h2 id="subtask-activity-title">最近活动</h2>
        </div>
        <span className="mono-label">{workspace.recent_activity.length} ITEMS</span>
      </div>
      {workspace.recent_activity.length === 0 ? (
        <p className="muted-copy" role="status">还没有子任务活动</p>
      ) : (
        <div className="subtask-activity__list">
          {workspace.recent_activity.slice(0, 8).map((item) => (
            <div className="subtask-activity__item" key={`${item.source_id}:${item.occurred_at}`}>
              <MessageSquareText size={14} aria-hidden="true" />
              <div>
                <strong>{item.summary}</strong>
                <span>{item.kind} · {formatTime(item.occurred_at)}</span>
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function subtaskStatus(status: string): { kind: StatusMarkKind; label: string } {
  if (status === "running" || status === "assigned" || status === "queued" || status === "retrying") {
    return { kind: "running", label: "执行中" };
  }
  if (status === "succeeded" || status === "completed") {
    return { kind: "completed", label: "已完成" };
  }
  if (status === "failed") {
    return { kind: "failed", label: "失败" };
  }
  if (status === "blocked" || status === "waiting_user" || status === "waiting_dependency") {
    return { kind: "stale", label: status === "waiting_user" ? "需要处理" : "等待" };
  }
  return { kind: "planned", label: "计划中" };
}

function formatTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}
