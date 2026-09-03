import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, CircleAlert, GitBranch, RefreshCcw, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link, useLocation, useParams } from "react-router-dom";

import { useWorkspaceStreamOptional } from "@/app/WorkspaceStreamProvider";
import { AsyncState } from "@/shared/ui/AsyncState";
import { StatusMark } from "@/shared/ui/StatusMark";

import {
  getTaskGraph,
  taskActivityQueryKey,
  taskGraphQueryKey,
  taskOverviewDetailQueryKey,
  taskOverviewQueryKey,
  taskQueryKey,
} from "../tasks/api";
import { subtaskWorkspaceQueryKey } from "../subtasks/api";
import { collaborationDecisionsQueryKey, collaborationDetailQueryKey } from "../collaboration/api";
import { planningRequestsQueryKey } from "../planning/api";
import { executionQueryKey, getExecution } from "../trace/api";
import {
  getHandoffCandidates,
  getWorkerItemWorkspace,
  getWorkerTransition,
  listWorkerItemActivity,
  recoverWorkerTransition,
  handoffCandidatesQueryKey,
  workerActivityQueryKey,
  workerWorkspaceQueryKey,
} from "./api";
import { WorkerComposer } from "./WorkerComposer";
import { HandoffDialog } from "./HandoffDialog";
import { HandoffProgress } from "./HandoffProgress";
import { WorkerSidebar } from "./WorkerSidebar";
import { WorkerTimeline } from "./WorkerTimeline";
import { WorkerTraceDialog } from "./WorkerTraceDialog";
import type { Execution } from "../trace/types";
import type { HandoffCandidateList, RecoverWorkerTransitionPayload, WorkerActivity, WorkerWorkspace } from "./types";

