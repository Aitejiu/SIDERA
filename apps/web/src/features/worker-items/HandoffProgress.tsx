import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import {
  ArrowRight,
  CheckCircle2,
  CircleAlert,
  CircleDot,
  ExternalLink,
  LoaderCircle,
  RefreshCcw,
  ShieldAlert,
} from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";

import { ApiProblem } from "@/shared/api/client";

import {
  getHandoffTransition,
  handoffTransitionQueryKey,
  recoverWorkerHandoff,
} from "./api";
import type { ActiveHandoff, HandoffTransition, RecoverWorkerHandoffPayload } from "./types";

const HANDOFF_PHASES = [
  "checkpoint_source",
  "stop_source",
  "freeze_source_workspace",
  "create_target_execution",
  "assemble_bundle",
  "transfer_workspace",
  "start_target_session",
  "acknowledge_context",
  "transfer_safe_inputs",
  "activate_target",
  "complete",
] as const;

type HandoffPhase = typeof HANDOFF_PHASES[number];
type RecoveryStrategy = RecoverWorkerHandoffPayload["strategy"];

export function HandoffProgress({
  workspaceId,
  transitionId,
  fallback,
  onRefresh,
  onClose,
}: {
  workspaceId: string;
  transitionId: string;
  fallback?: ActiveHandoff | null | undefined;
  onRefresh: () => Promise<void>;
  onClose?: (() => void) | undefined;
}) {
  const transitionQuery = useQuery({
    queryKey: handoffTransitionQueryKey(workspaceId, transitionId),
    queryFn: ({ signal }) => getHandoffTransition(workspaceId, transitionId, signal),
    enabled: transitionId.length > 0,
  });
  const transition = transitionQuery.data;

  if (transitionQuery.isPending && transition === undefined) {
    return (
      <div className="handoff-progress handoff-progress--loading">
        <div className="handoff-progress__loading" role="status" aria-label="正在读取 Agent 切换进度">
          <LoaderCircle size={16} aria-hidden="true" className="spin" />正在读取 Agent 切换进度
        </div>
      </div>
    );
  }

  if (transitionQuery.isError && transition === undefined && fallback === undefined) {
    return (
      <div className="handoff-progress handoff-progress--error" role="alert">
        <CircleAlert size={16} aria-hidden="true" />
        <div>
          <strong>无法读取 Agent 切换进度</strong>
          <span>{describeError(transitionQuery.error)}</span>
        </div>
        <button className="quiet-button" type="button" onClick={() => void transitionQuery.refetch()}>
          <RefreshCcw size={14} aria-hidden="true" />重试
        </button>
      </div>
    );
  }

  if (transition === undefined) {
    return <FallbackProgress fallback={fallback} onClose={onClose} />;
  }

  return (
    <HandoffProgressView
      workspaceId={workspaceId}
      transition={transition}
      transitionQuery={transitionQuery}
      onRefresh={onRefresh}
      onClose={onClose}
    />
  );
}

