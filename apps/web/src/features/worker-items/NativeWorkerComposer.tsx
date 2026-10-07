import {
  CheckCircle2,
  History,
  Info,
  MessageSquareText,
  RefreshCcw,
  Send,
  Square,
} from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";

import { LocalSessionButton } from "@/features/local-session/LocalSessionDialog";
import { useNativeIntervention } from "@/features/trace/useNativeIntervention";
import type { NativeInputStatus, NativeInterventionAction } from "@/features/trace/nativeIntervention";
import type { NativeWorkerContextView } from "@/features/assignments/types";
import type { Execution } from "@/features/trace/types";

import type { WorkerActivity, WorkerWorkspace } from "./types";

export function NativeWorkerComposer({
  workspaceId,
  workerItemId,
  workspace,
  execution,
  nativeContext,
  activities = [],
  onRefresh,
}: {
  workspaceId: string;
  workerItemId: string;
  workspace: WorkerWorkspace;
  execution: Execution | undefined;
  nativeContext: NativeWorkerContextView | undefined;
  activities?: readonly WorkerActivity[];
  onRefresh: () => Promise<void>;
}) {
  const intervention = useNativeIntervention({
    workspaceId,
    workerItemId,
    workspace,
    execution,
    nativeContext,
    onRefresh,
  });
  const [selectedActivityId, setSelectedActivityId] = useState<string | null>(null);
  const address = nativeContext?.context ?? null;
  const nativeSourceKnown = nativeContext?.workspace_id === workspaceId
    && nativeContext.worker_item_id === workerItemId
    && nativeContext.execution_mode === "native_profile"
    && address?.worker_item_id === workerItemId;
  const draftLoaded = intervention.inputAction !== null && intervention.stopAction !== null;
  const canEditText = intervention.sessionAuthorized && draftLoaded && intervention.storageError === null
    && nativeSourceKnown;
  const supersededInputs = activities.filter((activity) => activity.source_type === "execution_input"
    && activity.kind === "user_input"
    && activity.delivery_status === "superseded"
    && typeof activity.body === "string"
    && activity.body.trim().length > 0);
  const selectedActivity = supersededInputs.find((activity) => activity.activity_id === selectedActivityId);
  const canLoadSelected = selectedActivity !== undefined
    && typeof selectedActivity.body === "string"
    && selectedActivity.body.trim().length > 0
    && intervention.canLoadHistoricalInput;
  const rootTaskId = workspace.worker_item.root_task_id;
  const subtaskKey = workspace.current_placement?.subtask_key;
  const targetName = workspace.current_runtime?.worker_display_name ?? "当前 Agent 未接入";
  const inputStatus = intervention.inputObservation?.unavailable
    ? "投递状态暂不可用"
    : intervention.inputObservation?.status === null
      ? "尚未找到原生投递记录"
      : intervention.inputObservation?.status === undefined
        ? intervention.inputAction?.original === null || intervention.inputAction === null
          ? "尚未创建输入命令"
          : "投递状态未读取"
        : inputDeliveryLabel(intervention.inputObservation.status);
  const stopStatus = stopExecutionLabel(workspaceId, workerItemId, execution, intervention.stopAction);
  const commandStatus = commandStatusLabel(intervention.inputAction, intervention.stopAction, intervention.busy);
  const executionProfile = savedExecutionProfile(workspaceId, workerItemId, address, nativeContext);
  const ownerProfile = currentOwnerProfile(workspaceId, workerItemId, nativeContext);
  const readOnlyMessage = !intervention.sessionAuthorized
    ? "需要本地会话才能读取当前身份范围内的介入记录。"
    : intervention.storageError !== null
      ? "本地介入记录暂不可用；保存成功前不会提交。"
      : !nativeSourceKnown
        ? "当前 native 执行上下文不可用；已有原始操作仍可查询。"
        : !intervention.nativeFactsAvailable
          ? "执行事实暂不可确认；草稿可编辑，但不会因此获得发送或停止权限。"
          : "当前 native 上下文仅支持显式复核后的介入。";

  return (
    <section className="worker-composer" aria-labelledby="native-worker-composer-title">
      <h2 className="sr-only" id="native-worker-composer-title">介入当前 native Execution</h2>
      {!canEditText ? (
        <div className="worker-composer__notice">
          <RefreshCcw size={14} aria-hidden="true" />
          <span>{readOnlyMessage}</span>
          {!intervention.sessionAuthorized ? (
            <LocalSessionButton workspaceId={workspaceId} onAuthorized={intervention.onSessionAuthorized} />
          ) : null}
          {intervention.storageError !== null ? (
            <button className="quiet-button" type="button" onClick={intervention.retryDraftStorage}>
              <RefreshCcw size={14} aria-hidden="true" />重试保存
            </button>
          ) : null}
        </div>
      ) : null}

      {supersededInputs.length > 0 ? (
        <section className="worker-composer__superseded" aria-labelledby="native-superseded-title">
          <div className="worker-composer__superseded-heading">
            <div>
              <span className="eyebrow">SUPERSEDED INPUTS</span>
              <h3 id="native-superseded-title">已失效的用户介入</h3>
            </div>
            <span className="composer-state">需明确载入并复核</span>
          </div>
          {supersededInputs.map((activity) => (
            <div className="worker-composer__superseded-item" key={activity.activity_id}>
              <blockquote>{activity.body}</blockquote>
              <button
                className="quiet-button"
                type="button"
                aria-pressed={selectedActivityId === activity.activity_id}
                title="仅选择此条 canonical WorkerItem 活动"
                onClick={() => setSelectedActivityId(activity.activity_id)}
              >
                <History size={14} aria-hidden="true" />选择此失效输入
              </button>
            </div>
          ))}
          <div className="worker-composer__notice">
            <span>载入只会替换本地草稿文本，不会发送或覆盖原始操作。</span>
            <button
              className="quiet-button"
              type="button"
              disabled={!canLoadSelected}
              onClick={() => {
                if (selectedActivity?.body) intervention.explicitResendSuperseded(selectedActivity.body);
              }}
            >
              <CheckCircle2 size={14} aria-hidden="true" />载入为新介入草稿
            </button>
          </div>
        </section>
      ) : null}

      <textarea
        className="worker-composer__input"
        rows={2}
        value={intervention.text}
        onChange={(event) => intervention.onTextChange(event.target.value)}
        placeholder="只介入当前 WorkerItem 的 native Execution"
        disabled={!canEditText}
        aria-label="介入这个 Worker"
      />

      {intervention.error ? <div className="worker-composer__error" role="alert">{intervention.error}</div> : null}

      <div className="worker-composer__actions">
        <span className="worker-composer__target">{targetName}</span>
        <span className="composer-state" role="status" aria-live="polite">{commandStatus}</span>
        <Link
          className="worker-composer__main-agent-link"
          aria-label="与主 Agent 讨论调整"
          title="与主 Agent 讨论调整"
          to={`/workspaces/${workspaceId}/chat?rootTaskId=${encodeURIComponent(rootTaskId)}${subtaskKey ? `&subtaskKey=${encodeURIComponent(subtaskKey)}` : ""}&workerItemId=${encodeURIComponent(workerItemId)}`}
        >
          <MessageSquareText size={16} aria-hidden="true" />
        </Link>
        <button
          className="stop-button"
          type="button"
          aria-label="停止 Execution"
          title="停止当前 Execution"
          disabled={!intervention.canStop || intervention.busy || intervention.storageError !== null}
          onClick={() => void intervention.requestStop()}
        >
          <Square size={14} aria-hidden="true" />
        </button>
        <button
          className="primary-button"
          type="button"
          aria-label="发送到当前 Session"
          title="发送到当前 Session"
          disabled={!intervention.canSend || intervention.text.trim().length === 0 || intervention.busy
            || intervention.storageError !== null}
          onClick={() => void intervention.send()}
        >
          <Send size={15} aria-hidden="true" />
        </button>
      </div>

      <div className="worker-composer__native-status" aria-label="原生操作状态">
        <span>输入命令：{nativeActionLabel(intervention.inputAction, "input")}</span>
        <span>实际投递：{inputStatus}</span>
        <span>Execution：{stopStatus}</span>
      </div>

      {intervention.inputAction?.original ? (
        <div className="worker-composer__notice">
          <button
            className="quiet-button"
            type="button"
            disabled={intervention.busy}
            onClick={() => void intervention.refreshInputStatus()}
          >
            <RefreshCcw size={14} aria-hidden="true" />读取原生投递状态
          </button>
          {intervention.inputObservation?.canReadMore ? (
            <button
              className="quiet-button"
              type="button"
              disabled={intervention.busy}
              onClick={() => void intervention.readMoreInputStatus()}
            >
              <History size={14} aria-hidden="true" />读取更多
            </button>
          ) : null}
          <button
            className="quiet-button"
            type="button"
            disabled={intervention.busy}
            onClick={() => void intervention.lookupOriginalInput()}
          >
            查询原始输入命令
          </button>
        </div>
      ) : null}
      {intervention.stopAction?.original ? (
          <div className="worker-composer__notice">
            <span>Stop 原始命令只读查询；不会重发新的 Stop。</span>
            <button
              className="quiet-button"
              type="button"
              disabled={intervention.busy}
              onClick={() => void intervention.lookupOriginalStop()}
            >
              查询原始 Stop 命令
            </button>
          </div>
        ) : null}

      {intervention.sessionAuthorized && draftLoaded && intervention.storageError === null ? (
        <div className="worker-composer__notice">
          <button
            className="quiet-button"
            type="button"
            disabled={intervention.busy}
            onClick={() => void intervention.reviewCurrentContext()}
          >
            <CheckCircle2 size={14} aria-hidden="true" />复核当前 context
          </button>
        </div>
      ) : null}

      <details className="worker-composer__scope">
        <summary title="查看精确 native context"><Info size={15} aria-hidden="true" /><span>查看精确 native context</span></summary>
        {address ? (
          <dl>
            <div><dt>WorkerItem</dt><dd>{address.worker_item_id}</dd></div>
            <div><dt>Plan generation</dt><dd>{address.plan_generation}</dd></div>
            <div><dt>Context version</dt><dd>{address.context_version}</dd></div>
            <div><dt>Execution</dt><dd>{address.execution_id}</dd></div>
            <div><dt>Session</dt><dd>{address.session_id} · V{address.session_version}</dd></div>
            <div><dt>Worker</dt><dd>{address.worker_id}</dd></div>
            <div><dt>Run</dt><dd>{address.run_id}</dd></div>
            <div><dt>Binding version</dt><dd>{address.binding_version}</dd></div>
            <div><dt>Execution Profile</dt><dd>{profileLabel(executionProfile)}</dd></div>
            <div><dt>Current owner Profile</dt><dd>{profileLabel(ownerProfile)}</dd></div>
          </dl>
        ) : (
          <p>当前 native context 尚未确认。</p>
        )}
        {intervention.inputAction?.original ? (
          <>
            <h4>原始输入固定快照</h4>
            <dl>
              <div><dt>命令</dt><dd>{intervention.inputAction.original.command_id}</dd></div>
              <div><dt>Execution</dt><dd>{intervention.inputAction.review?.context.execution_id ?? "不可用"}</dd></div>
              <div><dt>Session</dt><dd>{intervention.inputAction.review?.context.session_id ?? "不可用"}</dd></div>
              <div><dt>Run</dt><dd>{intervention.inputAction.review?.context.run_id ?? "不可用"}</dd></div>
              <div><dt>Profile</dt><dd>{profileLabel(intervention.inputAction.review?.profile_ref ?? null)}</dd></div>
            </dl>
          </>
        ) : null}
        {intervention.stopAction?.original ? (
          <>
            <h4>原始 Stop 固定快照</h4>
            <dl>
              <div><dt>命令</dt><dd>{intervention.stopAction.original.command_id}</dd></div>
              <div><dt>Execution</dt><dd>{intervention.stopAction.review?.context.execution_id ?? "不可用"}</dd></div>
              <div><dt>Session</dt><dd>{intervention.stopAction.review?.context.session_id ?? "不可用"}</dd></div>
              <div><dt>Run</dt><dd>{intervention.stopAction.review?.context.run_id ?? "不可用"}</dd></div>
              <div><dt>Profile</dt><dd>{profileLabel(intervention.stopAction.review?.profile_ref ?? null)}</dd></div>
            </dl>
          </>
        ) : null}
      </details>
    </section>
  );
}

