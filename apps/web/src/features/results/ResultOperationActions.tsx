import { useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  CircleAlert,
  GitBranch,
  History,
  RefreshCcw,
  SearchCode,
  ShieldAlert,
} from "lucide-react";

import { ApiProblem } from "@/shared/api/client";

import {
  executeTaskResultAction,
  type ResultAction,
  type ResultActionInput,
  type TaskResultOverview,
} from "./api";
import { ReworkForm } from "./ReworkForm";

type OperationMode = "rework" | "budget" | "retry" | "reintegrate" | null;

export interface ResultOperationActionsProps {
  workspaceId: string;
  rootTaskId: string;
  workerItemId: string;
  view: TaskResultOverview | undefined;
  isLoading: boolean;
  isError: boolean;
  canIntervene: boolean;
  onRetry: () => void;
  onRefresh: () => Promise<void>;
  onOpenResults: (candidateId: string | undefined) => void;
}

export function ResultOperationActions({
  workspaceId,
  rootTaskId,
  workerItemId,
  view,
  isLoading,
  isError,
  canIntervene,
  onRetry,
  onRefresh,
  onOpenResults,
}: ResultOperationActionsProps) {
  const [mode, setMode] = useState<OperationMode>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const actions = view?.available_actions.filter((action) => action.command_type !== "CompleteTask") ?? [];
  const reworkAction = actions.find((action) => action.command_type === "RequestTaskRework");
  const scopeAllowed = reworkAction !== undefined
    && (reworkAction.allowed_worker_item_ids.length === 0
      || reworkAction.allowed_worker_item_ids.includes(workerItemId));
  const inspectAction = actions.find((action) => action.command_type === "InspectResultOperation");
  const retryActions = actions.filter((action) => action.command_type === "RetryResultVerification");
  const budgetAction = actions.find((action) => action.command_type === "AuthorizeAdditionalRework");
  const reintegrateAction = actions.find((action) => action.command_type === "ReintegrateTaskResult");

  async function submit(input: ResultActionInput, action: ResultAction) {
    if (pending) return;
    setPending(true);
    setError(null);
    try {
      await executeTaskResultAction(workspaceId, action, input);
      await onRefresh();
      setMode(null);
    } catch (cause) {
      setError(cause instanceof ApiProblem ? cause.detail : cause instanceof Error ? cause.message : "结果操作没有完成，请重新读取当前状态。");
    } finally {
      setPending(false);
    }
  }

  function submitRework(input: { issueKeys: string[]; reason: string }) {
    if (reworkAction === undefined || reworkAction.candidate_id === null || reworkAction.candidate_hash === null) return;
    void submit({
      command_type: "RequestTaskRework",
      payload: {
        candidate_id: reworkAction.candidate_id,
        candidate_hash: reworkAction.candidate_hash,
        mode: "user",
        issue_keys: input.issueKeys,
        target_worker_item_ids: [workerItemId],
        impact_worker_item_ids: [workerItemId],
        reason: input.reason,
        suggestion: null,
      },
    }, reworkAction);
  }

  function submitBudget(input: { rounds: number; seconds: number; reason: string }) {
    if (budgetAction === undefined) return;
    void submit({
      command_type: "AuthorizeAdditionalRework",
      payload: {
        additional_rounds: input.rounds,
        additional_seconds: input.seconds,
        reason: input.reason,
      },
    }, budgetAction);
  }

  function submitRetry(action: ResultAction, reason: string) {
    if (action.candidate_id === null || action.candidate_hash === null) return;
    void submit({
      command_type: "RetryResultVerification",
      payload: {
        verification_operation_id: action.target_id,
        expected_operation_version: action.expected_version,
        expected_result_version: action.expected_result_version,
        candidate_id: action.candidate_id,
        candidate_hash: action.candidate_hash,
        reason,
      },
    }, action);
  }

  function submitReintegrate(input: { reason: string }) {
    if (reintegrateAction === undefined || reintegrateAction.candidate_id === null || reintegrateAction.candidate_hash === null) return;
    void submit({
      command_type: "ReintegrateTaskResult",
      payload: {
        source_candidate_id: reintegrateAction.candidate_id,
        source_candidate_hash: reintegrateAction.candidate_hash,
        replacement_result_ids: reintegrateAction.replacement_result_ids,
        expected_result_version: reintegrateAction.expected_result_version,
        target_observation_id: null,
        reason: input.reason,
      },
    }, reintegrateAction);
  }

  return (
    <section className="worker-item-sidebar__section result-operation-actions" aria-label="结果恢复与局部返工" data-root-task-id={rootTaskId}>
      <div className="result-operation-actions__heading">
        <div>
          <span className="eyebrow">RESULT OPERATIONS</span>
          <h3><GitBranch size={15} aria-hidden="true" />结果操作</h3>
        </div>
        {view?.result_version ? <span className="mono-label">RESULT V{view.result_version}</span> : null}
      </div>
      {isLoading ? <span className="muted-copy" role="status">读取结果操作</span> : null}
      {isError ? (
        <div className="result-operation-actions__state" role="alert">
          <CircleAlert size={14} aria-hidden="true" />
          <span>结果操作暂不可用</span>
          <button className="quiet-button" type="button" onClick={onRetry}><RefreshCcw size={13} aria-hidden="true" />重试</button>
        </div>
      ) : null}
      {view && !isLoading && !isError ? (
        <>
          {view.rework_budget ? (
            <div className="result-operation-actions__budget">
              <span>自动返工额度</span>
              <strong>{view.rework_budget.auto_rounds_used} / {view.rework_budget.auto_rounds_limit} 轮</strong>
              <small>累计预算 · {formatSeconds(view.rework_budget.auto_seconds_limit)}</small>
            </div>
          ) : null}
          {mode === "rework" && reworkAction && scopeAllowed ? (
            <ReworkForm
              view={view}
              action={reworkAction}
              workerItemId={workerItemId}
              pending={pending}
              error={error}
              onSubmit={submitRework}
              onCancel={() => { setMode(null); setError(null); }}
            />
          ) : mode === "budget" && budgetAction ? (
            <BudgetForm
              pending={pending}
              error={error}
              onSubmit={submitBudget}
              onCancel={() => { setMode(null); setError(null); }}
            />
          ) : mode === "retry" && retryActions[0] ? (
            <RecoveryReasonForm
              title="重试检查"
              submitLabel="提交重试检查"
              pending={pending}
              error={error}
              onSubmit={(reason) => submitRetry(retryActions[0]!, reason)}
              onCancel={() => { setMode(null); setError(null); }}
            />
          ) : mode === "reintegrate" && reintegrateAction ? (
            <RecoveryReasonForm
              title="重新集成"
              submitLabel="提交重新集成"
              pending={pending}
              error={error}
              onSubmit={(reason) => submitReintegrate({ reason })}
              onCancel={() => { setMode(null); setError(null); }}
            />
          ) : (
            <div className="result-operation-actions__buttons">
              {reworkAction && scopeAllowed && canIntervene ? (
                <button className="quiet-button" type="button" onClick={() => { setMode("rework"); setError(null); }}>
                  <RefreshCcw size={14} aria-hidden="true" />局部返工
                </button>
              ) : null}
              {reworkAction && !scopeAllowed ? (
                <div className="result-operation-actions__revision" role="status">
                  <ShieldAlert size={14} aria-hidden="true" />
                  <span>当前 WorkerItem 不在可返工范围，需要主 Agent / Revision 调整</span>
                </div>
              ) : null}
              {budgetAction && canIntervene ? (
                <button className="quiet-button" type="button" onClick={() => { setMode("budget"); setError(null); }}>
                  <GitBranch size={14} aria-hidden="true" />增加返工额度
                </button>
              ) : null}
              {retryActions.length > 0 && canIntervene ? (
                <button className="quiet-button" type="button" onClick={() => { setMode("retry"); setError(null); }}>
                  <RefreshCcw size={14} aria-hidden="true" />重试检查
                </button>
              ) : null}
              {reintegrateAction && canIntervene ? (
                <button className="quiet-button" type="button" onClick={() => { setMode("reintegrate"); setError(null); }}>
                  <GitBranch size={14} aria-hidden="true" />重新集成
                </button>
              ) : null}
              {inspectAction ? (
                <button
                  className="quiet-button"
                  type="button"
                  onClick={() => onOpenResults(inspectAction.candidate_id ?? undefined)}
                >
                  <SearchCode size={14} aria-hidden="true" />核对应用状态
                </button>
              ) : null}
              {actions.length === 0 ? <span className="muted-copy">当前没有可执行的结果操作</span> : null}
              {!canIntervene && actions.some((action) => action.command_type !== "InspectResultOperation") ? (
                <div className="result-operation-actions__readonly">
                  <AlertTriangle size={14} aria-hidden="true" />当前 WorkerItem 只读，局部修改需主 Agent / Revision
                </div>
              ) : null}
            </div>
          )}
          {error && mode === null ? <div className="result-operation-actions__error" role="alert"><AlertTriangle size={14} aria-hidden="true" />{error}</div> : null}
        </>
      ) : null}
    </section>
  );
}

