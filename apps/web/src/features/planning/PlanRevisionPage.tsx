import { AlertTriangle, ArrowLeft, CheckCircle2, RefreshCw } from "lucide-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";

import { ApiProblem } from "@/shared/api/client";

import {
  buildConfirmPlanRevisionCommand,
  executePlanningCommand,
  getPlanRevisionDiff,
  getPlanningRequest,
  planRevisionDiffQueryKey,
  planningRequestQueryKey,
} from "./api";
import { getTask, taskGraphQueryKey, taskQueryKey } from "@/features/tasks/api";
import type { ConfirmPlanRevisionPayload, PlanningRequest, RevisionDiff } from "./types";

export function PlanRevisionPage() {
  const {
    workspaceId = "",
    rootTaskId = "",
    planningRequestId = "",
  } = useParams<{
    workspaceId: string;
    rootTaskId: string;
    planningRequestId: string;
  }>();
  const queryClient = useQueryClient();
  const [pending, setPending] = useState(false);
  const [commandError, setCommandError] = useState<string | null>(null);
  const [commandAccepted, setCommandAccepted] = useState(false);

  const requestQuery = useQuery({
    queryKey: planningRequestQueryKey(workspaceId, planningRequestId),
    queryFn: ({ signal }) => getPlanningRequest(workspaceId, planningRequestId, signal),
    enabled: Boolean(workspaceId && planningRequestId),
  });
  const request = requestQuery.data;
  const diffEnabled = request !== undefined && isRevisionDiffState(request.status);
  const diffQuery = useQuery({
    queryKey: planRevisionDiffQueryKey(workspaceId, planningRequestId),
    queryFn: ({ signal }) => getPlanRevisionDiff(workspaceId, planningRequestId, signal),
    enabled: diffEnabled,
  });
  const taskQuery = useQuery({
    queryKey: taskQueryKey(workspaceId, rootTaskId),
    queryFn: ({ signal }) => getTask(workspaceId, rootTaskId, signal),
    enabled: Boolean(workspaceId && rootTaskId),
  });

  if (requestQuery.isPending) {
    return <PlanRevisionState title="正在读取计划调整" detail="正在恢复当前方案和变更范围。" />;
  }
  if (requestQuery.isError || request === undefined) {
    return (
      <PlanRevisionState
        title="无法读取计划调整"
        detail="控制面没有返回可用的 Revision 状态。"
        error
        onRetry={() => void requestQuery.refetch()}
      />
    );
  }
  if (request.kind !== "revision") {
    return (
      <PlanRevisionState
        title="不是计划调整"
        detail="这个请求不是根任务下的 Revision。"
        error
      />
    );
  }
  const revisionRequest = request;

  const diff = diffQuery.data;
  const rootTitle = taskQuery.data?.title ?? rootTitleFromDiff(diff) ?? "根任务";
  const isStale = request.status === "stale"
    || request.status === "conflicted"
    || request.revision_freshness?.base_fresh === false
    || diff?.freshness.base_fresh === false;
  const staleReasons = request.revision_freshness !== undefined
    && request.revision_freshness !== null
    && request.revision_freshness.stale_reasons.length > 0
    ? request.revision_freshness.stale_reasons
    : ["活动计划发生变化"];
  const baseFresh = !isStale
    && diff?.freshness.base_fresh !== false;
  const isProposalReady = isRevisionReady(request.status);
  const canConfirm = isProposalReady
    && diff !== undefined
    && baseFresh
    && request.current_proposal_revision !== null
    && request.current_proposal_content_hash !== null;

  async function confirmRevision() {
    if (!canConfirm || diff === undefined || revisionRequest.current_proposal_revision === null || revisionRequest.current_proposal_content_hash === null) {
      return;
    }
    const payload: ConfirmPlanRevisionPayload = {
      expected_proposal_revision: revisionRequest.current_proposal_revision,
      expected_proposal_content_hash: revisionRequest.current_proposal_content_hash,
      expected_diff_hash: diff.diff_hash,
      expected_base_snapshot_id: diff.base.plan_snapshot_id,
      expected_base_content_hash: diff.base.plan_content_hash,
      profile_id: revisionRequest.planning_profile_id,
      profile_version: revisionRequest.planning_profile_version,
    };
    setPending(true);
    setCommandError(null);
    setCommandAccepted(false);
    try {
      await executePlanningCommand(buildConfirmPlanRevisionCommand({
        workspaceId,
        requestId: planningRequestId,
        expectedVersion: revisionRequest.version,
        payload,
      }));
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: planningRequestQueryKey(workspaceId, planningRequestId) }),
        queryClient.invalidateQueries({ queryKey: planRevisionDiffQueryKey(workspaceId, planningRequestId) }),
        queryClient.invalidateQueries({ queryKey: taskQueryKey(workspaceId, rootTaskId) }),
        queryClient.invalidateQueries({ queryKey: taskGraphQueryKey(workspaceId, rootTaskId) }),
        queryClient.invalidateQueries({ queryKey: ["task-overview", workspaceId, rootTaskId] }),
        queryClient.invalidateQueries({ queryKey: ["plan-transitions", workspaceId, rootTaskId] }),
      ]);
      setCommandAccepted(true);
    } catch (error) {
      setCommandError(error instanceof ApiProblem ? error.detail : "确认请求没有完成，请重新读取当前状态。");
    } finally {
      setPending(false);
    }
  }

  return (
    <section className="plan-revision-page" aria-labelledby="plan-revision-title">
      <div className="breadcrumb plan-revision-page__breadcrumb">
        <Link to={`/workspaces/${workspaceId}/tasks/${rootTaskId}`}>
          <ArrowLeft size={13} aria-hidden="true" />任务
        </Link>
        <span>/</span>
        <span>{rootTitle}</span>
        <span>/</span>
        <span>计划调整</span>
      </div>
      <header className="plan-revision-page__heading">
        <div>
          <span className="eyebrow">PLAN REVISION · {generationLabel(diff, request)}</span>
          <h1 id="plan-revision-title">调整任务计划</h1>
          <p>{rootTitle} 的整体执行结构变更，确认后才会成为新的活动计划。</p>
        </div>
        <div className="plan-revision-page__heading-meta">
          <span className={`plan-revision-status plan-revision-status--${request.status}`}>
            {revisionStatusLabel(request.status)}
          </span>
          <span className="mono-label">请求 V{request.version} · Proposal V{request.current_proposal_revision ?? "-"}</span>
        </div>
      </header>

      {commandError ? (
        <div className="plan-revision-page__message plan-revision-page__message--error" role="alert">
          <AlertTriangle size={15} aria-hidden="true" />
          <span>{commandError}</span>
        </div>
      ) : null}
      {commandAccepted ? (
        <div className="plan-revision-page__message plan-revision-page__message--success" role="status">
          <CheckCircle2 size={15} aria-hidden="true" />
          <span>新计划已提交，Worker 将按受影响分支异步收敛。</span>
        </div>
      ) : null}

      {isStale ? (
        <section className="plan-revision-page__stale" role="alert" aria-labelledby="revision-stale-title">
          <div>
            <strong id="revision-stale-title">当前基准已过期</strong>
            <p>活动计划已经变化，必须重新读取最新 Revision 后才能确认。</p>
          </div>
          <div className="plan-revision-page__stale-reasons">
            {staleReasons.map((reason) => <span key={reason}>{reason}</span>)}
          </div>
        </section>
      ) : null}

      <div className="plan-revision-page__layout">
        <main className="plan-revision-page__main">
          {isRevisionTerminal(request.status) ? (
            <RevisionTerminalState status={request.status} />
          ) : diffQuery.isPending ? (
            <div className="plan-revision-page__loading" role="status">正在读取变更范围</div>
          ) : diffQuery.isError ? (
            <div className="plan-revision-page__loading plan-revision-page__loading--error" role="alert">
              <strong>无法读取变更范围</strong>
              <button className="quiet-button" type="button" onClick={() => void diffQuery.refetch()}>
                <RefreshCw size={14} aria-hidden="true" />重新读取
              </button>
            </div>
          ) : diff !== undefined ? (
            <RevisionOverview diff={diff} />
          ) : (
            <div className="plan-revision-page__loading" role="status">当前状态没有可审阅的变更范围。</div>
          )}

          <div className="plan-revision-page__actions">
            <button
              className="primary-button"
              type="button"
              disabled={!canConfirm || pending}
              aria-busy={pending}
              onClick={() => void confirmRevision()}
            >
              <CheckCircle2 size={15} aria-hidden="true" />
              {pending ? "正在启用新计划" : "确认并启用新计划"}
            </button>
            <Link className="quiet-button" to={`/workspaces/${workspaceId}/tasks/${rootTaskId}`}>
              返回任务
            </Link>
          </div>
        </main>

        <aside className="plan-revision-page__inspector" aria-label="计划调整信息">
          <section>
            <span className="eyebrow">REVISION BASE</span>
            <strong>Generation {request.revision_base?.plan_generation ?? diff?.base.plan_generation ?? "-"}</strong>
            <span>Root Task V{request.revision_base?.root_task_version ?? diff?.base.root_task_version ?? "-"}</span>
          </section>
          <section>
            <span className="eyebrow">POLICY</span>
            <span>{diff?.accepted_transition_policy.failure_policy ?? "等待方案"}</span>
            <span>{diff?.accepted_transition_policy.activate_immediately ? "确认后立即成为活动计划" : "确认后等待调度"}</span>
          </section>
          <section>
            <span className="eyebrow">FENCE</span>
            <span className="mono-label">{diff?.diff_hash?.slice(0, 12) ?? "等待读取"}</span>
            <span className="mono-label">{isStale ? "STALE" : "FRESH"}</span>
          </section>
        </aside>
      </div>
    </section>
  );
}

