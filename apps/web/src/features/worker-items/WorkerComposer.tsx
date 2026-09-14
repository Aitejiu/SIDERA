import { CheckCircle2, Info, MessageSquareText, RefreshCcw, Send, Square } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { useWorkspaceStreamOptional } from "@/app/WorkspaceStreamProvider";
import { ApiProblem } from "@/shared/api/client";

import {
  clearWorkerItemDraft,
  contextFingerprint,
  loadWorkerItemDraft,
  saveWorkerItemDraft,
  workerItemDraftKey,
} from "../trace/draftStore";
import { sendExecutionInput, stopExecution } from "../trace/api";
import type { Execution } from "../trace/types";
import type { WorkerActivity, WorkerWorkspace } from "./types";

export function WorkerComposer({
  workspaceId,
  workerItemId,
  workspace,
  execution,
  activities = [],
  onRefresh,
}: {
  workspaceId: string;
  workerItemId: string;
  workspace: WorkerWorkspace;
  execution: Execution | undefined;
  activities?: readonly WorkerActivity[];
  onRefresh: () => Promise<void>;
}) {
  const stream = useWorkspaceStreamOptional();
  const draftKey = useMemo(
    () => workerItemDraftKey(workspaceId, workerItemId),
    [workerItemId, workspaceId],
  );
  const context = workspace.intervention_context;
  const subtaskKey = workspace.current_placement?.subtask_key;
  const fingerprint = contextFingerprint(context);
  const [draft, setDraft] = useState(() => loadWorkerItemDraft(draftKey));
  const [status, setStatus] = useState("idle");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const supersededInputs = activities.filter(
    (activity) => activity.kind === "user_input"
      && activity.delivery_status === "superseded"
      && activity.body?.trim(),
  );
  const streamReady = stream === null
    || stream.streamState.status === "live"
    || stream.streamState.status === "connecting";
  const contextChanged = draft.text.trim().length > 0
    && draft.lastContextFingerprint.length > 0
    && draft.lastContextFingerprint !== fingerprint;
  const handoffInProgress = workspace.active_handoff !== undefined
    && workspace.active_handoff !== null
    && workspace.active_handoff.status !== "converged";
  const writable = workspace.can_intervene
    && context !== null
    && streamReady
    && !contextChanged
    && !handoffInProgress;
  const readOnlyMessage = getReadOnlyMessage(workspace, streamReady, contextChanged, handoffInProgress);

  const confirmCurrentContext = () => {
    if (context === null || fingerprint === "") return;
    setDraft((current) => ({ ...current, lastContextFingerprint: fingerprint }));
    setStatus("idle");
    setErrorMessage(null);
  };

  useEffect(() => {
    saveWorkerItemDraft(draftKey, draft);
  }, [draft, draftKey]);

  const handleSend = async () => {
    if (!writable || context === null || draft.text.trim() === "") return;
    setStatus("sending");
    setErrorMessage(null);
    try {
      await sendExecutionInput(
        workspaceId,
        { id: context.session_id, version: context.session_version },
        context,
        draft.clientIdempotencyKey,
        draft.text,
      );
      clearWorkerItemDraft(draftKey);
      setDraft(loadWorkerItemDraft(draftKey));
      setStatus("accepted");
      try {
        await onRefresh();
      } catch {
        setErrorMessage("输入已接受，但工作区刷新失败，请稍后重试刷新。草稿已清除。");
      }
    } catch (error) {
      setStatus("rejected");
      if (isContextChanged(error)) {
        setErrorMessage("上下文已变化，草稿已保留。请刷新后确认当前 Session，再手动发送。");
        try {
          await onRefresh();
        } catch {
          // The draft remains local even when refreshing the projection fails.
        }
      } else {
        setErrorMessage(describeError(error));
      }
    }
  };

  const handleStop = async () => {
    if (!writable || execution === undefined || execution.stop_requested) return;
    if (typeof window !== "undefined" && !window.confirm("确认停止这一次 Execution？")) return;
    setStatus("stopping");
    setErrorMessage(null);
    try {
      await stopExecution(workspaceId, execution);
      await onRefresh();
      setStatus("stop-requested");
    } catch (error) {
      setStatus("rejected");
      setErrorMessage(describeError(error));
    }
  };

  const handleResend = async (text: string) => {
    if (!writable || context === null || text.trim() === "") return;
    setStatus("sending");
    setErrorMessage(null);
    try {
      await sendExecutionInput(
        workspaceId,
        { id: context.session_id, version: context.session_version },
        context,
        crypto.randomUUID(),
        text,
      );
      setStatus("accepted");
      try {
        await onRefresh();
      } catch {
        setErrorMessage("输入已重新发送，但工作区刷新失败，请稍后重试刷新。");
      }
    } catch (error) {
      setStatus("rejected");
      setErrorMessage(describeError(error));
    }
  };

  return (
    <section className="worker-composer" aria-labelledby="worker-composer-title">
      <h2 className="sr-only" id="worker-composer-title">介入这个 Worker</h2>
      {supersededInputs.length > 0 ? (
        <section className="worker-composer__superseded" aria-labelledby="worker-composer-superseded-title">
          <div className="worker-composer__superseded-heading">
            <div>
              <span className="eyebrow">SUPERSEDED INPUTS</span>
              <h3 id="worker-composer-superseded-title">已失效的用户介入</h3>
            </div>
            <span className="composer-state">需明确重新发送</span>
          </div>
          {supersededInputs.map((input) => (
            <div className="worker-composer__superseded-item" key={input.activity_id}>
              <blockquote>{input.body}</blockquote>
              <button
                className="quiet-button"
                type="button"
                title="以当前 context 重新发送，不复用旧 Session"
                onClick={() => void handleResend(input.body ?? "")}
                disabled={!writable || status === "sending"}
              >
                <Send size={14} aria-hidden="true" />重新发送
              </button>
            </div>
          ))}
        </section>
      ) : null}
      <textarea
        className="worker-composer__input"
        rows={2}
        value={draft.text}
        onChange={(event) => {
          setStatus("idle");
          setErrorMessage(null);
          const nextText = event.target.value;
          setDraft((current) => ({
            ...current,
            text: nextText,
            clientIdempotencyKey: current.text.trim() === "" && nextText.trim() !== ""
              ? crypto.randomUUID()
              : current.clientIdempotencyKey,
            lastContextFingerprint: fingerprint,
          }));
        }}
        placeholder="只修改这个 Worker 负责的当前工作"
        disabled={!writable}
        aria-label="介入这个 Worker"
      />
      {!writable ? (
        <div className="worker-composer__notice">
          <RefreshCcw size={14} aria-hidden="true" />
          <span>{readOnlyMessage}</span>
          {contextChanged ? (
            <button className="quiet-button worker-composer__notice-action" type="button" onClick={confirmCurrentContext}>
              <CheckCircle2 size={14} aria-hidden="true" />确认当前 context
            </button>
          ) : null}
        </div>
      ) : null}
      {errorMessage ? <div className="worker-composer__error" role="alert">{errorMessage}</div> : null}
      <div className="worker-composer__actions">
        <span className="worker-composer__target">{workspace.current_runtime?.worker_display_name ?? "当前 Agent 未接入"}</span>
        <span className={`composer-state composer-state--${status}`} role="status">{statusLabel(status)}</span>
        <Link
          className="worker-composer__main-agent-link"
          aria-label="与主 Agent 讨论调整" title="与主 Agent 讨论调整"
          to={`/workspaces/${workspaceId}/chat?rootTaskId=${encodeURIComponent(workspace.worker_item.root_task_id)}${subtaskKey ? `&subtaskKey=${encodeURIComponent(subtaskKey)}` : ""}&workerItemId=${encodeURIComponent(workerItemId)}`}
        >
          <MessageSquareText size={16} aria-hidden="true" />
        </Link>
        <button
          className="stop-button"
          type="button"
          aria-label="停止 Execution" title="停止当前执行"
          onClick={() => void handleStop()}
          disabled={!writable || execution === undefined || execution.stop_requested === true || status === "stopping"}
        >
          <Square size={14} aria-hidden="true" />
        </button>
        <button
          className="primary-button"
          type="button"
          aria-label="发送到当前 Session" title="发送到当前 Session"
          onClick={() => void handleSend()}
          disabled={!writable || draft.text.trim() === "" || status === "sending"}
        >
          <Send size={15} aria-hidden="true" />
        </button>
      </div>
      <details className="worker-composer__scope">
        <summary title="查看精确 context"><Info size={15} aria-hidden="true" /><span>查看精确 context</span></summary>
        {context ? (
          <dl>
            <div><dt>WorkerItem</dt><dd>{context.worker_item_id}</dd></div>
            <div><dt>Plan generation</dt><dd>{context.plan_generation}</dd></div>
            <div><dt>Context version</dt><dd>{context.context_version}</dd></div>
            <div><dt>Execution</dt><dd>{context.execution_id}</dd></div>
            <div><dt>Session</dt><dd>{context.session_id} · V{context.session_version}</dd></div>
            <div><dt>Worker</dt><dd>{context.worker_id}</dd></div>
          </dl>
        ) : <span>当前没有可验证的 Session context</span>}
      </details>
    </section>
  );
}

