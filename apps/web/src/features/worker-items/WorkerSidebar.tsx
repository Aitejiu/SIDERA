import {
  Activity,
  ArrowRight,
  ArrowRightLeft,
  CheckCircle2,
  ChevronDown,
  CircleAlert,
  GitBranch,
  History,
  PackageCheck,
  Puzzle,
  RefreshCcw,
  Route,
  ServerCog,
} from "lucide-react";
import { Link } from "react-router-dom";
import { useState } from "react";

import { StatusMark, type StatusMarkKind } from "@/shared/ui/StatusMark";
import { AssigneeAppointment } from "../assignments/AssigneeAppointment";
import type { AssigneeValue } from "../assignments/types";

import { workerCollaborationLabel } from "../collaboration/types";
import { WorkerGovernancePanel } from "../collaboration/WorkerGovernancePanel";
import { WorkerContextSummary } from "../context/WorkerContextSummary";
import type { ResultInputAction, WorkerResultInputsView } from "../result-inputs/api";
import { ResultInputPanel } from "../result-inputs/ResultInputPanel";
import { ResultOperationActions } from "../results/ResultOperationActions";
import type { TaskResultOverview } from "../results/api";
import { ExecutionRoutingPanel } from "./ExecutionRoutingPanel";
import type { WorkerItemCollaborationSummary, WorkerPlacement, WorkerWorkspace } from "./types";