function RevisionOverview({ diff }: { diff: RevisionDiff }) {
  const entries = [
    ...diff.root_changes.map((change) => ({ path: "根任务", strategy: "修改", reason: change.field_path })),
    ...diff.subtask_changes.map((change) => ({ path: change.path, strategy: change.strategy, reason: change.reason })),
    ...diff.worker_item_changes.map((change) => ({ path: change.path, strategy: change.strategy, reason: change.reasons[0] ?? change.context_disposition })),
  ];
  return (
    <section className="plan-revision-page__overview" aria-labelledby="revision-overview-title">
      <div className="section-heading">
        <div>
          <span className="eyebrow">CHANGE SURFACE</span>
          <h2 id="revision-overview-title">这次会改变什么</h2>
        </div>
        <span className="mono-label">{entries.length} 个结构项</span>
      </div>
      <div className="plan-revision-page__impact-grid">
        {(["preserve", "patch", "continue", "create", "derive", "move", "retire"] as const).map((key) => (
          <div key={key}>
            <span>{impactLabel(key)}</span>
            <strong>{diff.impact_counts[key]}</strong>
          </div>
        ))}
      </div>
      <div className="plan-revision-page__change-list">
        {entries.length === 0 ? <p>没有可展示的结构变化。</p> : entries.map((entry) => (
          <article key={`${entry.path}:${entry.strategy}`}>
            <div>
              <strong>{entry.path}</strong>
              <span>{entry.strategy}</span>
            </div>
            <p>{entry.reason}</p>
          </article>
        ))}
      </div>
    </section>
  );
}

