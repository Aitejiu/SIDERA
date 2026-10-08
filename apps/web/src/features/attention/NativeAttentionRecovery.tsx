import { CheckCircle2, CircleAlert, RefreshCcw, ShieldCheck } from "lucide-react";

import { LocalSessionButton } from "../local-session/LocalSessionDialog";
import type { NativeAttentionStatus } from "./nativeAttentionAcknowledgement";
import type { useNativeAttentionAcknowledgement } from "./useNativeAttentionAcknowledgement";

export type NativeAttentionRecoveryProps = {
  workspaceId: string;
  actions: Pick<ReturnType<typeof useNativeAttentionAcknowledgement>,
    "authority" | "authorized" | "state" | "error" | "storageError" | "busy" | "lookupOriginal" | "retryStorage" | "onSessionAuthorized">;
};

const statusLabels: Record<NativeAttentionStatus, string> = {
  idle: "尚未提交确认", pending: "原确认处理中", unknown: "原确认结果未知",
  applied: "原确认已处理", rejected: "原确认被拒绝",
};

export function NativeAttentionRecovery({ workspaceId, actions }: NativeAttentionRecoveryProps) {
  const { state, authority, error, storageError, busy } = actions;
  const original = state?.original;
  if (authority?.kind === "legacy") return null;
  if (!original && !error && !storageError && actions.authorized) return null;
  return <section className="attention-panel attention-panel--compact" aria-label="待处理事项确认记录" aria-busy={busy}>
    <div className="section-heading"><h2><ShieldCheck size={15} aria-hidden="true" />确认记录</h2></div>
    {original && state ? <div className="result-input-panel__action-state" role="status">
      {state.outcome === "acknowledged" ? <CheckCircle2 size={14} aria-hidden="true" /> : <CircleAlert size={14} aria-hidden="true" />}
      <span>{statusLabels[state.status]}</span>
      {state.outcome === "acknowledged" ? <strong>已确认处理</strong>
        : state.status === "applied" ? <span>结果尚未映射</span> : null}
    </div> : authority === null ? <span role="status">正在确认操作身份</span>
      : !actions.authorized ? <span role="status">当前操作身份未确认</span> : null}
    {error ? <div className="attention-panel__error" role="alert"><CircleAlert size={14} aria-hidden="true" /><span>{error}</span></div> : null}
    <div className="worker-governance__actions">
      <LocalSessionButton workspaceId={workspaceId} onAuthorized={actions.onSessionAuthorized} />
      {original ? <button className="quiet-button" type="button" disabled={busy} onClick={() => void actions.lookupOriginal()}>
        <RefreshCcw size={14} aria-hidden="true" />查询原确认
      </button> : null}
      {storageError ? <button className="quiet-button" type="button" disabled={busy} onClick={actions.retryStorage}>
        <RefreshCcw size={14} aria-hidden="true" />重试本地记录
      </button> : null}
    </div>
  </section>;
}
