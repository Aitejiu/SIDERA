import { CheckCircle2, CircleAlert, RefreshCcw, ShieldCheck } from "lucide-react";

import { LocalSessionButton } from "../local-session/LocalSessionDialog";
import type { WorkerResultActionStatus, WorkerResultOutcome } from "./nativeWorkerResultActions";
import type { useNativeWorkerResultActions } from "./useNativeWorkerResultActions";

export type NativeWorkerResultActionRecoveryProps = {
  workspaceId: string;
  actions: Pick<ReturnType<typeof useNativeWorkerResultActions>,
    "authority" | "authorized" | "state" | "error" | "storageError" | "busy" | "lookupOriginal" | "retryStorage" | "onSessionAuthorized">;
};

const statusLabels: Record<WorkerResultActionStatus, string> = {
  idle: "尚未提交操作", pending: "原操作处理中", unknown: "原操作结果未知",
  applied: "原操作已处理", rejected: "原操作被拒绝",
};
const outcomeLabels: Record<WorkerResultOutcome, string> = {
  paused: "已暂停自动放行", approved: "Proposal 已批准", stale: "Proposal 已失效",
  rejected: "Proposal 已驳回", automatic: "已恢复自动放行", queued: "上下文组装重试已排队",
};

export function NativeWorkerResultActionRecovery({ workspaceId, actions }: NativeWorkerResultActionRecoveryProps) {
  const { state, authority, error, storageError, busy } = actions;
  const original = state?.original;
  if (!original && !error && !storageError && actions.authorized) return null;
  if (authority?.kind === "legacy") return null;
  return (
    <section className="worker-governance worker-item-sidebar__section" aria-label="结果操作记录" aria-busy={busy}>
      <div className="worker-governance__heading"><h3><ShieldCheck size={15} aria-hidden="true" />结果操作记录</h3></div>
      {original && state ? (
        <div className="result-input-panel__action-state" role="status">
          {state.status === "applied" ? <CheckCircle2 size={14} aria-hidden="true" /> : <CircleAlert size={14} aria-hidden="true" />}
          <span>{statusLabels[state.status]}</span>
          {state.outcome ? <strong>{outcomeLabels[state.outcome]}</strong> : null}
        </div>
      ) : authority === null ? <span role="status">正在确认操作身份</span>
        : !actions.authorized ? <span role="status">当前操作身份未确认</span> : null}
      {error ? <div className="governance-dialog__error" role="alert"><CircleAlert size={14} aria-hidden="true" /><span>{error}</span></div> : null}
      <div className="worker-governance__actions">
        <LocalSessionButton workspaceId={workspaceId} onAuthorized={actions.onSessionAuthorized} />
        {original ? <button className="quiet-button" type="button" disabled={busy} onClick={() => void actions.lookupOriginal()}>
          <RefreshCcw size={14} aria-hidden="true" />查询原操作
        </button> : null}
        {storageError ? <button className="quiet-button" type="button" disabled={busy} onClick={actions.retryStorage}>
          <RefreshCcw size={14} aria-hidden="true" />重试本地记录
        </button> : null}
      </div>
    </section>
  );
}