function BudgetForm({
  pending,
  error,
  onSubmit,
  onCancel,
}: {
  pending: boolean;
  error: string | null;
  onSubmit: (input: { rounds: number; seconds: number; reason: string }) => void;
  onCancel: () => void;
}) {
  const [rounds, setRounds] = useState("1");
  const [seconds, setSeconds] = useState("300");
  const [reason, setReason] = useState("");
  const valid = Number(rounds) >= 0 && Number(seconds) >= 0 && reason.trim().length > 0 && (Number(rounds) > 0 || Number(seconds) > 0);
  return (
    <form className="result-operation-form" aria-label="增加返工额度" onSubmit={(event) => {
      event.preventDefault();
      if (valid) onSubmit({ rounds: Number(rounds), seconds: Number(seconds), reason: reason.trim() });
    }}>
      <div className="result-operation-form__heading"><div><span className="eyebrow">BUDGET</span><h4>增加返工额度</h4></div><History size={15} aria-hidden="true" /></div>
      <label><span>追加轮数</span><input aria-label="追加轮数" type="number" min="0" step="1" value={rounds} onChange={(event) => setRounds(event.target.value)} /></label>
      <label><span>追加秒数</span><input aria-label="追加秒数" type="number" min="0" step="60" value={seconds} onChange={(event) => setSeconds(event.target.value)} /></label>
      <label><span>额度调整原因</span><textarea aria-label="额度调整原因" rows={2} value={reason} onChange={(event) => setReason(event.target.value)} /></label>
      {error ? <div className="result-operation-form__error" role="alert">{error}</div> : null}
      <div className="result-operation-form__actions"><button className="quiet-button" type="button" onClick={onCancel} disabled={pending}>取消</button><button className="primary-button" type="submit" disabled={!valid || pending}>{pending ? "正在提交" : "提交额度调整"}</button></div>
    </form>
  );
}

