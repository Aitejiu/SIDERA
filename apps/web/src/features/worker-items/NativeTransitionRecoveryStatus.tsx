import { CheckCircle2, CircleAlert, RefreshCcw, ShieldCheck } from "lucide-react";

import { LocalSessionButton } from "../local-session/LocalSessionDialog";
import type { NativeTransitionRecoveryOutcome, NativeTransitionRecoveryStatus as RecoveryStatus } from "./nativeTransitionRecovery";
import type { useNativeTransitionRecovery } from "./useNativeTransitionRecovery";

const statusLabels: Record<RecoveryStatus, string> = {
  idle: "尚未提交恢复策略", pending: "原恢复处理中", unknown: "原恢复结果未知",
  applied: "原恢复已处理", rejected: "原恢复被拒绝",
};
const outcomeLabels: Record<NativeTransitionRecoveryOutcome, string> = {
  recorded: "恢复请求已记录，Adapter 尚未调用", queued: "恢复已排队", branch_closed: "分支已安全关闭",
};

export function NativeTransitionRecoveryStatus({ workspaceId, actions }: {
  workspaceId: string; actions: ReturnType<typeof useNativeTransitionRecovery>;
}) {
  const { action, authority, original, error, storageError, busy } = actions;
  if (authority?.kind === "legacy") return null;
  if (!original && !error && !storageError && actions.authorized) return null;
  return <section className="worker-governance worker-item-sidebar__section" aria-label="计划同步恢复记录" aria-busy={busy}>
    <div className="worker-governance__heading"><h3><ShieldCheck size={15} aria-hidden="true" />计划同步恢复记录</h3></div>
    {original && action ? <div className="result-input-panel__action-state" role="status">
      {action.status === "applied" ? <CheckCircle2 size={14} aria-hidden="true" /> : <CircleAlert size={14} aria-hidden="true" />}
      <span>{statusLabels[action.status]}</span>
      {action.outcome ? <strong>{outcomeLabels[action.outcome]}</strong> : null}
      <span className="mono-label">{original.command_id} · Transition V{original.expected_version}</span>
    </div> : authority === null ? <span role="status">正在确认操作身份</span>
      : !actions.authorized ? <span role="status">当前操作身份未确认</span> : null}
    {error ? <div className="governance-dialog__error" role="alert"><CircleAlert size={14} aria-hidden="true" /><span>{error}</span></div> : null}
    <div className="worker-governance__actions">
      <LocalSessionButton workspaceId={workspaceId} onAuthorized={actions.onSessionAuthorized} />
      {original ? <button className="quiet-button" type="button" disabled={busy} onClick={() => void actions.lookupOriginal()}>
        <RefreshCcw size={14} aria-hidden="true" />查询原恢复
      </button> : null}
      {storageError ? <button className="quiet-button" type="button" disabled={busy} onClick={actions.retryStorage}>
        <RefreshCcw size={14} aria-hidden="true" />重试本地记录
      </button> : null}
    </div>
  </section>;
}