function savedExecutionProfile(
  workspaceId: string,
  workerItemId: string,
  address: NativeWorkerContextView["context"],
  nativeContext: NativeWorkerContextView | undefined,
) {
  const saved = nativeContext?.execution;
  const run = nativeContext?.run;
  if (address === null || nativeContext === undefined
    || nativeContext.workspace_id !== workspaceId || nativeContext.worker_item_id !== workerItemId
    || saved === null || saved === undefined || run === null || run === undefined) return null;
  const profile = saved.profile_ref;
  if (profile === null
    || saved.workspace_id !== workspaceId || saved.worker_item_id !== workerItemId
    || saved.execution_id !== address.execution_id || saved.run_id !== address.run_id
    || profile.workspace_id !== workspaceId || run.profile_ref.workspace_id !== workspaceId
    || !sameProfileRef(profile, run.profile_ref)) return null;
  return profile;
}

function currentOwnerProfile(workspaceId: string, workerItemId: string, nativeContext: NativeWorkerContextView | undefined) {
  const binding = nativeContext?.binding;
  const profile = binding?.profile_ref;
  if (binding === null || binding === undefined || profile === null || profile === undefined
    || binding.workspace_id !== workspaceId || binding.target_type !== "worker_item"
    || binding.target_id !== workerItemId || profile.workspace_id !== workspaceId) return null;
  return profile;
}