function HandoffProgressView({
  workspaceId,
  transition,
  transitionQuery,
  onRefresh,
  onClose,
}: {
  workspaceId: string;
  transition: HandoffTransition;
  transitionQuery: UseQueryResult<HandoffTransition>;
  onRefresh: () => Promise<void>;
  onClose?: (() => void) | undefined;
}) {
  const [recoveryOpen, setRecoveryOpen] = useState(true);
  const [strategy, setStrategy] = useState<RecoveryStrategy | "">("");
  const [reason, setReason] = useState("");
  const [evidenceRefs, setEvidenceRefs] = useState(() => transition.recovery_evidence_refs.join(", "));
  const [recoveryStatus, setRecoveryStatus] = useState<"idle" | "submitting" | "submitted">("idle");
  const [recoveryError, setRecoveryError] = useState<string | null>(null);
  const allowedStrategies = transition.available_recovery_strategies.filter(isRecoveryStrategy);
  const selectedStrategy = strategy || allowedStrategies[0] || "";
  const phaseIndex = phasePosition(transition.phase);
  const blocked = transition.status === "blocked"
    || transition.status === "blocked_unknown"
    || transition.status === "failed";
  const isUnknownTargetStart = transition.status === "blocked_unknown"
    || transition.failure_code === "target_start_unknown";
  const evidence = evidenceRefs.split(",").map((value) => value.trim()).filter(Boolean);
  const recoveryAcknowledged = recoveryStatus === "submitted";
  const canSubmitRecovery = recoveryOpen
    && selectedStrategy !== ""
    && reason.trim().length > 0
    && evidence.length > 0
    && recoveryStatus === "idle";

  const submitRecovery = async () => {
    if (!canSubmitRecovery) return;
    setRecoveryStatus("submitting");
    setRecoveryError(null);
    try {
      await recoverWorkerHandoff(
        workspaceId,
        transition,
        selectedStrategy,
        reason.trim(),
        evidence,
      );
      setRecoveryStatus("submitted");
      window.setTimeout(() => {
        void Promise.all([transitionQuery.refetch(), onRefresh()]).catch((error: unknown) => {
          setRecoveryError(`恢复请求已记录，但新的切换状态读取失败：${describeError(error)}`);
        });
      }, 0);
    } catch (error) {
      setRecoveryStatus("idle");
      setRecoveryError(describeError(error));
    }
  };

  return (
    <section className={`handoff-progress handoff-progress--${statusTone(transition.status)}`} role="region" aria-label="Agent 切换进度">
      <div className="handoff-progress__heading">
        <div>
          <span className="eyebrow">WORKER HANDOFF</span>
          <h2>Agent 切换进度</h2>
        </div>
        <div className="handoff-progress__heading-actions">
          <span className="handoff-progress__status">{handoffStatusLabel(transition.status)}</span>
          {onClose ? (
            <button className="icon-button" type="button" aria-label="关闭 Agent 切换进度" title="关闭 Agent 切换进度" onClick={onClose}>
              ×
            </button>
          ) : null}
        </div>
      </div>

      <div className="handoff-progress__participants" aria-label="Agent 切换参与者">
        <ParticipantSummary label="源 Agent" participant={transition.source} />
        <ArrowRight size={17} aria-hidden="true" />
        <ParticipantSummary label="目标 Agent" participant={transition.target} />
      </div>

      <div className={`handoff-progress__callout${isUnknownTargetStart ? " is-attention" : ""}`}>
        {isUnknownTargetStart ? <ShieldAlert size={16} aria-hidden="true" /> : <CircleDot size={16} aria-hidden="true" />}
        <div>
          <strong>{isUnknownTargetStart ? "目标 Agent 启动结果未知" : phaseLabel(transition.phase)}</strong>
          <span>{writerStateLabel(transition.writer_state)} · 第 {transition.logical_attempt} 次逻辑尝试</span>
          {transition.failure_code ? <small>冻结原因：{transition.failure_code}</small> : null}
        </div>
      </div>

      <div className="handoff-progress__milestones" aria-label="Agent 切换阶段">
        {HANDOFF_PHASES.map((phase, index) => {
          const state = index < phaseIndex ? "completed" : index === phaseIndex ? "current" : "pending";
          return (
            <div className={`handoff-progress__milestone is-${state}`} key={phase}>
              <span className="handoff-progress__milestone-marker" aria-hidden="true">
                {state === "completed" ? <CheckCircle2 size={14} /> : state === "current" ? <CircleDot size={14} /> : <span />}
              </span>
              <span>{phaseLabel(phase)}</span>
            </div>
          );
        })}
      </div>

      <dl className="handoff-progress__facts">
        <div><dt>Workspace</dt><dd>{workspaceTransferLabel(transition.workspace.transfer_mode)} · manifest V{transition.workspace.target_manifest_version ?? "--"}</dd></div>
        <div><dt>Context</dt><dd>source V{transition.context.source_context_version} → target V{transition.target_context_version}</dd></div>
        <div><dt>Proposal hash</dt><dd className="mono-value">{transition.proposal_hash}</dd></div>
        <div><dt>Writer</dt><dd>{writerStateLabel(transition.writer_state)}</dd></div>
      </dl>

      <div className="handoff-progress__traces">
        <TraceLink workspaceId={workspaceId} label="查看源 Agent Trace" descriptor={transition.source_trace_descriptor ?? transition.source.execution?.trace_descriptor} />
        <TraceLink workspaceId={workspaceId} label="查看目标 Agent Trace" descriptor={transition.target_trace_descriptor ?? transition.target.execution?.trace_descriptor} />
      </div>

      {blocked && (allowedStrategies.length > 0 || recoveryAcknowledged) ? (
        <section className="handoff-progress__recovery" aria-labelledby="handoff-recovery-title">
          <div className="handoff-progress__recovery-heading">
            <div>
              <span className="eyebrow">BOUNDED RECOVERY</span>
              <h3 id="handoff-recovery-title">恢复这次切换</h3>
            </div>
            <span>{recoveryAcknowledged ? "请求已记录" : `${allowedStrategies.length} 个后端允许策略`}</span>
          </div>
          {recoveryAcknowledged ? (
            <div className="handoff-progress__success" role="status">恢复请求已记录，正在读取新的切换状态。</div>
          ) : !recoveryOpen ? (
            <button className="quiet-button" type="button" onClick={() => {
              setRecoveryOpen(true);
              setStrategy(allowedStrategies[0] ?? "");
              setRecoveryStatus("idle");
              setRecoveryError(null);
            }}>
              <RefreshCcw size={14} aria-hidden="true" />选择恢复策略
            </button>
          ) : (
            <>
              <fieldset className="handoff-progress__recovery-options">
                <legend>恢复策略</legend>
                {allowedStrategies.map((option) => (
                  <label key={option}>
                    <input
                      type="radio"
                      name={`handoff-recovery-${transition.id}`}
                      value={option}
                      checked={selectedStrategy === option}
                      onChange={() => setStrategy(option)}
                      disabled={recoveryStatus !== "idle"}
                    />
                    <span>{recoveryStrategyLabel(option)}</span>
                  </label>
                ))}
              </fieldset>
              <label className="handoff-progress__field">
                <span>恢复原因</span>
                <input
                  value={reason}
                  onChange={(event) => setReason(event.target.value)}
                  aria-label="恢复原因"
                  placeholder="说明已确认的事实"
                  disabled={recoveryStatus !== "idle"}
                />
              </label>
              <label className="handoff-progress__field">
                <span>证据引用</span>
                <input
                  value={evidenceRefs}
                  onChange={(event) => setEvidenceRefs(event.target.value)}
                  aria-label="证据引用"
                  placeholder="用逗号分隔，例如 event:123, log:456"
                  disabled={recoveryStatus !== "idle"}
                />
              </label>
              {transition.recovery_evidence_refs.length > 0 ? (
                <div className="handoff-progress__evidence" aria-label="当前检查证据">
                  <span>当前检查证据已由后端带入，可按需补充</span>
                  <code>{transition.recovery_evidence_refs.join(", ")}</code>
                </div>
              ) : null}
              {recoveryError ? <div className="handoff-progress__error" role="alert"><CircleAlert size={14} aria-hidden="true" />{recoveryError}</div> : null}
              <div className="handoff-progress__recovery-actions">
                <button className="quiet-button" type="button" onClick={() => setRecoveryOpen(false)} disabled={recoveryStatus === "submitting"}>收起</button>
                <button className="primary-button" type="button" onClick={() => void submitRecovery()} disabled={!canSubmitRecovery}>
                  {recoveryStatus === "submitting" ? "提交中" : "提交恢复策略"}
                </button>
              </div>
            </>
          )}
        </section>
      ) : null}
    </section>
  );
}