export function WorkerItemPage() {
  const { workspaceId = "", workerItemId = "" } = useParams<{
    workspaceId: string;
    workerItemId: string;
  }>();
  const location = useLocation();
  const stream = useWorkspaceStreamOptional();
  const queryClient = useQueryClient();
  const workspaceQuery = useQuery({
    queryKey: workerWorkspaceQueryKey(workspaceId, workerItemId),
    queryFn: ({ signal }) => getWorkerItemWorkspace(workspaceId, workerItemId, signal),
    enabled: Boolean(workspaceId && workerItemId),
  });
  const handoffCandidatesEnabled = Boolean(
    workspaceId
      && workerItemId
      && workspaceQuery.data?.can_intervene
      && workspaceQuery.data.current_runtime?.session_id
      && workspaceQuery.data.active_handoff == null,
  );
  const handoffCandidatesQuery = useQuery({
    queryKey: handoffCandidatesQueryKey(workspaceId, workerItemId),
    queryFn: ({ signal }) => getHandoffCandidates(workspaceId, workerItemId, signal),
    enabled: handoffCandidatesEnabled,
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
  const executionId = workspaceQuery.data?.current_runtime?.execution_id ?? "";
  const executionQuery = useQuery({
    queryKey: executionQueryKey(workspaceId, executionId),
    queryFn: ({ signal }) => getExecution(workspaceId, executionId, signal),
    enabled: Boolean(workspaceId && executionId),
  });
  useEffect(() => {
    const cursor = workspaceQuery.data?.last_change_cursor;
    if (stream !== null && cursor !== undefined) {
      stream.seedSnapshotCursor(cursor);
    }
  }, [stream, workspaceQuery.data?.last_change_cursor]);
  useEffect(() => {
    const nextPath = `${location.pathname}${location.search}${location.hash}`;
    if (window.location.pathname !== location.pathname || window.location.search !== location.search || window.location.hash !== location.hash) {
      window.history.replaceState({}, "", nextPath);
    }
  }, [location.hash, location.pathname, location.search]);
  const activity = useMemo(() => flattenActivities(activityQuery.data?.pages), [activityQuery.data?.pages]);

  const refreshWorkerItem = async () => {
    const queryKeys: Array<readonly unknown[]> = [
      workerWorkspaceQueryKey(workspaceId, workerItemId),
      workerActivityQueryKey(workspaceId, workerItemId),
      taskOverviewQueryKey(workspaceId),
      planningRequestsQueryKey(workspaceId, true),
    ];
    if (rootTaskId) {
      queryKeys.push(
        taskGraphQueryKey(workspaceId, rootTaskId),
        taskOverviewDetailQueryKey(workspaceId, rootTaskId),
        taskQueryKey(workspaceId, rootTaskId),
        taskActivityQueryKey(workspaceId, rootTaskId),
        collaborationDetailQueryKey(workspaceId, rootTaskId),
        collaborationDecisionsQueryKey(workspaceId, rootTaskId),
        subtaskWorkspaceQueryKey(
          workspaceId,
          rootTaskId,
          workspaceQuery.data?.current_placement.subtask_key ?? "",
        ),
      );
    }
    if (executionId) queryKeys.push(executionQueryKey(workspaceId, executionId));
    if (handoffCandidatesEnabled) queryKeys.push(handoffCandidatesQueryKey(workspaceId, workerItemId));
    await Promise.all(queryKeys.map((queryKey) => queryClient.invalidateQueries({ queryKey })));
  };

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
            execution={executionQuery.data}
            onRefresh={refreshWorkerItem}
            handoffCandidates={handoffCandidatesQuery.data}
            handoffCandidatesPending={handoffCandidatesQuery.isPending}
            handoffCandidatesError={handoffCandidatesQuery.error}
            onRefreshHandoffCandidates={() => handoffCandidatesQuery.refetch()}
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
  execution,
  onRefresh,
  handoffCandidates,
  handoffCandidatesPending,
  handoffCandidatesError,
  onRefreshHandoffCandidates,
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
  execution: Execution | undefined;
  onRefresh: () => Promise<void>;
  handoffCandidates: HandoffCandidateList | undefined;
  handoffCandidatesPending: boolean;
  handoffCandidatesError: unknown;
  onRefreshHandoffCandidates: () => Promise<unknown>;
}) {
  const [traceDescriptor, setTraceDescriptor] = useState<NonNullable<WorkerActivity["trace_descriptor"]> | null>(null);
  const [handoffOpen, setHandoffOpen] = useState(false);
  const workerStatus = statusFor(workspace.worker_item.status);
  const timelineActivity = useMemo(
    () => enrichWorkerActivities(activity, workspace),
    [activity, workspace],
  );
  const handoffAvailable = workspace.active_handoff == null
    && workspace.can_intervene
    && handoffCandidates?.candidates.some((candidate) => candidate.eligible) === true;
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

      <PlanSyncBanner
        workspaceId={workspace.workspace_id}
        sync={workspace.plan_sync}
        scope={`${taskTitle} / ${subtaskTitle} / ${workspace.worker_item.title}`}
        onRefresh={onRefresh}
      />

      <div className="worker-item-page__layout">
        <div className="worker-item-page__main">
          <div className="worker-item-page__scope">
            <span className="eyebrow">LONG-LIVED WORKSPACE</span>
            <strong>{workspace.worker_item.responsibility}</strong>
            <span>Execution / Session 会在这里连续呈现，历史上下文不会因换代丢失。</span>
          </div>
          {workspace.active_handoff && !handoffOpen ? (
            <HandoffProgress
              workspaceId={workspace.workspace_id}
              transitionId={workspace.active_handoff.transition_id}
              fallback={workspace.active_handoff}
              onRefresh={onRefresh}
            />
          ) : null}
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
              activities={timelineActivity}
              hasMore={hasMore}
              isLoadingMore={isLoadingMore}
              onLoadMore={onLoadMore}
              onOpenTrace={(activityItem) => setTraceDescriptor(activityItem.trace_descriptor)}
            />
          )}
          <WorkerComposer
            workspaceId={workspace.workspace_id}
            workerItemId={workspace.worker_item.id}
            workspace={workspace}
            execution={execution}
            activities={activity}
            onRefresh={onRefresh}
          />
        </div>
        <WorkerSidebar
          workspace={workspace}
          taskTitle={taskTitle}
          subtaskTitle={subtaskTitle}
          onRefresh={onRefresh}
          handoffAvailable={handoffAvailable}
          onOpenHandoff={() => setHandoffOpen(true)}
        />
      </div>
      {handoffOpen ? (
        <HandoffDialog
          workspace={workspace}
          taskTitle={taskTitle}
          subtaskTitle={subtaskTitle}
          candidates={handoffCandidates}
          candidatesPending={handoffCandidatesPending}
          candidatesError={handoffCandidatesError}
          onRefreshCandidates={onRefreshHandoffCandidates}
          onRefresh={onRefresh}
          onClose={() => setHandoffOpen(false)}
        />
      ) : null}
      {traceDescriptor ? (
        <WorkerTraceDialog
          workspaceId={workspace.workspace_id}
          descriptor={traceDescriptor}
          onClose={() => setTraceDescriptor(null)}
        />
      ) : null}
    </>
  );
}

type WorkerPlanSync = NonNullable<WorkerWorkspace["plan_sync"]>;
type RecoveryStrategy = RecoverWorkerTransitionPayload["strategy"];