function sameProfileRef(
  left: NonNullable<NativeWorkerContextView["execution"]>["profile_ref"],
  right: NonNullable<NativeWorkerContextView["run"]>["profile_ref"],
): boolean {
  return left !== null && left.workspace_id === right.workspace_id
    && left.profile_id === right.profile_id && left.revision === right.revision;
}

function profileLabel(profile: { profile_id: string; revision: number } | null | undefined): string {
  return profile === null || profile === undefined ? "不可用" : `${profile.profile_id} · r${profile.revision}`;
}

function commandStatusLabel(
  input: NativeInterventionAction | null,
  stop: NativeInterventionAction | null,
  busy: boolean,
): string {
  if (busy) return "命令处理中";
  const latest = stop !== null && stop.status !== "idle" ? stop : input;
  if (latest === null || latest.status === "idle") return "尚无原生命令";
  if (latest.status === "pending") return "命令待确认";
  if (latest.status === "unknown") return "命令状态未确认";
  if (latest.status === "rejected") return "命令已拒绝";
  return "命令已处理";
}

function nativeActionLabel(action: NativeInterventionAction | null, kind: "input" | "stop"): string {
  if (action === null || action.status === "idle") return kind === "input" ? "尚未发送" : "尚未请求";
  if (action.status === "pending") return "命令处理中";
  if (action.status === "unknown") return "状态未确认";
  if (action.status === "rejected") return "命令被拒绝";
  return kind === "input" ? "命令已处理" : "请求已记录";
}