export function WorkerSidebar({
  workspace,
  legacyExecutionEligible,
  taskTitle,
  subtaskTitle,
  onRefresh,
  handoffAvailable,
  onOpenHandoff,
  onNativeHandoffRequest,
  onNativeHandoffHistoryRequest,
  resultInputs,
  resultInputsLoading,
  resultInputsError,
  onRetryResultInputs,
  onOpenWorkerItem,
  onOpenArtifact,
  onResultInputAction,
  resultInputActionState,
  resultView,
  resultViewLoading,
  resultViewError,
  onRetryResultView,
  onOpenResults,
}: {
  workspace: WorkerWorkspace;
  legacyExecutionEligible: boolean;
  taskTitle: string;
  subtaskTitle: string;
  onRefresh: () => Promise<void>;
  handoffAvailable: boolean;
  onOpenHandoff: () => void;
  onNativeHandoffRequest: (target: AssigneeValue) => void;
  onNativeHandoffHistoryRequest: () => void;
  resultInputs: WorkerResultInputsView | undefined;
  resultInputsLoading: boolean;
  resultInputsError: boolean;
  onRetryResultInputs: () => void;
  onOpenWorkerItem: (workerItemId: string) => void;
  onOpenArtifact: (artifactId: string) => void;
  onResultInputAction: (action: ResultInputAction) => void;
  resultInputActionState: {
    status: "idle" | "submitting" | "success" | "error";
    message: string | null;
  };
  resultView: TaskResultOverview | undefined;
  resultViewLoading: boolean;
  resultViewError: boolean;
  onRetryResultView: () => void;
  onOpenResults: (candidateId: string | undefined) => void;
}) {
  const { worker_item: workerItem, current_runtime: runtime } = workspace;
  const [detailsOpen, setDetailsOpen] = useState(false);
  const currentExecution = workspace.execution_history[0] ?? null;
  const contextSynchronized = workerItem.context_version === workerItem.acknowledged_context_version;
  const canOperateResult = !["retired", "paused"].includes(workerItem.status)
    && !["context_not_acknowledged", "projection_stale"].includes(workspace.read_only_reason ?? "");
  const showExecutionConfiguration = currentExecution !== null && (
    currentExecution.status !== "running"
    || currentExecution.requires_operator
    || Boolean(currentExecution.waiting_reason)
  );
  const needsAttention = resultInputsError || resultViewError
    || resultInputActionState.status === "error"
    || !contextSynchronized
    || Boolean(workspace.plan_sync?.failure_code)
    || Boolean(currentExecution?.requires_operator || currentExecution?.waiting_reason)
    || currentExecution?.status === "failed"
    || workspace.collaboration?.attention_state === "decision_required"
    || workspace.collaboration?.governance_mode === "paused"
    || Boolean(workspace.collaboration?.waiting_summary)
    || resultInputs?.governance.attention_state === "requires_user"
    || resultInputs?.governance.attention_state === "decision_required"
    || resultInputs?.governance.governance_mode === "paused"
    || resultInputs?.request?.status === "blocked"
    || resultInputs?.delivery.assembly_status === "blocked"
    || resultInputs?.delivery.assembly_status === "retry_wait"
    || Boolean(resultInputs?.delivery.materialization_failure_code);
  return (
    <aside className="worker-item-sidebar" aria-label="WorkerItem 事实">
      <AssigneeAppointment
        workspaceId={workspace.workspace_id}
        targetKind="worker_item"
        targetId={workerItem.id}
        workerWorkspace={workspace}
        onRefresh={onRefresh}
        onNativeHandoffRequest={onNativeHandoffRequest}
        onNativeHandoffHistoryRequest={onNativeHandoffHistoryRequest}
      />
      <section className="worker-item-sidebar__section worker-item-sidebar__agent">
        <h3><ServerCog size={15} aria-hidden="true" />当前 Agent</h3>
        <strong>{runtime?.worker_display_name ?? "尚未接入 Agent"}</strong>
        <span>{runtime ? `${runtime.adapter_type} · ${statusLabel(runtime.execution_status)}` : "等待 runtime"}</span>
        {workspace.active_handoff ? (
          <div className="worker-item-handoff-summary" role="status">
            <ArrowRightLeft size={14} aria-hidden="true" />
            <span><strong>Agent 切换中</strong><small>{handoffPhaseLabel(workspace.active_handoff.phase)}</small></span>
          </div>
        ) : handoffAvailable ? (
          <button className="quiet-button worker-item-handoff-button" type="button" onClick={onOpenHandoff}>
            <ArrowRightLeft size={14} aria-hidden="true" />调整执行 Agent
          </button>
        ) : null}
      </section>

      <details className="worker-item-sidebar__collapsible">
        <summary className="worker-item-sidebar__collapsible-summary">
          <span className="worker-item-sidebar__collapsible-title"><PackageCheck size={15} aria-hidden="true" />交付物</span>
          <span className="mono-label">{workspace.artifacts.length} 份 · {workspace.evidence.length} 条证据</span>
          <ChevronDown size={15} aria-hidden="true" />
        </summary>
        <div className="worker-item-sidebar__section">
          {workspace.artifacts.length === 0 ? <span className="muted-copy">还没有 Artifact</span> : null}
          {workspace.artifacts.map((artifact) => (
            <Link className="worker-item-sidebar__link" key={artifact.artifact_id}
              to={`/workspaces/${workspace.workspace_id}/artifacts/${artifact.artifact_id}`}>
              <span>{artifact.artifact_type}</span>
              <small>{artifact.content_available ? "可查看" : "不可用"}</small>
            </Link>
          ))}
          {workspace.evidence.map((evidence) => (
            <div className="worker-item-sidebar__evidence" key={evidence.evidence_id}>
              <strong>{evidence.acceptance_ref}</strong><span>{evidence.conclusion}</span>
            </div>
          ))}
        </div>
      </details>

      <details className="worker-item-sidebar__collapsible">
        <summary className="worker-item-sidebar__collapsible-summary">
          <span className="worker-item-sidebar__collapsible-title"><Puzzle size={15} aria-hidden="true" />用量</span>
          <span className="worker-item-sidebar__usage-total">{formatNumber(workspace.usage.input_tokens + workspace.usage.output_tokens)} tokens · {formatCost(workspace.usage.cost_microunits)}</span>
          <ChevronDown size={15} aria-hidden="true" />
        </summary>
        <dl className="worker-item-usage">
          <div><dt>输入 tokens</dt><dd>{formatNumber(workspace.usage.input_tokens)}</dd></div>
          <div><dt>输出 tokens</dt><dd>{formatNumber(workspace.usage.output_tokens)}</dd></div>
          <div><dt>缓存 tokens</dt><dd>{formatNumber(workspace.usage.cached_tokens)}</dd></div>
          <div><dt>费用</dt><dd>{formatCost(workspace.usage.cost_microunits)}</dd></div>
        </dl>
      </details>

      <details className="worker-item-sidebar__collapsible worker-item-sidebar__more" open={needsAttention || detailsOpen}
        onToggle={(event) => setDetailsOpen(event.currentTarget.open)}>
        <summary className="worker-item-sidebar__collapsible-summary">
          <span className="worker-item-sidebar__collapsible-title">更多任务信息</span>
          {needsAttention ? <CircleAlert size={14} aria-label="有待处理事项" /> : null}
          <ChevronDown size={15} aria-hidden="true" />
        </summary>

      <div className="worker-item-sidebar__section">
        <div className={`worker-item-context${contextSynchronized ? " is-synchronized" : " is-pending"}`}>
          {contextSynchronized ? <CheckCircle2 size={15} aria-hidden="true" /> : <CircleAlert size={15} aria-hidden="true" />}
          <strong>context V{workerItem.context_version} · {contextSynchronized ? "已同步" : "待同步"}</strong>
        </div>
        {!contextSynchronized ? <span className="muted-copy">Agent 尚未确认最新工作上下文</span> : null}
      </div>

      {workspace.collaboration ? <WorkerCollaborationSection collaboration={workspace.collaboration} /> : null}

      <details className="worker-item-sidebar__collapsible" open>
        <summary className="worker-item-sidebar__collapsible-summary">
          <span className="worker-item-sidebar__collapsible-title"><GitBranch size={15} aria-hidden="true" />结果输入</span>
          <span className="mono-label">{resultInputSummary(resultInputs, resultInputsLoading, resultInputsError)}</span>
          <ChevronDown size={15} aria-hidden="true" />
        </summary>
        <div className="worker-item-sidebar__collapsible-body">
          {resultInputsLoading ? (
            <section className="result-input-panel result-input-panel--state worker-item-sidebar__section" aria-label="结果输入" role="status">
              <span className="eyebrow">RESULT INPUTS</span>
              <strong>读取结果输入事实</strong>
            </section>
          ) : null}
          {resultInputsError ? (
            <section className="result-input-panel result-input-panel--state worker-item-sidebar__section" aria-label="结果输入" role="alert">
              <span className="eyebrow">RESULT INPUTS</span>
              <strong>结果输入暂不可用</strong>
              <span>中央 Worker 工作区仍可继续查看。</span>
              <button className="quiet-button" type="button" onClick={onRetryResultInputs}>
                <RefreshCcw size={14} aria-hidden="true" />重试读取结果输入
              </button>
            </section>
          ) : null}
          {resultInputs ? (
            <ResultInputPanel
              view={resultInputs}
              onOpenWorkerItem={onOpenWorkerItem}
              onOpenArtifact={onOpenArtifact}
              onAction={onResultInputAction}
            />
          ) : null}
          {resultInputActionState.message ? (
            <div className={`result-input-panel__action-state result-input-panel__action-state--${resultInputActionState.status}`} role={resultInputActionState.status === "error" ? "alert" : "status"}>
              {resultInputActionState.status === "error" ? <CircleAlert size={14} aria-hidden="true" /> : <CheckCircle2 size={14} aria-hidden="true" />}
              <span>{resultInputActionState.message}</span>
            </div>
          ) : null}
        </div>
      </details>

      <ResultOperationActions
        workspaceId={workspace.workspace_id}
        rootTaskId={workerItem.root_task_id}
        workerItemId={workerItem.id}
        view={resultView}
        isLoading={resultViewLoading}
        isError={resultViewError}
        canIntervene={workspace.can_intervene}
        canOperateResult={canOperateResult}
        onRetry={onRetryResultView}
        onRefresh={onRefresh}
        onOpenResults={onOpenResults}
      />

      <WorkerGovernancePanel
        workspaceId={workspace.workspace_id}
        workerItemId={workerItem.id}
        workerItemVersion={workerItem.version}
        targetPlanGeneration={workspace.current_placement.plan_generation}
        taskPath={`${taskTitle} / ${subtaskTitle} / ${workerItem.title}`}
        canIntervene={workspace.can_intervene}
        collaboration={workspace.collaboration}
        {...(resultInputs === undefined ? {} : { resultInputs })}
        onRefresh={onRefresh}
      />

      {legacyExecutionEligible ? (
        <details className="worker-item-sidebar__collapsible" open={showExecutionConfiguration}>
          <summary className="worker-item-sidebar__collapsible-summary">
            <span className="worker-item-sidebar__collapsible-title"><Route size={15} aria-hidden="true" />执行配置</span>
            <ChevronDown size={15} aria-hidden="true" />
          </summary>
          <ExecutionRoutingPanel
            workspaceId={workspace.workspace_id}
            execution={currentExecution}
            workerItemId={workerItem.id}
            rootTaskId={workerItem.root_task_id}
            subtaskKey={workspace.current_placement.subtask_key}
            onRefresh={onRefresh}
          />
        </details>
      ) : null}

      {needsAttention || detailsOpen ? <WorkerContextSummary
        workspaceId={workspace.workspace_id}
        executions={workspace.execution_history}
      /> : null}

      <details className="worker-item-sidebar__collapsible">
        <summary className="worker-item-sidebar__collapsible-summary">
          <span className="worker-item-sidebar__collapsible-title"><Activity size={15} aria-hidden="true" />会话详情</span>
          <ChevronDown size={15} aria-hidden="true" />
        </summary>
        <div className="worker-item-sidebar__section">
          {runtime ? (
            <dl className="worker-item-facts">
              <div><dt>Execution</dt><dd>{shortId(runtime.execution_id)} · {statusLabel(runtime.execution_status)}</dd></div>
              <div><dt>Session</dt><dd>{runtime.session_id ? `${shortId(runtime.session_id)} · ${statusLabel(runtime.session_status ?? "unknown")}` : "无 Session"}</dd></div>
              <div><dt>Session version</dt><dd>V{runtime.session_version ?? "--"}</dd></div>
            </dl>
          ) : (
            <span className="muted-copy">当前没有运行中的 Execution / Session</span>
          )}
          <span className="mono-label">{taskTitle} / {subtaskTitle}</span>
          <span className="mono-label">WorkerItem {shortId(workerItem.id)}</span>
        </div>
      </details>

      {workspace.plan_sync ? (
        <section className="worker-item-sidebar__section">
          <h3><GitBranch size={15} aria-hidden="true" />计划同步</h3>
          <dl className="worker-item-facts">
            <div><dt>Action</dt><dd>{planActionLabel(workspace.plan_sync.action)}</dd></div>
            <div><dt>Generation</dt><dd>GEN {workspace.plan_sync.source_generation} → GEN {workspace.plan_sync.target_generation}</dd></div>
            <div><dt>Context</dt><dd>V{workspace.plan_sync.current_context_version} → V{workspace.plan_sync.target_context_version}</dd></div>
          </dl>
          {workspace.plan_sync.failure_code ? <span className="muted-copy">冻结原因：{workspace.plan_sync.failure_code}</span> : null}
        </section>
      ) : null}

      <details className="worker-item-sidebar__collapsible">
        <summary className="worker-item-sidebar__collapsible-summary">
          <span className="worker-item-sidebar__collapsible-title"><History size={15} aria-hidden="true" />分配历史</span>
          <ChevronDown size={15} aria-hidden="true" />
        </summary>
        <div className="worker-item-placement-list worker-item-sidebar__collapsible-body">
          {workspace.placement_history.map((placement) => (
            <PlacementRow key={`${placement.plan_generation}:${placement.subtask_key}`} placement={placement} />
          ))}
        </div>
      </details>

      <div className="worker-item-sidebar__footer">
        <History size={13} aria-hidden="true" />
        <span>CURSOR {workspace.last_change_cursor} · {formatTime(workspace.as_of)}</span>
      </div>
      </details>

      {!workspace.can_intervene ? (
        <section className="worker-item-readonly" role="status">
          <CircleAlert size={15} aria-hidden="true" />
          <div>
            <strong>只读</strong>
            <span>{readOnlyReason(workspace.read_only_reason)}</span>
          </div>
        </section>
      ) : null}

    </aside>
  );
}