function PlanSyncBanner({
  workspaceId,
  sync,
  scope,
  onRefresh,
}: {
  workspaceId: string;
  sync: WorkerPlanSync | null | undefined;
  scope: string;
  onRefresh: () => Promise<void>;
}) {
  const [dialogOpen, setDialogOpen] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  if (sync === null || sync === undefined) return null;

  const recoverable = sync.status === "blocked_unknown";
  return (
    <>
      <section className={`worker-plan-sync worker-plan-sync--${recoverable ? "attention" : sync.status}`} aria-labelledby="worker-plan-sync-title">
        <div className="worker-plan-sync__icon" aria-hidden="true"><GitBranch size={17} /></div>
        <div className="worker-plan-sync__body">
          <div className="worker-plan-sync__heading">
            <div>
              <span className="eyebrow">PLAN TRANSITION</span>
              <h2 id="worker-plan-sync-title">计划同步</h2>
            </div>
            <strong>{planSyncStatusLabel(sync.status)}</strong>
          </div>
          <div className="worker-plan-sync__facts">
            <span>{generationLabel(sync)}</span>
            <span>{planSyncActionLabel(sync.action)}</span>
            <span>Context V{sync.current_context_version} → V{sync.target_context_version}</span>
          </div>
          {sync.failure_code ? <p>冻结原因：{sync.failure_code}</p> : null}
          {sync.superseded_input_ids.length > 0 ? (
            <p>{sync.superseded_input_ids.length} 条旧输入已失效，可在下方明确选择重新发送。</p>
          ) : null}
        </div>
        {recoverable ? (
          <button className="quiet-button worker-plan-sync__recover" type="button" onClick={() => {
            setMessage(null);
            setDialogOpen(true);
          }}>
            <RefreshCcw size={14} aria-hidden="true" />恢复计划同步
          </button>
        ) : null}
        {message ? <span className="worker-plan-sync__message" role="status">{message}</span> : null}
      </section>
      {dialogOpen ? (
        <PlanSyncRecoveryDialog
          workspaceId={workspaceId}
          sync={sync}
          scope={scope}
          onClose={() => setDialogOpen(false)}
          onRecovered={(nextMessage) => {
            setDialogOpen(false);
            setMessage(nextMessage);
          }}
          onRefresh={onRefresh}
        />
      ) : null}
    </>
  );
}

