import {
  ArrowUpRight,
  CheckCircle2,
  CircleAlert,
  FileCheck2,
  FileText,
  GitBranch,
  History,
  PackageOpen,
  RefreshCcw,
  ShieldAlert,
  Timer,
} from "lucide-react";

import type { WorkerResultInputsView, ResultInputAction } from "./api";
import { resultInputActions } from "./api";

export interface ResultInputPanelProps {
  view: WorkerResultInputsView;
  onOpenWorkerItem: (workerItemId: string) => void;
  onOpenArtifact: (artifactId: string) => void;
  onAction: (action: ResultInputAction) => void;
}

export function ResultInputPanel({
  view,
  onOpenWorkerItem,
  onOpenArtifact,
  onAction,
}: ResultInputPanelProps) {
  const actions = resultInputActions(view);
  const governanceActions = actions.filter((action) => isGovernanceAction(action));
  const recoveryActions = actions.filter((action) => action.command_type === "RetryContextAssembly");
  const unsupportedActions = actions.filter(
    (action) => !isGovernanceAction(action) && action.command_type !== "RetryContextAssembly",
  );
  const selectedCount = view.groups.reduce((count, group) => count + group.selected_sources.length, 0);

  return (
    <section className="result-input-panel worker-item-sidebar__section" aria-label="结果输入">
      <div className="result-input-panel__heading">
        <div>
          <span className="eyebrow">RESULT INPUTS</span>
          <h3><GitBranch size={15} aria-hidden="true" />结果输入</h3>
        </div>
        <div className="result-input-panel__badges">
          {view.is_historical ? <span className="result-input-panel__badge"><History size={12} aria-hidden="true" />历史执行</span> : null}
          <span className="mono-label">SCHEMA V{view.schema_version}</span>
        </div>
      </div>

      <div className="result-input-panel__summary">
        <div>
          <span>消费者</span>
          <strong>{view.worker_item_title}</strong>
          <small>{view.worker_item_key}</small>
        </div>
        <div>
          <span>当前放行</span>
          <strong>{releaseStateLabel(view.governance.release_state)}</strong>
          <small>{view.governance.waiting_summary ?? "没有等待说明"}</small>
        </div>
        <div>
          <span>固定来源</span>
          <strong>{selectedCount} 份</strong>
          <small>Selection 由控制面固定</small>
        </div>
      </div>

      {view.groups.length > 0 ? view.groups.map((group) => (
        <ResultInputGroup
          key={group.group_key}
          group={group}
          onOpenWorkerItem={onOpenWorkerItem}
          onOpenArtifact={onOpenArtifact}
        />
      )) : null}

      {view.hard_dependencies.length > 0 ? (
        <section className="result-input-panel__block" aria-labelledby="result-input-hard-dependencies-title">
          <div className="result-input-panel__block-heading">
            <div>
              <span className="eyebrow">INDEPENDENT GATES</span>
              <h4 id="result-input-hard-dependencies-title">独立硬依赖</h4>
            </div>
            <span className="mono-label">{view.hard_dependencies.filter((item) => item.ready).length} / {view.hard_dependencies.length} READY</span>
          </div>
          <div className="result-input-hard-dependencies">
            {view.hard_dependencies.map((dependency) => (
              <div className={`result-input-hard-dependency${dependency.ready ? " is-ready" : ""}`} key={dependency.dependency_key}>
                <div className="result-input-hard-dependency__heading">
                  {dependency.ready ? <CheckCircle2 size={14} aria-hidden="true" /> : <CircleAlert size={14} aria-hidden="true" />}
                  <strong>{dependency.upstream_worker_item_title}</strong>
                  <span>{dependency.ready ? "已满足" : hardDependencyStateLabel(dependency.reason_code)}</span>
                </div>
                <small>{dependency.upstream_worker_item_key} · {dependency.dependency_key}</small>
                {dependency.required_artifact_refs.length > 0 ? <span>Artifact：{dependency.required_artifact_refs.join("、")}</span> : null}
                {dependency.required_evidence_refs.length > 0 ? <span>Evidence：{dependency.required_evidence_refs.join("、")}</span> : null}
                {dependency.upstream_worker_item_id ? (
                  <button
                    className="result-input-link"
                    type="button"
                    onClick={() => onOpenWorkerItem(dependency.upstream_worker_item_id ?? "")}
                  >
                    <ArrowUpRight size={12} aria-hidden="true" />查看来源 WorkerItem
                  </button>
                ) : null}
              </div>
            ))}
          </div>
        </section>
      ) : null}

      {view.groups.length === 0 && view.hard_dependencies.length === 0 ? (
        <div className="result-input-panel__empty" role="status">
          <FileCheck2 size={15} aria-hidden="true" />
          <span>没有声明结果输入组或独立硬依赖</span>
        </div>
      ) : null}

      <ResultInputDelivery view={view} />

      {governanceActions.length > 0 ? (
        <div className="result-input-panel__governance-note" role="note">
          <ShieldAlert size={14} aria-hidden="true" />
          <span>放行、批准与驳回仍由上方协同放行控制处理，沿用当前 Proposal 的固定 Selection。</span>
        </div>
      ) : null}
      {recoveryActions.length > 0 ? (
        <div className="result-input-panel__actions" aria-label="结果输入恢复动作">
          {recoveryActions.map((action) => (
            <button className="quiet-button" type="button" key={`${action.command_type}:${action.target_id}`} onClick={() => onAction(action)}>
              <RefreshCcw size={14} aria-hidden="true" />{actionLabel(action)}
            </button>
          ))}
        </div>
      ) : null}
      {unsupportedActions.map((action) => (
        <div className="result-input-panel__unsupported" role="status" key={`${action.command_type}:${action.target_id}`}>
          <CircleAlert size={14} aria-hidden="true" />
          <span>暂不支持此操作：{action.action}</span>
        </div>
      ))}
    </section>
  );
}

