import { RefreshCcw, Send, Square } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { ApiProblem } from "@/shared/api/client";
import { useWorkspaceStreamOptional } from "@/app/WorkspaceStreamProvider";

import {
  cancelExecutionInput,
  retryBlockedExecutionInput,
  sendExecutionInput,
  stopExecution,
  updatePendingExecutionInput,
} from "./api";
import {
  clearInterventionDraft,
  interventionDraftKey,
  loadInterventionDraft,
  saveInterventionDraft,
} from "./draftStore";
import type {
  Execution,
  ExecutionContextAddress,
  ExecutionInput,
  ExecutionSession,
  WorkerTrace,
} from "./types";

export function InterventionComposer({
  workspaceId,
  rootTaskId,
  context,
  contextBranch,
  session,
  execution,
  inputs,
  traceFresh,
  onRefresh,
}: {
  workspaceId: string;
  rootTaskId: string;
  context: ExecutionContextAddress | null;
  contextBranch: WorkerTrace["context_branch"];
  session: ExecutionSession | undefined;
  execution: Execution | undefined;
  inputs: ExecutionInput[];
  traceFresh: boolean;
  onRefresh: () => Promise<void>;
}) {
  const stream = useWorkspaceStreamOptional();
  const storageKey = useMemo(
    () => context === null ? null : interventionDraftKey(workspaceId, context),
    [context, workspaceId],
  );
  const [draft, setDraft] = useState(() =>
    storageKey === null ? { text: "", clientIdempotencyKey: "" } : loadInterventionDraft(storageKey),
  );
  const [status, setStatus] = useState("idle");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [awaitingClientKey, setAwaitingClientKey] = useState<string | null>(null);
  const matchingInput = inputs.find((item) => item.client_idempotency_key === draft.clientIdempotencyKey);
  const streamReady = stream === null || stream.streamState.status === "live";
  const writable = context !== null && session !== undefined && execution !== undefined && traceFresh && streamReady;
  const pendingInput = matchingInput?.status === "queued" || matchingInput?.status === "blocked" ? matchingInput : undefined;
  const matchedAwaitingInput = awaitingClientKey !== null && matchingInput !== undefined;
  const visibleDraftText = matchedAwaitingInput ? "" : draft.text;
  const visibleStatus = matchedAwaitingInput
    ? matchingInput.status === "delivered" ? "worker-responded" : matchingInput.status
    : status;

  useEffect(() => {
    if (storageKey !== null && !matchedAwaitingInput) {
      saveInterventionDraft(storageKey, draft);
    }
  }, [draft, matchedAwaitingInput, storageKey]);

  useEffect(() => {
    if (!matchedAwaitingInput || storageKey === null) {
      return;
    }
    clearInterventionDraft(storageKey);
  }, [matchedAwaitingInput, storageKey]);

  const handleSend = async () => {
    if (!writable || context === null || session === undefined || execution === undefined || draft.text.trim() === "") {
      return;
    }
    setStatus("sending");
    setErrorMessage(null);
    try {
      await onRefresh();
      if (pendingInput?.status === "queued") {
        await updatePendingExecutionInput(
          workspaceId,
          session,
          execution.id,
          draft.clientIdempotencyKey,
          draft.text,
        );
      } else {
        await sendExecutionInput(
          workspaceId,
          session,
          context,
          draft.clientIdempotencyKey,
          draft.text,
        );
      }
      await onRefresh();
      setAwaitingClientKey(draft.clientIdempotencyKey);
      setStatus("accepted");
    } catch (error) {
      setStatus("rejected");
      setErrorMessage(describeError(error));
    }
  };

  const handleCancel = async () => {
    if (!writable || pendingInput?.status !== "queued" || session === undefined || execution === undefined) return;
    setStatus("canceling");
    try {
      await cancelExecutionInput(workspaceId, session, execution.id, draft.clientIdempotencyKey);
      await onRefresh();
      setStatus("canceled");
    } catch (error) {
      setStatus("rejected");
      setErrorMessage(describeError(error));
    }
  };

  const handleRetry = async () => {
    if (!writable || pendingInput?.status !== "blocked" || session === undefined || execution === undefined) return;
    setStatus("retrying");
    try {
      await retryBlockedExecutionInput(workspaceId, session, execution.id, draft.clientIdempotencyKey);
      await onRefresh();
      setStatus("retry-queued");
    } catch (error) {
      setStatus("rejected");
      setErrorMessage(describeError(error));
    }
  };

  const handleStop = async () => {
    if (!writable || execution === undefined) return;
    if (typeof window !== "undefined" && !window.confirm("确认停止这一次精确 Execution？")) return;
    setStatus("stopping");
    try {
      await stopExecution(workspaceId, execution);
      await onRefresh();
      setStatus("stop-requested");
    } catch (error) {
      setStatus("rejected");
      setErrorMessage(describeError(error));
    }
  };

  return (
    <section className="intervention-composer" aria-labelledby="intervention-title">
      <div className="section-heading">
        <div>
          <span className="eyebrow">EXACT INTERVENTION</span>
          <h2 id="intervention-title">介入当前 Session</h2>
        </div>
        <span className={`composer-state composer-state--${visibleStatus}`}>{statusLabel(visibleStatus)}</span>
      </div>
      {context === null ? <div className="composer-notice">当前 trace 没有可验证的完整上下文地址，写入已关闭。</div> : (
        <div className="intervention-address" role="group" aria-label="精确执行地址">
          <AddressValue label="Plan" value={context.plan_snapshot_id} />
          <AddressValue label="Root" value={context.root_task_id} />
          <AddressValue label="WorkItem" value={context.work_item_key} />
          <AddressValue label="Slot" value={context.worker_slot_key} />
          <AddressValue label="Worker" value={context.worker_id} />
          <AddressValue label="Execution" value={context.execution_id} />
          <AddressValue label="Session" value={context.session_id} />
          <AddressValue label="Version" value={session === undefined ? "--" : String(session.version)} />
        </div>
      )}
      <textarea
        className="intervention-input"
        value={visibleDraftText}
        onChange={(event) => {
          setAwaitingClientKey(null);
          setDraft((current) => ({ ...current, text: event.target.value }));
        }}
        placeholder="输入只会作用于这个精确 Session"
        disabled={!writable}
        aria-label="介入内容"
      />
      {!traceFresh ? <div className="composer-notice"><RefreshCcw size={14} />轨迹正在刷新，刷新完成前不会发送。</div> : null}
      {!streamReady ? <div className="composer-notice">实时连接陈旧，已暂停写入。</div> : null}
      {errorMessage ? <div className="composer-error" role="alert">{errorMessage}</div> : null}
      <div className="composer-actions">
        <button className="primary-button" type="button" onClick={() => void handleSend()} disabled={!writable || draft.text.trim() === "" || status === "sending"}>
          <Send size={15} aria-hidden="true" />{pendingInput?.status === "queued" ? "更新排队输入" : "发送到当前 Session"}
        </button>
        {pendingInput?.status === "queued" ? <button className="quiet-button" type="button" onClick={() => void handleCancel()} disabled={!writable}><span>取消排队</span></button> : null}
        {pendingInput?.status === "blocked" ? <button className="quiet-button" type="button" onClick={() => void handleRetry()} disabled={!writable}><span>重试输入</span></button> : null}
        <button className="stop-button" type="button" onClick={() => void handleStop()} disabled={!writable || execution?.stop_requested === true}>
          <Square size={14} aria-hidden="true" />停止 Execution
        </button>
        {contextBranch?.replay_capability === "portable_replay" && execution?.source_execution_id && execution.source_session_id ? (
          <Link className="quiet-button" to={`/workspaces/${workspaceId}/tasks/${rootTaskId}/executions/${execution.source_execution_id}/sessions/${execution.source_session_id}/trace`}>
            Portable Replay
          </Link>
        ) : null}
      </div>
    </section>
  );
}

function AddressValue({ label, value }: { label: string; value: string }) {
  return <span><small>{label}</small><code>{value.length > 20 ? `${value.slice(0, 8)}…${value.slice(-6)}` : value}</code></span>;
}

function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    idle: "待发送",
    sending: "发送中",
    accepted: "已接受",
    queued: "已排队",
    delivered: "已投递",
    "worker-responded": "Worker 已响应",
    blocked: "已阻塞",
    rejected: "已拒绝",
    canceled: "已取消",
    "retry-queued": "已重试排队",
    "stop-requested": "停止已请求",
  };
  return labels[status] ?? status;
}

function describeError(error: unknown): string {
  if (error instanceof ApiProblem) {
    if (error.code === "execution_context_changed") {
      const fields = Array.isArray(error.details.changed_fields)
        ? error.details.changed_fields.filter((field): field is string => typeof field === "string")
        : [];
      return `执行地址已变化：${fields.join("、") || "请刷新当前 Session"}。草稿已保留，没有自动切换目标。`;
    }
    return error.detail;
  }
  return error instanceof Error ? error.message : "命令未能完成";
}