function PlanSyncRecoveryDialog({
  workspaceId,
  sync,
  scope,
  onClose,
  onRecovered,
  onRefresh,
}: {
  workspaceId: string;
  sync: WorkerPlanSync;
  scope: string;
  onClose: () => void;
  onRecovered: (message: string) => void;
  onRefresh: () => Promise<void>;
}) {
  const [strategy, setStrategy] = useState<RecoveryStrategy>("reconcile_adapter");
  const [reason, setReason] = useState("");
  const [evidenceRefs, setEvidenceRefs] = useState("");
  const [status, setStatus] = useState<"idle" | "submitting">("idle");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const options: { value: RecoveryStrategy; label: string; detail: string }[] = [
    {
      value: "reconcile_adapter",
      label: "重新核对 Adapter 状态",
      detail: "重新读取外部执行结果，不改变 WorkerItem 的责任边界。",
    },
    ...(sync.source_execution_id ? [{
      value: "continue_after_confirmed_stop" as const,
      label: "确认停止后继续",
      detail: "仅在已有证据证明旧 Execution 停止后，恢复该分支。",
    }] : []),
    ...(sync.patch_id ? [{
      value: "continue_after_confirmed_patch_absence" as const,
      label: "确认补丁未落地后继续",
      detail: "仅在能证明 Context Patch 未被应用时使用。",
    }] : []),
    {
      value: "fail_closed_branch",
      label: "将分支安全关闭",
      detail: "保留未知结果和证据，停止该分支的后续推进。",
    },
  ];
  const evidence = evidenceRefs.split(",").map((value) => value.trim()).filter(Boolean);
  const canSubmit = reason.trim().length > 0 && evidence.length > 0 && status === "idle";

  const submit = async () => {
    if (!canSubmit) return;
    setStatus("submitting");
    setErrorMessage(null);
    try {
      const transition = await getWorkerTransition(workspaceId, sync.worker_transition_id);
      await recoverWorkerTransition(workspaceId, transition, {
        strategy,
        reason: reason.trim(),
        evidence_refs: evidence,
      });
      await onRefresh();
      onRecovered("恢复请求已记录，等待新的 transition 状态。");
    } catch (error) {
      setStatus("idle");
      setErrorMessage(error instanceof Error ? error.message : "恢复请求未能提交");
    }
  };

  return (
    <div className="worker-plan-recovery-backdrop">
      <section className="worker-plan-recovery" role="dialog" aria-modal="true" aria-labelledby="worker-plan-recovery-title">
        <div className="worker-plan-recovery__heading">
          <div>
            <span className="eyebrow">BOUNDED RECOVERY</span>
            <h2 id="worker-plan-recovery-title">恢复计划同步</h2>
          </div>
          <button className="icon-button" type="button" aria-label="关闭恢复窗口" title="关闭恢复窗口" onClick={onClose}>
            <X size={16} aria-hidden="true" />
          </button>
        </div>
        <div className="worker-plan-recovery__scope">
          <span className="eyebrow">影响范围</span>
          <strong>{scope}</strong>
          <span>只处理当前 WorkerItem 的计划同步，不会替换整体 Worker 排布。</span>
        </div>
        <dl className="worker-plan-recovery__facts">
          <div><dt>Transition</dt><dd>{sync.worker_transition_id}</dd></div>
          <div><dt>冻结原因</dt><dd>{sync.failure_code ?? "未知结果"}</dd></div>
          <div><dt>代际</dt><dd>{generationLabel(sync)}</dd></div>
        </dl>
        <fieldset className="worker-plan-recovery__options">
          <legend>恢复策略</legend>
          {options.map((option) => (
            <label key={option.value}>
              <input
                type="radio"
                name="worker-plan-recovery-strategy"
                value={option.value}
                checked={strategy === option.value}
                onChange={() => setStrategy(option.value)}
              />
              <span><strong>{option.label}</strong><small>{option.detail}</small></span>
            </label>
          ))}
        </fieldset>
        <label className="worker-plan-recovery__field">
          <span>恢复原因</span>
          <input value={reason} onChange={(event) => setReason(event.target.value)} aria-label="恢复原因" placeholder="说明你确认了什么事实" />
        </label>
        <label className="worker-plan-recovery__field">
          <span>证据引用</span>
          <input value={evidenceRefs} onChange={(event) => setEvidenceRefs(event.target.value)} aria-label="证据引用" placeholder="用逗号分隔，例如 event:123, log:456" />
        </label>
        {errorMessage ? <div className="worker-plan-recovery__error" role="alert"><CircleAlert size={14} aria-hidden="true" />{errorMessage}</div> : null}
        <div className="worker-plan-recovery__actions">
          <button className="quiet-button" type="button" onClick={onClose} disabled={status === "submitting"}>取消</button>
          <button className="primary-button" type="button" onClick={() => void submit()} disabled={!canSubmit}>
            {status === "submitting" ? "提交中" : "提交恢复策略"}
          </button>
        </div>
      </section>
    </div>
  );
}