function ResultInputGroup({
  group,
  onOpenWorkerItem,
  onOpenArtifact,
}: {
  group: WorkerResultInputsView["groups"][number];
  onOpenWorkerItem: (workerItemId: string) => void;
  onOpenArtifact: (artifactId: string) => void;
}) {
  return (
    <section className="result-input-panel__block result-input-group" aria-labelledby={`result-input-group-${group.group_key}`}>
      <div className="result-input-panel__block-heading result-input-group__heading">
        <div>
          <span className="eyebrow">INPUT GROUP · {group.group_key}</span>
          <h4 id={`result-input-group-${group.group_key}`}>{group.title}</h4>
        </div>
        <span className={`result-input-group__threshold${group.ready_count >= group.required_count ? " is-ready" : ""}`}>
          {thresholdLabel(group)}
        </span>
      </div>

      <div className="result-input-group__rule">
        <span>{selectionStrategyLabel(group.mode, group.selection_hash)}</span>
        {group.reason_code ? <strong>{groupReasonLabel(group.reason_code)}</strong> : <span>按当前固定规则评估</span>}
      </div>

      <div className="result-input-group__candidates" aria-label={`${group.title} 候选来源`}>
        <span className="result-input-panel__label">候选来源</span>
        {group.candidates.map((candidate) => (
          <div className={`result-input-candidate${candidate.ready ? " is-ready" : " is-waiting"}`} key={candidate.worker_item_key}>
            <div className="result-input-candidate__main">
              {candidate.ready ? <CheckCircle2 size={14} aria-hidden="true" /> : <Timer size={14} aria-hidden="true" />}
              <div>
                <strong>{candidate.worker_item_title}</strong>
                <small>{candidate.subtask_path.join(" / ")} · priority {candidate.priority}</small>
              </div>
            </div>
            <div className="result-input-candidate__facts">
              <span>{candidateStateLabel(candidate)}</span>
              {candidate.worker_item_id ? (
                <button
                  className="result-input-link"
                  type="button"
                  onClick={() => onOpenWorkerItem(candidate.worker_item_id ?? "")}
                  aria-label={`查看来源 WorkerItem：${candidate.worker_item_title}`}
                >
                  <ArrowUpRight size={12} aria-hidden="true" />查看 WorkerItem
                </button>
              ) : null}
            </div>
            {candidate.missing_artifact_refs.length > 0 ? <span className="result-input-candidate__missing">缺少 Artifact：{candidate.missing_artifact_refs.join("、")}</span> : null}
            {candidate.missing_evidence_refs.length > 0 ? <span className="result-input-candidate__missing">缺少 Evidence：{candidate.missing_evidence_refs.join("、")}</span> : null}
            {candidate.reason_code && candidate.missing_artifact_refs.length === 0 && candidate.missing_evidence_refs.length === 0 ? <span className="result-input-candidate__missing">{candidateReasonLabel(candidate.reason_code)}</span> : null}
          </div>
        ))}
      </div>

      <div className="result-input-group__selected">
        <div className="result-input-panel__block-heading">
          <div>
            <span className="result-input-panel__label">本次采用</span>
            <strong>已采用 {group.selected_sources.length} 份结果</strong>
          </div>
          <span className="mono-label">{group.selection_id ? `SEL ${shortId(group.selection_id)}` : "未固定"}</span>
        </div>
        {group.selected_sources.length > 0 ? group.selected_sources.map((source) => (
          <article className="result-input-selected-source" key={`${source.worker_item_key}:${source.effective_request_id}`}>
            <div className="result-input-selected-source__heading">
              <FileText size={14} aria-hidden="true" />
              <div>
                <strong>{source.worker_item_title}</strong>
                <small>{source.subtask_path.join(" / ")} · 固定版本 · attempt {source.attempt}</small>
              </div>
              <span className="mono-label">P{source.priority}</span>
            </div>
            <div className="result-input-selected-source__meta">
              <span>request {shortId(source.effective_request_id)}</span>
              <span>basis {shortHash(source.source_basis_hash)}</span>
            </div>
            {source.artifacts.length > 0 ? (
              <div className="result-input-selected-source__artifacts">
                {source.artifacts.map((artifact) => (
                  <button
                    className="result-input-artifact"
                    type="button"
                    key={artifact.artifact_id}
                    onClick={() => onOpenArtifact(artifact.artifact_id)}
                    aria-label={`查看固定 Artifact：${artifact.artifact_type}`}
                  >
                    <PackageOpen size={12} aria-hidden="true" />
                    <span>查看固定 Artifact：{artifact.artifact_type}</span>
                    <small>{artifact.media_type} · {formatBytes(artifact.size_bytes)} · {shortHash(artifact.content_hash)}</small>
                  </button>
                ))}
              </div>
            ) : <span className="result-input-muted">没有可查看的固定 Artifact</span>}
            {source.evidence.length > 0 ? (
              <div className="result-input-selected-source__evidence">
                {source.evidence.map((evidence) => (
                  <span key={evidence.evidence_id}><FileCheck2 size={12} aria-hidden="true" />{evidence.acceptance_criterion_ref}</span>
                ))}
              </div>
            ) : null}
          </article>
        )) : <span className="result-input-muted">当前没有固定采用来源</span>}
      </div>
    </section>
  );
}