function WorkerCollaborationSection({
  collaboration,
}: {
  collaboration: WorkerItemCollaborationSummary;
}) {
  const label = workerCollaborationLabel(collaboration.state) ?? "按现有流程调度";
  return (
    <section className="worker-item-sidebar__section worker-item-collaboration" aria-labelledby="worker-item-collaboration-title">
      <div className="worker-item-collaboration__heading">
        <h3 id="worker-item-collaboration-title"><GitBranch size={15} aria-hidden="true" />协同状态</h3>
        <StatusMark kind={collaborationStatusKind(collaboration.state)} label={label} />
      </div>
      {collaboration.waiting_summary && collaboration.waiting_summary !== label ? (
        <p className="worker-item-collaboration__waiting">{collaboration.waiting_summary}</p>
      ) : collaboration.waiting_summary === null ? (
        <span className="muted-copy">当前没有等待中的协同输入</span>
      ) : null}
      {collaboration.upstream_links.length > 0 ? (
        <div className="worker-item-collaboration__upstream">
          <span className="eyebrow">上游依赖</span>
          {collaboration.upstream_links.map((link) => (
            <Link
              className="worker-item-collaboration__link"
              key={link.href}
              to={link.href}
              aria-label={`查看上游：${link.title}`}
            >
              <span>{link.title}</span>
              <ArrowRight size={13} aria-hidden="true" />
            </Link>
          ))}
        </div>
      ) : null}
    </section>
  );
}

