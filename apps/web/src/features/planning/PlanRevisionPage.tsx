import { AlertTriangle, ArrowLeft, CheckCircle2, RefreshCw } from "lucide-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { ApiProblem } from "@/shared/api/client";
import { LocalSessionButton } from "@/features/local-session/LocalSessionDialog";
import { ProposalAssignees } from "@/features/assignments/ProposalAssignees";
import { workContextByRootTaskQueryKey } from "@/features/context/api";

import {
  buildConfirmPlanRevisionCommand,
  executePlanningCommand,
  getPlanRevisionDiff,
  getPlanningRequest,
  listPlanningRuns,
  planRevisionDiffQueryKey,
  planningRequestQueryKey,
  planningRunsQueryKey,
  planningBriefsQueryKey,
} from "./api";
import { getTask, taskGraphQueryKey, taskQueryKey } from "@/features/tasks/api";
import { RevisionDagDiff } from "./RevisionDagDiff";
import { RevisionDecisionSummary } from "./RevisionDecisionSummary";
import { RevisionHierarchyDiff } from "./RevisionHierarchyDiff";
import { RevisionImpactPanel } from "./RevisionImpactPanel";
import type { ConfirmPlanRevisionPayload, PlanningRequest, RevisionDiff } from "./types";
import { useNativePlanRevision } from "./useNativePlanRevision";
// @ts-expect-error Explicit TSX avoids the case-insensitive helper basename collision.
import { FailedPlanningFeedback } from "./FailedPlanningFeedback.tsx";

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
  const runsQuery = useQuery({
    queryKey: planningRunsQueryKey(workspaceId, planningRequestId),
    queryFn: ({ signal }) => listPlanningRuns(workspaceId, planningRequestId, signal),
    enabled: request?.kind === "revision" && Boolean(workspaceId && planningRequestId),
    retry: false,
  });
  const refreshRevisionViews = useCallback(async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: planningRequestQueryKey(workspaceId, planningRequestId) }),
      queryClient.invalidateQueries({ queryKey: planningRunsQueryKey(workspaceId, planningRequestId) }),
      queryClient.invalidateQueries({ queryKey: planningBriefsQueryKey(workspaceId, planningRequestId) }),
      queryClient.invalidateQueries({ queryKey: workContextByRootTaskQueryKey(workspaceId, rootTaskId) }),
      queryClient.invalidateQueries({ queryKey: planRevisionDiffQueryKey(workspaceId, planningRequestId) }),
      queryClient.invalidateQueries({ queryKey: taskQueryKey(workspaceId, rootTaskId) }),
      queryClient.invalidateQueries({ queryKey: taskGraphQueryKey(workspaceId, rootTaskId) }),
      queryClient.invalidateQueries({ queryKey: ["task-overview", workspaceId, rootTaskId] }),
      queryClient.invalidateQueries({ queryKey: ["plan-transitions", workspaceId, rootTaskId] }),
    ]);
  }, [planningRequestId, queryClient, rootTaskId, workspaceId]);
  const nativeRevision = useNativePlanRevision({
    workspaceId,
    rootTaskId,
    requestId: planningRequestId,
    request,
    diff: diffQuery.data,
    rootTask: taskQuery.data,
    runs: runsQuery.data?.items,
    onRefresh: refreshRevisionViews,
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
  const isStale = nativeRevision.stale || request.status === "stale"
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
  const legacyCanConfirm = nativeRevision.mode === "legacy" && isProposalReady
    && diff !== undefined
    && baseFresh
    && request.current_proposal_revision !== null
    && request.current_proposal_content_hash !== null;
  const visibleError = nativeRevision.mode === "legacy" ? commandError : nativeRevision.error;

  async function confirmRevision() {
    if (!legacyCanConfirm || diff === undefined || revisionRequest.current_proposal_revision === null || revisionRequest.current_proposal_content_hash === null) {
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

      {visibleError ? (
        <div className="plan-revision-page__message plan-revision-page__message--error" role="alert">
          <AlertTriangle size={15} aria-hidden="true" />
          <span>{visibleError}</span>
        </div>
      ) : null}
      {commandAccepted ? (
        <div className="plan-revision-page__message plan-revision-page__message--success" role="status">
          <CheckCircle2 size={15} aria-hidden="true" />
          <span>新计划已提交，Worker 将按受影响分支异步收敛。</span>
        </div>
      ) : null}

      {nativeRevision.mode !== "legacy" && nativeRevision.nativeHint && !nativeRevision.sessionValid ? (
        <div className="plan-revision-page__message" role="status">
          <span>需要当前工作区的有效本地会话</span>
          <LocalSessionButton workspaceId={workspaceId} onAuthorized={nativeRevision.onSessionAuthorized} />
        </div>
      ) : null}

      {nativeRevision.command.original !== null ? (
        <div className="plan-revision-page__message" role="group" aria-label="原始命令恢复">
          <span>{nativeRevisionStatusLabel(nativeRevision.command.status, nativeRevision.command.original.command_type)}</span>
          {nativeRevision.command.unresolved ? (
            <button
              className="quiet-button"
              type="button"
              disabled={!nativeRevision.sessionValid || nativeRevision.command.busy}
              onClick={() => void nativeRevision.command.lookupOriginal()}
            >
              {nativeRevision.command.busy ? "正在查询" : "核对原始命令"}
            </button>
          ) : null}
        </div>
      ) : null}

      {isStale ? (
        <section className="plan-revision-page__stale" role="alert" aria-labelledby="revision-stale-title">
          <div>
            <strong id="revision-stale-title">当前基准已过期</strong>
            <p>活动计划已经变化，必须重新读取最新 Revision 后才能确认。</p>
            {nativeRevision.mode !== "legacy" ? (
              <button
                className="quiet-button"
                type="button"
                disabled={!nativeRevision.sessionValid || nativeRevision.revisitBusy
                  || nativeRevision.command.unresolved || nativeRevision.command.busy}
                onClick={() => void nativeRevision.revisit()}
              >
                {nativeRevision.revisitBusy ? "正在重新读取" : "重新审阅最新 Revision"}
              </button>
            ) : null}
          </div>
          <div className="plan-revision-page__stale-reasons">
            {staleReasons.map((reason) => <span key={reason}>{reason}</span>)}
          </div>
        </section>
      ) : null}

      <div className="plan-revision-page__layout">
        <main className="plan-revision-page__main">
          {request.status === "failed" && nativeRevision.nativeHint ? (
            <FailedPlanningFeedback scope={nativeRevision.scope} session={nativeRevision.session} request={request}
              runs={runsQuery.data?.items} command={nativeRevision.command}
              disabledReason={nativeRevision.failedFeedbackDisabledReason}
              onSubmit={nativeRevision.submitFailedFeedback} onRefresh={refreshRevisionViews} />
          ) : null}
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
            <div className="plan-revision-page__review">
              <RevisionDecisionSummary
                rootTitle={rootTitle}
                rootDescription={taskQuery.data?.description}
                diff={diff}
              />
              <RevisionImpactPanel diff={diff} />
              <RevisionHierarchyDiff diff={diff} />
              <RevisionDagDiff diff={diff} />
            </div>
          ) : (
            <div className="plan-revision-page__loading" role="status">当前状态没有可审阅的变更范围。</div>
          )}

          {nativeRevision.mode !== "legacy" && nativeRevision.nativeHint
            && !isRevisionTerminal(request.status) && nativeRevision.targets.length > 0 ? (
              <section className="plan-revision-page__review" aria-labelledby="revision-owner-title">
                <div>
                  <span className="eyebrow">REVISION OWNERS</span>
                  <h2 id="revision-owner-title">负责人</h2>
                </div>
                <ProposalAssignees
                  workspaceId={workspaceId}
                  principalId={nativeRevision.session?.principal_id ?? ""}
                  targets={nativeRevision.targets}
                  profiles={nativeRevision.profiles}
                  choices={nativeRevision.choices}
                  reusedBindings={nativeRevision.reusedBindings}
                  onChange={nativeRevision.onChoiceChange}
                  disabled={!nativeRevision.sessionValid || nativeRevision.stale
                    || nativeRevision.command.unresolved || nativeRevision.command.busy
                    || nativeRevision.command.status === "applied" && nativeRevision.command.original?.command_type === "ConfirmPlanRevision"}
                />
                {nativeRevision.missingOwnerReason ? (
                  <p role="status">{nativeRevision.missingOwnerReason}</p>
                ) : null}
              </section>
            ) : null}

          <div className="plan-revision-page__actions">
            <p className="plan-revision-page__actions-note">新计划会立即成为活动目标，Worker 随后异步同步。</p>
            {nativeRevision.mode === "legacy" ? (
              <button
                className="primary-button"
                type="button"
                disabled={!legacyCanConfirm || pending}
                aria-busy={pending}
                onClick={() => void confirmRevision()}
              >
                <CheckCircle2 size={15} aria-hidden="true" />
                {pending ? "正在启用新计划" : "确认并启用新计划"}
              </button>
            ) : (
              <>
                {nativeRevision.outcome === "command_applied" ? (
                  <div className="plan-revision-page__message" role="status">原始命令已处理，但新计划状态尚未核实。</div>
                ) : null}
                {nativeRevision.outcome === "base_conflicted" ? (
                  <div className="plan-revision-page__message plan-revision-page__message--error" role="alert">
                    活动计划基准已变化；原始确认已处理，但新计划没有启用。
                  </div>
                ) : null}
                {nativeRevision.outcome === "plan_enabled" ? (
                  <div className="plan-revision-page__message plan-revision-page__message--success" role="status">
                    <CheckCircle2 size={15} aria-hidden="true" />
                    <span>新计划已启用，Worker 将按受影响分支异步收敛。</span>
                  </div>
                ) : null}
                {nativeRevision.mode === "unavailable" ? (
                  <p role="status">{nativeRevision.disabledReason}</p>
                ) : null}
                <button
                  className="primary-button"
                  type="button"
                  disabled={!nativeRevision.canConfirm || nativeRevision.command.busy || nativeRevision.revisitBusy}
                  aria-busy={nativeRevision.command.busy}
                  onClick={() => void nativeRevision.confirm()}
                >
                  <CheckCircle2 size={15} aria-hidden="true" />
                  {nativeRevision.command.busy ? "正在确认计划调整" : "确认并启用新计划"}
                </button>
              </>
            )}
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
  return path?.split(" / ")[0] || undefined;
}

function nativeRevisionStatusLabel(status: string, commandType: string): string {
  if (commandType === "ReviseFailedPlanningRequest") {
    switch (status) {
      case "pending": return "原始补充要求仍在处理中";
      case "unknown": return "原始补充要求结果待核对";
      case "applied": return "原始补充要求已处理";
      case "rejected": return "原始补充要求被拒绝";
      default: return "原始补充要求状态不可用";
    }
  }
  switch (status) {
    case "pending": return "原始确认仍在处理中";
    case "unknown": return "原始确认结果待核对";
    case "applied": return "原始确认已处理";
    case "rejected": return "原始确认被拒绝";
    default: return "原始确认状态不可用";
  }
}