function ResultInputDelivery({ view }: { view: WorkerResultInputsView }) {
  const delivery = view.delivery;
  return (
    <section className="result-input-panel__block result-input-delivery" aria-label="内容交付">
      <div className="result-input-panel__block-heading">
        <div>
          <span className="eyebrow">CONTENT DELIVERY</span>
          <h4>内容交付</h4>
        </div>
        <strong className={`result-input-delivery__status result-input-delivery__status--${deliveryTone(delivery)}`}>
          {deliveryStatusLabel(delivery)}
        </strong>
      </div>
      <div className="result-input-delivery__facts">
        <div><span>放行</span><strong>{releaseStateLabel(view.governance.release_state)}</strong></div>
        <div><span>Package</span><strong>{delivery.package_status ?? "尚未创建"}</strong></div>
        <div><span>组装</span><strong>{assemblyLabel(delivery.assembly_status, delivery.assembly_attempt)}</strong></div>
        <div><span>Ack</span><strong>{ackLabel(delivery.acknowledgement_status)}</strong></div>
      </div>
      {delivery.reason_code ? <p className="result-input-delivery__reason"><CircleAlert size={13} aria-hidden="true" />{deliveryReasonLabel(delivery.reason_code)}</p> : null}
      {delivery.materialization_failure_code ? <p className="result-input-delivery__reason"><CircleAlert size={13} aria-hidden="true" />{delivery.materialization_failure_code}</p> : null}
      {delivery.context_version !== null ? <span className="result-input-muted">Context V{delivery.context_version} · package {shortHash(delivery.package_hash)}</span> : null}
      {view.execution ? <span className="result-input-muted">Execution request V{view.execution.request_version} · {executionLabel(view.execution.status)}</span> : null}
    </section>
  );
}

function isGovernanceAction(action: ResultInputAction): boolean {
  return [
    "ApproveCollaborationReleaseProposal",
    "RejectCollaborationReleaseProposal",
    "PauseCollaborationRelease",
    "ResumeAutomaticCollaboration",
  ].includes(action.command_type);
}