function PlanRevisionState({
  title,
  detail,
  error = false,
  onRetry,
}: {
  title: string;
  detail: string;
  error?: boolean;
  onRetry?: () => void;
}) {
  return (
    <section className="plan-revision-page" aria-labelledby="plan-revision-state-title">
      <div className="breadcrumb">工作区 / 任务 / 计划调整</div>
      <div className="page-heading">
        <div>
          <span className="eyebrow">PLAN REVISION</span>
          <h1 id="plan-revision-state-title">{title}</h1>
        </div>
      </div>
      <div className={`plan-revision-page__state${error ? " plan-revision-page__state--error" : ""}`} role={error ? "alert" : "status"}>
        <strong>{title}</strong>
        <p>{detail}</p>
        {onRetry ? <button className="quiet-button" type="button" onClick={onRetry}>重试读取</button> : null}
      </div>
    </section>
  );
}

function RevisionTerminalState({ status }: { status: string }) {
  const copy = status === "materialized" || status === "completed"
    ? {
        title: "新计划已启用",
        detail: "这次计划调整已经成为活动计划，当前页面保留为只读审阅记录。",
      }
    : status === "cancelled"
      ? {
          title: "计划调整已取消",
          detail: "这次 Revision 没有改变当前活动计划。",
        }
      : {
          title: "计划调整未启用",
          detail: "这次 Revision 没有成为活动计划，当前页面保留为只读记录。",
        };
  return (
    <div className="plan-revision-page__terminal" role="status">
      <strong>{copy.title}</strong>
      <p>{copy.detail}</p>
    </div>
  );
}

function isRevisionDiffState(status: string): boolean {
  return ["proposal_ready", "awaiting_confirmation", "needs_confirmation", "stale", "conflicted"].includes(status);
}

function isRevisionReady(status: string): boolean {
  return ["proposal_ready", "awaiting_confirmation", "needs_confirmation"].includes(status);
}

function isRevisionTerminal(status: string): boolean {
  return ["materialized", "completed", "cancelled", "rejected"].includes(status);
}

function revisionStatusLabel(status: string): string {
  switch (status) {
    case "proposal_ready":
    case "awaiting_confirmation":
    case "needs_confirmation":
      return "等待确认";
    case "stale":
    case "conflicted":
      return "需要重新审阅";
    case "materialized":
      return "已启用";
    case "cancelled":
      return "已取消";
    default:
      return status;
  }
}

function generationLabel(diff: RevisionDiff | undefined, request: PlanningRequest): string {
  const from = request.revision_base?.plan_generation ?? diff?.base.plan_generation;
  const to = diff?.target_descriptor_preview.target_generation;
  return from !== undefined && to !== undefined ? `Generation ${from} → ${to}` : "PENDING";
}

function rootTitleFromDiff(diff: RevisionDiff | undefined): string | undefined {
  const path = diff?.affected_branch_paths[0];
  return path?.split(" /")[0] || undefined;
}

function impactLabel(key: string): string {
  const labels: Record<string, string> = {
    preserve: "复用",
    patch: "补丁",
    continue: "续接",
    create: "新增",
    derive: "派生",
    move: "移动",
    retire: "退出",
  };
  return labels[key] ?? key;
}
