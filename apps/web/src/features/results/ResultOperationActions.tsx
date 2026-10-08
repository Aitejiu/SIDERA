import { useLayoutEffect, useMemo, useRef, useState } from "react";
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
import { LocalSessionButton } from "@/features/local-session/LocalSessionDialog";
import { readBusinessCommandAuthority } from "@/features/local-session/businessAuthority";

import {
  executeTaskResultAction,
  type ResultAction,
  type NativeResultOperationInput,
  type TaskResultOverview,
} from "./api";
import { ReworkForm } from "./ReworkForm";
import { useNativeResultOperations } from "./useNativeResultOperations";

type OperationMode = "rework" | "budget" | "retry" | "reintegrate" | null;

export interface ResultOperationActionsProps {
  workspaceId: string;
  rootTaskId: string;
  workerItemId: string;
  view: TaskResultOverview | undefined;
  isLoading: boolean;
  isError: boolean;
  canIntervene: boolean;
  canOperateResult?: boolean;
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
  canOperateResult = canIntervene,
  onRetry,
  onRefresh,
  onOpenResults,
}: ResultOperationActionsProps) {
  const [selection, setSelection] = useState<{ key: string; mode: OperationMode; action: ResultAction } | null>(null);
  const [legacyPending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const operations = useNativeResultOperations({ workspaceId, rootTaskId, workerItemId, view, canOperateResult, onRefresh });
  const principal = operations.session?.principal_id ?? operations.authority.kind;
  const ownerKey = JSON.stringify([workspaceId, rootTaskId, workerItemId, principal]);
  const lease = useMemo(() => ({ key: ownerKey }), [ownerKey]);
  const activeLease = useRef<typeof lease | null>(null);
  const legacyLocks = useRef(new Set<typeof lease>());
  useLayoutEffect(() => {
    activeLease.current = lease;
    return () => { if (activeLease.current === lease) activeLease.current = null; };
  }, [lease]);
  const currentSelection = selection?.key === ownerKey ? selection : null;
  const mode = currentSelection?.mode ?? null;
  const reviewedAction = currentSelection?.action;
  const pending = legacyPending || operations.busy;
  const newOperationDisabled = pending || (operations.authority.kind !== "legacy" && !operations.canSubmit);
  const actions = view?.available_actions.filter((action) => action.command_type !== "CompleteTask") ?? [];
  const reworkAction = actions.find((action) => action.command_type === "RequestTaskRework");
  const scopeAllowed = reworkAction !== undefined
    && (reworkAction.allowed_worker_item_ids.length === 0
      || reworkAction.allowed_worker_item_ids.includes(workerItemId));
  const inspectAction = actions.find((action) => action.command_type === "InspectResultOperation");
  const retryActions = actions.filter((action) => action.command_type === "RetryResultVerification");
  const budgetAction = actions.find((action) => action.command_type === "AuthorizeAdditionalRework");
  const reintegrateAction = actions.find((action) => action.command_type === "ReintegrateTaskResult");

  function open(mode: OperationMode, action: ResultAction) {
    setSelection({ key: ownerKey, mode, action: { ...action,
      allowed_worker_item_ids: [...action.allowed_worker_item_ids], replacement_result_ids: [...action.replacement_result_ids] } });
    setError(null);
  }

  function close() { setSelection(null); setError(null); }

  async function submit(input: NativeResultOperationInput, action: ResultAction) {
    if (pending || !canOperateResult || legacyLocks.current.has(lease)) return;
    if (operations.authority.kind === "product") {
      const handled = await operations.submit(action, input);
      if (activeLease.current === lease && handled) close();
      return;
    }
    if (operations.authority.kind !== "legacy") {
      setError("当前本地会话未获授权，请确认会话后重试");
      operations.onSessionAuthorized();
      return;
    }
    legacyLocks.current.add(lease);
    setPending(true);
    setError(null);
    try {
      const fresh = await readBusinessCommandAuthority(workspaceId);
      if (activeLease.current !== lease) return;
      if (fresh.kind !== "legacy") {
        setError("当前本地会话未获授权，请确认会话后重试");
        operations.onSessionAuthorized();
        return;
      }
      await executeTaskResultAction(workspaceId, action, input);
      if (activeLease.current !== lease) return;
      await onRefresh();
      if (activeLease.current !== lease) return;
      close();
    } catch (cause) {
      if (activeLease.current === lease) setError(cause instanceof ApiProblem ? cause.detail : cause instanceof Error ? cause.message : "结果操作没有完成，请重新读取当前状态。");
    } finally {
      legacyLocks.current.delete(lease);
      if (activeLease.current === lease) setPending(false);
    }
  }

  function submitRework(input: { issueKeys: string[]; reason: string }) {
    if (reviewedAction === undefined || reviewedAction.candidate_id === null || reviewedAction.candidate_hash === null) return;
    void submit({
      command_type: "RequestTaskRework",
      payload: {
        candidate_id: reviewedAction.candidate_id,
        candidate_hash: reviewedAction.candidate_hash,
        mode: "user",
        issue_keys: input.issueKeys,
        target_worker_item_ids: [workerItemId],
        impact_worker_item_ids: [workerItemId],
        reason: input.reason,
        suggestion: null,
      },
    }, reviewedAction);
  }

  function submitBudget(input: { rounds: number; seconds: number; reason: string }) {
    if (reviewedAction === undefined) return;
    void submit({
      command_type: "AuthorizeAdditionalRework",
      payload: {
        additional_rounds: input.rounds,
        additional_seconds: input.seconds,
        reason: input.reason,
      },
    }, reviewedAction);
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
    if (reviewedAction === undefined || reviewedAction.candidate_id === null || reviewedAction.candidate_hash === null) return;
    void submit({
      command_type: "ReintegrateTaskResult",
      payload: {
        source_candidate_id: reviewedAction.candidate_id,
        source_candidate_hash: reviewedAction.candidate_hash,
        replacement_result_ids: reviewedAction.replacement_result_ids,
        expected_result_version: reviewedAction.expected_result_version,
        target_observation_id: null,
        reason: input.reason,
      },
    }, reviewedAction);
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
      <NativeOperationRecovery operations={operations} workspaceId={workspaceId} />
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
          {mode === "rework" && reworkAction && scopeAllowed && reviewedAction ? (
            <ReworkForm
              key={ownerKey}
              view={view}
              action={reviewedAction}
              workerItemId={workerItemId}
              pending={pending}
              error={error}
              onSubmit={submitRework}
              onCancel={close}
            />
          ) : mode === "budget" && budgetAction ? (
            <BudgetForm
              key={ownerKey}
              pending={pending}
              error={error}
              onSubmit={submitBudget}
              onCancel={close}
            />
          ) : mode === "retry" && retryActions[0] && reviewedAction ? (
            <RecoveryReasonForm
              key={ownerKey}
              title="重试检查"
              submitLabel="提交重试检查"
              pending={pending}
              error={error}
              onSubmit={(reason) => submitRetry(reviewedAction, reason)}
              onCancel={close}
            />
          ) : mode === "reintegrate" && reintegrateAction ? (
            <RecoveryReasonForm
              key={ownerKey}
              title="重新集成"
              submitLabel="提交重新集成"
              pending={pending}
              error={error}
              onSubmit={(reason) => submitReintegrate({ reason })}
              onCancel={close}
            />
          ) : (
            <div className="result-operation-actions__buttons">
              {reworkAction && scopeAllowed && canOperateResult ? (
                <button className="quiet-button" type="button" disabled={newOperationDisabled} onClick={() => open("rework", reworkAction)}>
                  <RefreshCcw size={14} aria-hidden="true" />局部返工
                </button>
              ) : null}
              {reworkAction && !scopeAllowed ? (
                <div className="result-operation-actions__revision" role="status">
                  <ShieldAlert size={14} aria-hidden="true" />
                  <span>当前 WorkerItem 不在可返工范围，需要主 Agent / Revision 调整</span>
                </div>
              ) : null}
              {budgetAction && canOperateResult ? (
                <button className="quiet-button" type="button" disabled={newOperationDisabled} onClick={() => open("budget", budgetAction)}>
                  <GitBranch size={14} aria-hidden="true" />增加返工额度
                </button>
              ) : null}
              {retryActions.length > 0 && canOperateResult ? (
                <button className="quiet-button" type="button" disabled={newOperationDisabled} onClick={() => open("retry", retryActions[0]!)}>
                  <RefreshCcw size={14} aria-hidden="true" />重试检查
                </button>
              ) : null}
              {reintegrateAction && canOperateResult ? (
                <button className="quiet-button" type="button" disabled={newOperationDisabled} onClick={() => open("reintegrate", reintegrateAction)}>
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
              {!canOperateResult && actions.some((action) => action.command_type !== "InspectResultOperation") ? (
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

function NativeOperationRecovery({ operations, workspaceId }: {
  operations: ReturnType<typeof useNativeResultOperations>;
  workspaceId: string;
}) {
  if (operations.authority.kind === "legacy") return null;
  const outcomes = {
    rework_requested: "局部返工已请求", attention_required: "返工需要人工处理", budget_authorized: "返工额度已授权",
    verification_scheduled: "检查重试已安排", verification_inspection_required: "原检查状态未知，需要核对",
    integration_scheduled: "重新集成已安排",
  };
  const status = operations.original === null ? null : operations.status === "rejected" ? "原始结果操作已被拒绝"
    : operations.status === "applied" ? operations.outcome === null ? "原始结果操作已处理，操作映射尚未确认" : outcomes[operations.outcome]
      : operations.busy ? "结果操作正在提交" : "结果操作尚未确认，请查询原始操作";
  if (operations.authorized && status === null && operations.error === null && operations.storageError === null) return null;
  return (
    <div className="result-operation-actions__state" aria-label="原始结果操作">
      {!operations.authorized ? <LocalSessionButton workspaceId={workspaceId} onAuthorized={operations.onSessionAuthorized} /> : null}
      {status !== null ? <span role="status">{status}</span> : null}
      {operations.original !== null ? (
        <button className="quiet-button" type="button" disabled={!operations.authorized || operations.busy} onClick={() => void operations.lookupOriginal()}>
          <SearchCode size={14} aria-hidden="true" />查询原始结果操作
        </button>
      ) : null}
      {operations.error !== null && operations.error !== status ? <span role="alert">{operations.error}</span> : null}
      {operations.storageError !== null ? <>
        <span role="alert">{operations.storageError}</span>
        <button className="quiet-button" type="button" disabled={operations.busy} onClick={operations.retryStorage}>
          <RefreshCcw size={14} aria-hidden="true" />重试本地结果操作记录
        </button>
      </> : null}
    </div>
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
  const valid = Number.isSafeInteger(Number(rounds)) && Number.isSafeInteger(Number(seconds))
    && Number(rounds) >= 0 && Number(seconds) >= 0 && reason.trim().length > 0 && (Number(rounds) > 0 || Number(seconds) > 0);
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