function FallbackProgress({
  fallback,
  onClose,
}: {
  fallback?: ActiveHandoff | null | undefined;
  onClose?: (() => void) | undefined;
}) {
  const phase = fallback?.phase ?? "";
  return (
    <section className="handoff-progress handoff-progress--loading" role="region" aria-label="Agent 切换进度">
      <div className="handoff-progress__heading">
        <div>
          <span className="eyebrow">WORKER HANDOFF</span>
          <h2>Agent 切换进度</h2>
        </div>
        {onClose ? <button className="icon-button" type="button" aria-label="关闭 Agent 切换进度" title="关闭 Agent 切换进度" onClick={onClose}>×</button> : null}
      </div>
      <div className="handoff-progress__loading" role="status">
        <LoaderCircle size={16} aria-hidden="true" className="spin" />
        <span>{phase ? phaseLabel(phase) : "正在读取 Agent 切换状态"}</span>
      </div>
      {fallback ? <span className="mono-label">Transition {fallback.transition_id}</span> : null}
    </section>
  );
}

function ParticipantSummary({
  label,
  participant,
}: {
  label: string;
  participant: HandoffTransition["source"];
}) {
  return (
    <div className="handoff-progress__participant">
      <span className="eyebrow">{label}</span>
      <strong>{participant.worker.display_name}</strong>
      <span>{participant.worker.adapter_type} · {participant.worker.runtime_profile_label ?? "未标注 profile"}</span>
      <small className="mono-label">{participant.worker.worker_id}</small>
    </div>
  );
}