function RecoveryReasonForm({
  title,
  submitLabel,
  pending,
  error,
  onSubmit,
  onCancel,
}: {
  title: string;
  submitLabel: string;
  pending: boolean;
  error: string | null;
  onSubmit: (reason: string) => void;
  onCancel: () => void;
}) {
  const [reason, setReason] = useState("");
  const valid = reason.trim().length > 0;
  return (
    <form className="result-operation-form" aria-label={title} onSubmit={(event) => {
      event.preventDefault();
      if (valid) onSubmit(reason.trim());
    }}>
      <div className="result-operation-form__heading"><div><span className="eyebrow">RECOVERY</span><h4>{title}</h4></div><CheckCircle2 size={15} aria-hidden="true" /></div>
      <label><span>操作原因</span><textarea aria-label="操作原因" rows={3} value={reason} onChange={(event) => setReason(event.target.value)} placeholder="说明本次恢复的人工依据" /></label>
      {error ? <div className="result-operation-form__error" role="alert">{error}</div> : null}
      <div className="result-operation-form__actions"><button className="quiet-button" type="button" onClick={onCancel} disabled={pending}>取消</button><button className="primary-button" type="submit" disabled={!valid || pending}>{pending ? "正在提交" : submitLabel}</button></div>
    </form>
  );
}

function formatSeconds(value: number): string {
  if (value < 60) return `${value} 秒`;
  return `${Math.floor(value / 60)} 分钟`;
}