function getReadOnlyMessage(
  workspace: WorkerWorkspace,
  streamReady: boolean,
  contextChanged: boolean,
  handoffInProgress: boolean,
): string {
  if (handoffInProgress) return "Agent 正在切换，当前 Session 暂停写入；草稿已保留。";
  if (!streamReady) return "实时连接陈旧，已暂停写入；恢复同步后才能介入。";
  if (contextChanged) return "工作 context 已变化，草稿已保留；请确认最新 context 后再发送。";
  if (workspace.read_only_reason === "worker_item_retired") return "这个 WorkerItem 已退役，只能查看历史上下文";
  if (workspace.read_only_reason === "no_active_session") return "当前没有可写入的 Session。";
  if (workspace.read_only_reason === "context_not_synchronized") return "Agent 尚未确认最新工作上下文。";
  if (workspace.read_only_reason === "adapter_unsupported") return "当前 Worker adapter 不支持介入。";
  if (workspace.read_only_reason !== null) return "当前工作区只读，暂不能介入。";
  if (workspace.intervention_context === null) return "当前没有可验证的 Session context。";
  return "当前工作区只读，暂不能介入。";
}

function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    idle: "待发送",
    sending: "发送中",
    accepted: "已接受",
    stopping: "停止中",
    "stop-requested": "停止已请求",
    rejected: "已拒绝",
  };
  return labels[status] ?? status;
}

function isContextChanged(error: unknown): boolean {
  return error instanceof ApiProblem && error.code === "execution_context_changed";
}

function describeError(error: unknown): string {
  if (error instanceof ApiProblem) return error.detail;
  return error instanceof Error ? error.message : "命令未能完成";
}