function inputDeliveryLabel(status: NativeInputStatus): string {
  const labels: Record<NativeInputStatus, string> = {
    accepted: "已接受",
    queued: "已排队",
    dispatching: "投递中",
    delivered: "已投递",
    blocked: "已阻塞",
    failed: "投递失败",
    cancelled: "已取消",
    superseded: "已失效",
  };
  return labels[status];
}

function stopExecutionLabel(
  workspaceId: string,
  workerItemId: string,
  execution: Execution | undefined,
  stop: NativeInterventionAction | null,
): string {
  const original = stop?.original;
  const reviewed = stop?.review;
  const originalExecutionId = original?.command_type === "StopExecution" && original.target_type === "execution"
    ? original.target_id
    : null;
  const observesOriginal = execution !== undefined
    && execution.workspace_id === workspaceId
    && execution.worker_item_id === workerItemId
    && execution.id === originalExecutionId
    && reviewed?.context.execution_id === originalExecutionId
    && reviewed.context.worker_item_id === workerItemId;
  if (observesOriginal && execution.status === "stopped") return "Execution 已停止";
  if (stop?.status === "applied") return "停止请求已记录";
  if (observesOriginal && execution.stop_requested === true) return "Execution 已确认停止请求";
  if (stop?.status === "pending") return "停止请求处理中";
  if (stop?.status === "unknown") return "停止状态未确认";
  return "尚未确认停止";
}