function actionLabel(action: ResultInputAction): string {
  if (action.command_type === "RetryContextAssembly") return "重试上下文组装";
  return action.action;
}

function thresholdLabel(group: WorkerResultInputsView["groups"][number]): string {
  return group.mode === "all"
    ? `全部 ${group.ready_count} / ${group.required_count}`
    : `至少 ${group.required_count} / ${group.candidates.length}`;
}

function selectionStrategyLabel(mode: string, selectionHash: string | null): string {
  if (mode === "all") return "全部候选必须满足";
  return selectionHash === null ? "按优先顺序选择" : "Selection 已固定，按优先顺序记录";
}

function candidateStateLabel(candidate: WorkerResultInputsView["groups"][number]["candidates"][number]): string {
  if (candidate.ready) return "可采用";
  if (candidate.outcome_unknown) return "结果未知";
  if (candidate.execution_status === "running" || candidate.execution_status === "queued") return "执行中";
  if (candidate.worker_item_id === null) return "尚未开始";
  if (candidate.retry_possible) return "等待重试";
  return candidateReasonLabel(candidate.reason_code);
}

function candidateReasonLabel(reason: string | null): string {
  return {
    execution_in_progress: "等待执行完成",
    execution_failed: "执行失败",
    result_input_unavailable: "结果内容暂不可用",
    result_input_integrity_failed: "结果完整性校验失败",
  }[reason ?? ""] ?? reason ?? "暂未满足输入条件";
}

function groupReasonLabel(reason: string): string {
  return {
    waiting_candidates: "等待候选结果",
    insufficient_ready_sources: "就绪来源不足",
    selection_stale: "固定 Selection 已过期",
  }[reason] ?? reason;
}

function hardDependencyStateLabel(reason: string | null): string {
  return reason === null ? "等待满足" : candidateReasonLabel(reason);
}

function releaseStateLabel(state: string): string {
  return {
    pending: "待确认",
    proposed: "待确认",
    released: "已放行",
    not_released: "未放行",
    unreleased: "未放行",
  }[state] ?? state;
}

function deliveryStatusLabel(delivery: WorkerResultInputsView["delivery"]): string {
  if (["result_input_unavailable", "result_input_integrity_failed", "result_input_format_unsupported", "result_input_budget_exceeded"].includes(delivery.reason_code ?? "")) return "内容暂不可用";
  if (delivery.materialization_failure_code) return "内容暂不可用";
  if (delivery.acknowledgement_status === "acknowledged") return "已接收";
  if (delivery.materialization_outcome === "succeeded") return "已生成";
  if (delivery.assembly_status === "blocked" || delivery.assembly_status === "retry_wait") return "组装受阻";
  if (delivery.package_id !== null) return "等待接收";
  return "等待交付";
}

function deliveryTone(delivery: WorkerResultInputsView["delivery"]): "attention" | "blocked" | "complete" | "neutral" {
  const label = deliveryStatusLabel(delivery);
  if (label === "已接收" || label === "已生成") return "complete";
  if (label === "内容暂不可用" || label === "组装受阻") return "blocked";
  if (label === "等待交付" || label === "等待接收") return "attention";
  return "neutral";
}

function deliveryReasonLabel(reason: string): string {
  return {
    awaiting_release: "等待协同放行",
    result_input_unavailable: "内容暂不可用",
    result_input_integrity_failed: "内容完整性校验失败",
    result_input_format_unsupported: "内容格式不支持",
    result_input_budget_exceeded: "内容超出交付预算",
  }[reason] ?? reason;
}

function assemblyLabel(status: string | null, attempt: number | null): string {
  if (status === null) return "尚未请求";
  return `${status}${attempt === null ? "" : ` · #${attempt}`}`;
}

function ackLabel(status: string): string {
  return {
    pending: "等待确认",
    acknowledged: "已确认",
    failed: "确认失败",
  }[status] ?? status;
}

function executionLabel(status: string | null): string {
  return {
    blocked: "执行阻塞",
    queued: "等待调度",
    running: "执行中",
    succeeded: "执行完成",
    failed: "执行失败",
  }[status ?? ""] ?? status ?? "尚未开始";
}

function shortId(value: string): string {
  return value.slice(0, 8);
}

function shortHash(value: string | null): string {
  return value === null ? "--" : `${value.slice(0, 8)}…`;
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  return `${(value / 1024).toFixed(1)} KB`;
}
