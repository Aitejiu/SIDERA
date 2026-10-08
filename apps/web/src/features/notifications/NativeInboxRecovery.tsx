import { CheckCircle2, CircleAlert, RefreshCcw, ShieldCheck } from "lucide-react";

import { LocalSessionButton } from "../local-session/LocalSessionDialog";
import type { NativeInboxStatus } from "./nativeInboxCommand";
import type { useNativeInboxCommand } from "./useNativeInboxCommand";

export type NativeInboxRecoveryProps = {
  workspaceId: string;
  actions: Pick<ReturnType<typeof useNativeInboxCommand>, "authority" | "authorized" | "state" | "error" | "storageError" | "busy" | "lookupOriginal" | "retryStorage" | "onSessionAuthorized">;
};
const statusLabels: Record<NativeInboxStatus, string> = {
  idle: "尚未提交操作", pending: "原操作处理中", unknown: "原操作结果未知", applied: "原操作已处理", rejected: "原操作被拒绝",
};
export function NativeInboxRecovery({ workspaceId, actions }: NativeInboxRecoveryProps) {
  const { state, authority, error, storageError, busy } = actions;
  const original = state?.original;
  if (authority?.kind === "legacy") return null;
  if (!original && !error && !storageError && actions.authorized) return null;
  const outcome = state?.outcome;
  return <section className="attention-panel attention-panel--compact" aria-label="信箱操作记录" aria-busy={busy}>
    <div className="section-heading"><h2><ShieldCheck size={15} aria-hidden="true" />操作记录</h2></div>
    {original && state ? <>
      <div className="result-input-panel__action-state" role="status">
        {outcome ? <CheckCircle2 size={14} aria-hidden="true" /> : <CircleAlert size={14} aria-hidden="true" />}
        <span>{statusLabels[state.status]}</span>
        <span>{state.intent?.kind === "mark_read" ? "标记已读" : state.intent?.kind === "archive" ? "归档线程" : "重新投递"}</span>
        {outcome ? <strong>{outcome.kind === "mark_read" ? "已标记已读" : outcome.kind === "archive" ? "线程已归档" : "下一轮待投递"}</strong>
          : state.status === "applied" ? <span>结果尚未映射</span> : null}
      </div>
      <div className="mono-label">原线程 · {state.intent?.threadId}</div>
    </> : authority === null ? <span role="status">正在确认操作身份</span>
      : !actions.authorized ? <span role="status">当前操作身份未确认</span> : null}
    {error ? <div className="attention-panel__error" role="alert"><CircleAlert size={14} aria-hidden="true" /><span>{error}</span></div> : null}
    <div className="worker-governance__actions">
      <LocalSessionButton workspaceId={workspaceId} onAuthorized={actions.onSessionAuthorized} />
      {original ? <button className="quiet-button" type="button" disabled={busy} onClick={() => void actions.lookupOriginal()}>
        <RefreshCcw size={14} aria-hidden="true" />查询原操作
      </button> : null}
      {storageError ? <button className="quiet-button" type="button" disabled={busy} onClick={actions.retryStorage}>
        <RefreshCcw size={14} aria-hidden="true" />重试本地记录
      </button> : null}
    </div>
  </section>;
}
