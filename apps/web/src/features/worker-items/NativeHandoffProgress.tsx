import { ArrowRight, CircleAlert, CircleDot, ExternalLink, RefreshCw, X } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";

import type { HandoffTraceDescriptor, NativeHandoffTransition } from "./types";

type NativeHandoffProgressProps = {
  workspaceId: string;
  transition: NativeHandoffTransition;
  traceDescriptor?: HandoffTraceDescriptor | null;
  onRefresh: () => Promise<void>;
  onClose?: (() => void) | undefined;
};

export function NativeHandoffProgress({
  workspaceId,
  transition,
  traceDescriptor = null,
  onRefresh,
  onClose,
}: NativeHandoffProgressProps) {
  const [refreshing, setRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const sourceTrace = traceMatches(traceDescriptor, transition.source_execution_id, transition.source_session_id)
    ? traceDescriptor
    : null;
  const targetTrace = transition.target_execution_id !== null && transition.target_session_id !== null
    && traceMatches(traceDescriptor, transition.target_execution_id, transition.target_session_id)
    ? traceDescriptor
    : null;
  const targetRunNotAdmitted = transition.target_run_id === null;
  const sourceStopPending = transition.writer_state === "source_stop_requested";
  const sourceStopUnconfirmed = transition.phase === "stop_source"
    && sourceStopPending
    && transition.failure_code === "source_stop_unconfirmed";
  const waitingForSourceStop = sourceStopPending || sourceStopUnconfirmed;

  const refresh = async () => {
    if (refreshing) return;
    setRefreshing(true);
    setRefreshError(null);
    try {
      await onRefresh();
    } catch {
      setRefreshError("原生交接状态暂不可用，请重试读取");
    } finally {
      setRefreshing(false);
    }
  };

  return (
    <section
      className={`handoff-progress handoff-progress--${transition.status === "converged" ? "complete" : "progress"}`}
      role="region"
      aria-label="原生交接进度"
    >
      <div className="handoff-progress__heading">
        <div>
          <span className="eyebrow">NATIVE PROFILE HANDOFF</span>
          <h2>原生交接进度</h2>
        </div>
        <div className="handoff-progress__heading-actions">
          <span className="handoff-progress__status">{transition.status}</span>
          {onClose ? (
            <button className="icon-button" type="button" aria-label="关闭原生交接进度" title="关闭原生交接进度" onClick={onClose}>
              <X size={16} aria-hidden="true" />
            </button>
          ) : null}
        </div>
      </div>

      <div className="handoff-progress__participants" aria-label="原生交接参与者">
        <Participant
          label="来源 Profile"
          title={`${transition.source_profile_ref.profile_id} · R${transition.source_profile_ref.revision}`}
          details={[
            ["Execution", transition.source_execution_id],
            ["Run", transition.source_run_id],
            ["Session", transition.source_session_id],
            ["Binding", `V${transition.source_binding_version}`],
          ]}
        />
        <ArrowRight size={17} aria-hidden="true" />
        <Participant
          label="目标归属"
          title={targetOwnerLabel(transition)}
          details={[
            ["Profile", transition.target_profile_ref === null
              ? "当前用户"
              : `${transition.target_profile_ref.profile_id} · R${transition.target_profile_ref.revision}`],
            ["Worker", transition.target_worker_id ?? "尚未分配"],
            ["Execution", transition.target_execution_id ?? "尚未创建"],
            ["Session", transition.target_session_id ?? "尚未创建"],
            ["Run", transition.target_run_id ?? "尚未接入"],
            ["Binding", transition.target_binding_version === null ? "尚未建立" : `V${transition.target_binding_version}`],
          ]}
        />
      </div>

      <div className={`handoff-progress__callout${waitingForSourceStop ? " is-attention" : ""}`}>
        {waitingForSourceStop ? <CircleAlert size={16} aria-hidden="true" /> : <CircleDot size={16} aria-hidden="true" />}
        <div>
          <strong>{sourceStopUnconfirmed
            ? "等待源 Agent 停止证据"
            : sourceStopPending ? "等待源 Agent 停止" : `当前阶段：${transition.phase}`}</strong>
          <span>Transition V{transition.version} · {transition.writer_state} · 第 {transition.logical_attempt} 次逻辑尝试</span>
          {targetRunNotAdmitted ? <small>目标 Runtime 尚未接入；这不表示目标已启动或启动失败。</small> : null}
          {transition.status !== "converged" && transition.failure_code ? <small>冻结原因：{transition.failure_code}</small> : null}
        </div>
      </div>

      <dl className="handoff-progress__facts">
        <Fact label="Proposal" value={`${transition.proposal_id}`} />
        <Fact label="Transition" value={`${transition.id} · V${transition.version}`} />
        <Fact label="Phase" value={transition.phase} />
        <Fact label="Writer state" value={transition.writer_state} />
        <Fact label="Source input cutoff" value={String(transition.source_input_cutoff_sequence)} />
        <Fact label="Target Context" value={`V${transition.target_context_version}`} />
      </dl>

      <div className="handoff-progress__traces">
        <TraceLink workspaceId={workspaceId} label="查看源 Trace" descriptor={sourceTrace} />
        <TraceLink workspaceId={workspaceId} label="查看目标 Trace" descriptor={targetTrace} />
      </div>

      {refreshError ? <div className="handoff-progress__error" role="alert">{refreshError}</div> : null}
      <div className="handoff-progress__recovery-actions">
        <button className="quiet-button" type="button" disabled={refreshing} onClick={() => void refresh()}>
          <RefreshCw size={14} aria-hidden="true" />{refreshing ? "读取中" : "重新读取原生进度"}
        </button>
      </div>
    </section>
  );
}

function Participant({
  label,
  title,
  details,
}: {
  label: string;
  title: string;
  details: Array<[string, string]>;
}) {
  return (
    <div className="handoff-progress__participant">
      <span className="eyebrow">{label}</span>
      <strong>{title}</strong>
      {details.map(([name, value]) => <span className="mono-label" key={name}>{name} · {value}</span>)}
    </div>
  );
}

function Fact({ label, value }: { label: string; value: string }) {
  return <div><dt>{label}</dt><dd><code>{value}</code></dd></div>;
}

function TraceLink({
  workspaceId,
  label,
  descriptor,
}: {
  workspaceId: string;
  label: string;
  descriptor: HandoffTraceDescriptor | null;
}) {
  if (descriptor === null) return <span className="handoff-progress__trace-disabled">{label} · 暂无</span>;
  const href = `/workspaces/${encodeURIComponent(workspaceId)}/tasks/${encodeURIComponent(descriptor.root_task_id)}`
    + `/executions/${encodeURIComponent(descriptor.execution_id)}/sessions/${encodeURIComponent(descriptor.session_id)}/trace`;
  return <Link className="handoff-progress__trace-link" to={href}>{label}<ExternalLink size={13} aria-hidden="true" /></Link>;
}

function traceMatches(
  descriptor: HandoffTraceDescriptor | null,
  executionId: string,
  sessionId: string,
): descriptor is HandoffTraceDescriptor {
  return descriptor !== null
    && descriptor.execution_id === executionId
    && descriptor.session_id === sessionId;
}

function targetOwnerLabel(transition: NativeHandoffTransition): string {
  if (transition.target_assignee.kind === "human") return "当前用户";
  return transition.target_profile_ref === null
    ? `Profile ${transition.target_assignee.id}`
    : `${transition.target_profile_ref.profile_id} · R${transition.target_profile_ref.revision}`;
}