function collaborationStatusKind(state: WorkerItemCollaborationSummary["state"]): StatusMarkKind {
  return state === "materialized" ? "running" : "planned";
}

function resultInputSummary(
  resultInputs: WorkerResultInputsView | undefined,
  isLoading: boolean,
  isError: boolean,
): string {
  if (isLoading) return "读取中";
  if (isError) return "读取失败";
  if (resultInputs === undefined) return "等待数据";
  if (resultInputs.is_historical) return "历史执行";
  const selectedCount = resultInputs.groups.reduce((count, group) => count + group.selected_sources.length, 0);
  return `${selectedCount} 份固定来源`;
}

function PlacementRow({ placement }: { placement: WorkerPlacement }) {
  return (
    <div className="worker-item-placement">
      <span className="mono-label">第 {placement.plan_generation} 代 · {placement.subtask_key}</span>
      <span>{placement.role} · {placement.capability_requirements.join(" / ") || "无特殊能力要求"}</span>
    </div>
  );
}

function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    active: "当前",
    running: "执行中",
    queued: "排队中",
    succeeded: "已完成",
    completed: "已完成",
    failed: "失败",
    closed: "已关闭",
    paused: "已暂停",
    retired: "已退役",
    unknown: "未知",
  };
  return labels[status] ?? status;
}

function readOnlyReason(reason: string | null): string {
  const labels: Record<string, string> = {
    worker_item_retired: "这个 WorkerItem 已退役，只能查看历史上下文",
    no_active_session: "当前没有可写入的 Session",
    context_not_synchronized: "上下文尚未同步，暂不能写入",
    stream_stale: "实时状态过期，恢复同步后才能写入",
  };
  return reason === null ? "当前工作区只读" : labels[reason] ?? "当前工作区只读，暂不能介入";
}

function planActionLabel(action: string): string {
  const labels: Record<string, string> = {
    preserve: "保留当前 Worker",
    patch: "应用 Context Patch",
    continue: "续接当前 Worker",
    move: "迁移到新 Worker",
    retire: "退役并保留历史",
  };
  return labels[action] ?? action;
}

function handoffPhaseLabel(phase: string): string {
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
  return labels[phase] ?? phase;
}

function shortId(value: string): string {
  return value.slice(0, 8);
}

function formatNumber(value: number): string {
  return new Intl.NumberFormat("en-US").format(value);
}

function formatCost(value: number): string {
  return `${new Intl.NumberFormat("en-US").format(value)} μu`;
}

function formatTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN");
}