function TraceLink({
  workspaceId,
  label,
  descriptor,
}: {
  workspaceId: string;
  label: string;
  descriptor: HandoffTransition["source_trace_descriptor"] | undefined;
}) {
  if (descriptor === null || descriptor === undefined) {
    return <span className="handoff-progress__trace-disabled">{label} · 暂无</span>;
  }
  const href = `/workspaces/${workspaceId}/tasks/${descriptor.root_task_id}/executions/${descriptor.execution_id}/sessions/${descriptor.session_id}/trace`;
  return (
    <Link className="handoff-progress__trace-link" to={href}>
      {label}<ExternalLink size={13} aria-hidden="true" />
    </Link>
  );
}

function phasePosition(phase: string): number {
  const index = HANDOFF_PHASES.indexOf(phase as HandoffPhase);
  return index === -1 ? 0 : index;
}

function phaseLabel(phase: string): string {
  const labels: Record<string, string> = {
    checkpoint_source: "记录源 Agent 检查点",
    stop_source: "停止源 Agent 写入",
    freeze_source_workspace: "冻结源 Workspace",
    create_target_execution: "创建目标 Execution",
    assemble_bundle: "组装上下文 Bundle",
    transfer_workspace: "转移 Workspace",
    start_target_session: "启动目标 Session",
    acknowledge_context: "确认目标 Context",
    transfer_safe_inputs: "转移安全输入",
    activate_target: "激活目标 Agent",
    complete: "完成 Agent 切换",
  };
  return labels[phase] ?? phase.replaceAll("_", " ");
}

function handoffStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    pending: "等待开始",
    running: "进行中",
    blocked: "已阻塞",
    blocked_unknown: "未知结果",
    failed: "切换失败",
    converged: "已完成",
  };
  return labels[status] ?? status;
}

function writerStateLabel(state: string): string {
  const labels: Record<string, string> = {
    source_active: "源 Agent 可写",
    source_stop_requested: "等待源 Agent 停止",
    writer_none: "当前无可写 Agent",
    target_starting: "目标 Agent 启动中",
    target_active: "目标 Agent 可写",
    unknown: "写入者状态未知",
  };
  return labels[state] ?? state;
}

function workspaceTransferLabel(mode: string): string {
  return mode === "reuse_exact" ? "复用同一 Workspace" : mode;
}

function recoveryStrategyLabel(strategy: RecoveryStrategy): string {
  const labels: Record<RecoveryStrategy, string> = {
    retry_current_phase: "重试当前阶段",
    retry_same_target: "继续使用当前目标 Agent",
    select_new_target: "选择新的目标 Agent",
    continue_with_source_agent: "继续使用源 Agent",
    confirm_source_stopped: "确认源 Agent 已停止",
    confirm_target_absent: "确认目标尚未启动",
    confirm_target_active: "确认目标已运行",
  };
  return labels[strategy];
}

function isRecoveryStrategy(value: string): value is RecoveryStrategy {
  return value in {
    retry_current_phase: true,
    retry_same_target: true,
    select_new_target: true,
    continue_with_source_agent: true,
    confirm_source_stopped: true,
    confirm_target_absent: true,
    confirm_target_active: true,
  };
}

function statusTone(status: string): "progress" | "attention" | "complete" {
  if (status === "converged") return "complete";
  if (status === "blocked" || status === "blocked_unknown" || status === "failed") return "attention";
  return "progress";
}

function describeError(error: unknown): string {
  if (error instanceof ApiProblem) return error.detail;
  return error instanceof Error ? error.message : "读取请求未能完成";
}