function enrichWorkerActivities(
  activities: readonly WorkerActivity[],
  workspace: WorkerWorkspace,
): WorkerActivity[] {
  const result = [...activities];
  const checkpoint = workspace.latest_checkpoint;
  if (checkpoint && !result.some((item) => item.kind === "checkpoint" && item.execution_id === checkpoint.execution_id && item.title === checkpoint.title)) {
    result.push({
      activity_id: `checkpoint:${checkpoint.execution_id}:${checkpoint.sequence}`,
      source_type: "checkpoint",
      source_id: checkpoint.execution_id,
      execution_id: checkpoint.execution_id,
      session_id: null,
      occurred_at: checkpoint.occurred_at,
      kind: "checkpoint",
      title: checkpoint.title,
      summary: checkpoint.title,
      correlation_id: checkpoint.execution_id,
      body: null,
      delivery_status: null,
      artifact_ids: checkpoint.artifact_ids,
      trace_descriptor: null,
    });
  }
  const sync = workspace.plan_sync;
  if (sync) {
    const fromGeneration = sync.source_generation;
    const targetGeneration = sync.target_generation;
    const baseTime = sync.started_at ?? workspace.as_of;
    result.push({
      activity_id: `plan-generation:${sync.plan_transition_id}`,
      source_type: "plan_transition",
      source_id: sync.plan_transition_id,
      execution_id: null,
      session_id: null,
      occurred_at: baseTime,
      kind: "plan.generation",
      title: `计划切换：GEN ${fromGeneration} → GEN ${targetGeneration}`,
      summary: `WorkerItem 从第 ${fromGeneration} 代同步到第 ${targetGeneration} 代`,
      correlation_id: sync.plan_transition_id,
      body: null,
      delivery_status: null,
      artifact_ids: [],
      trace_descriptor: null,
    });
    if (sync.patch_id) {
      result.push({
        activity_id: `plan-patch:${sync.worker_transition_id}`,
        source_type: "context_patch",
        source_id: sync.patch_id,
        execution_id: sync.source_execution_id,
        session_id: sync.source_session_id,
        occurred_at: baseTime,
        kind: "plan.patch",
        title: sync.patch_acknowledged ? "Context Patch 已确认" : "Context Patch 待确认",
        summary: `Context V${sync.current_context_version} → V${sync.target_context_version}`,
        correlation_id: sync.worker_transition_id,
        body: null,
        delivery_status: sync.patch_acknowledged ? "delivered" : "queued",
        artifact_ids: [],
        trace_descriptor: null,
      });
    }
    if (sync.continuation_execution_id || sync.continuation_session_id) {
      result.push({
        activity_id: `plan-continuation:${sync.worker_transition_id}`,
        source_type: "worker_transition",
        source_id: sync.worker_transition_id,
        execution_id: sync.continuation_execution_id,
        session_id: sync.continuation_session_id,
        occurred_at: sync.completed_at ?? workspace.as_of,
        kind: "plan.continuation",
        title: "Worker 已续接",
        summary: `已在 GEN ${targetGeneration} 继续推进`,
        correlation_id: sync.worker_transition_id,
        body: null,
        delivery_status: null,
        artifact_ids: [],
        trace_descriptor: null,
      });
    }
    if (sync.status === "blocked_unknown") {
      result.push({
        activity_id: `plan-recovery:${sync.worker_transition_id}`,
        source_type: "worker_transition_recovery",
        source_id: sync.worker_transition_id,
        execution_id: null,
        session_id: null,
        occurred_at: sync.completed_at ?? workspace.as_of,
        kind: "plan.recovery",
        title: "收敛冻结：未知结果",
        summary: sync.failure_code ?? "外部执行结果无法证明",
        correlation_id: sync.worker_transition_id,
        body: "当前分支保持冻结，等待带证据的恢复策略。",
        delivery_status: "blocked",
        artifact_ids: [],
        trace_descriptor: null,
      });
    }
  }
  const handoff = workspace.active_handoff;
  if (handoff && !result.some((item) => item.source_type === "handoff_transition" && item.source_id === handoff.transition_id)) {
    const blocked = handoff.status === "blocked" || handoff.status === "blocked_unknown" || handoff.status === "failed";
    result.push({
      activity_id: `handoff:${handoff.transition_id}:${handoff.updated_at}`,
      source_type: "handoff_transition",
      source_id: handoff.transition_id,
      execution_id: handoff.target_execution_id ?? handoff.source_execution_id,
      session_id: handoff.target_session_id ?? handoff.source_session_id,
      occurred_at: handoff.updated_at,
      kind: "handoff.transition",
      title: blocked ? "Agent 切换需要关注" : handoff.phase,
      summary: blocked ? handoff.failure_code ?? "Agent 切换结果需要确认" : "WorkerItem 正在切换执行 Agent",
      correlation_id: handoff.transition_id,
      body: blocked ? "当前 Session 保持只读，等待带证据的恢复策略。" : null,
      delivery_status: blocked ? "blocked" : null,
      artifact_ids: [],
      trace_descriptor: handoff.source_trace_descriptor,
    });
  }
  return result.sort((left, right) => {
    const timeDifference = Date.parse(left.occurred_at) - Date.parse(right.occurred_at);
    if (Number.isFinite(timeDifference) && timeDifference !== 0) return timeDifference;
    return activityOrder(left.kind) - activityOrder(right.kind);
  });
}

function activityOrder(kind: string): number {
  if (kind === "plan.generation") return 1;
  if (kind === "plan.patch") return 2;
  if (kind === "checkpoint") return 3;
  if (kind === "plan.continuation") return 4;
  if (kind === "plan.recovery") return 5;
  return 0;
}

function generationLabel(sync: WorkerPlanSync): string {
  return `GEN ${sync.source_generation} → GEN ${sync.target_generation}`;
}

function planSyncStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    pending: "等待同步",
    waiting_safe_boundary: "等待安全边界",
    applying: "应用中",
    waiting_activation: "等待激活",
    converging: "同步中",
    converged: "已收敛",
    failed: "同步失败",
    blocked_unknown: "未知结果",
  };
  return labels[status] ?? status;
}

function planSyncActionLabel(action: string): string {
  const labels: Record<string, string> = {
    preserve: "保留当前 Worker",
    patch: "应用 Context Patch",
    continue: "续接当前 Worker",
    move: "迁移到新 Worker",
    retire: "退役并保留历史",
  };
  return labels[action] ?? action;
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
