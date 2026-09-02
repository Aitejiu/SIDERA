import {
  Activity,
  ArrowRight,
  ArrowRightLeft,
  CheckCircle2,
  CircleAlert,
  GitBranch,
  Gauge,
  History,
  PackageCheck,
  Puzzle,
  Route,
  ServerCog,
  ShieldCheck,
} from "lucide-react";
import { Link } from "react-router-dom";

import { StatusMark, type StatusMarkKind } from "@/shared/ui/StatusMark";

import { workerCollaborationLabel } from "../collaboration/types";
import { WorkerContextSummary } from "../context/WorkerContextSummary";
import { ExecutionRoutingPanel } from "./ExecutionRoutingPanel";
import type { WorkerItemCollaborationSummary, WorkerPlacement, WorkerWorkspace } from "./types";

export function WorkerSidebar({
  workspace,
  taskTitle,
  subtaskTitle,
  onRefresh,
  handoffAvailable,
  onOpenHandoff,
}: {
  workspace: WorkerWorkspace;
  taskTitle: string;
  subtaskTitle: string;
  onRefresh: () => Promise<void>;
  handoffAvailable: boolean;
  onOpenHandoff: () => void;
}) {
  const { worker_item: workerItem, current_runtime: runtime } = workspace;
  const currentExecution = workspace.execution_history[0] ?? null;
  const contextSynchronized = workerItem.context_version === workerItem.acknowledged_context_version;
  return (
    <aside className="worker-item-sidebar" aria-label="WorkerItem 事实">
      <div className="worker-item-sidebar__heading">
        <div>
          <span className="eyebrow">WORKERITEM FACTS</span>
          <h2>任务信息</h2>
        </div>
        <StatusMark kind={workerStatusKind(workerItem.status)} />
      </div>

      {workspace.collaboration ? <WorkerCollaborationSection collaboration={workspace.collaboration} /> : null}

      <ExecutionRoutingPanel
        workspaceId={workspace.workspace_id}
        execution={currentExecution}
        workerItemId={workerItem.id}
        rootTaskId={workerItem.root_task_id}
        subtaskKey={workspace.current_placement.subtask_key}
        onRefresh={onRefresh}
      />

      <WorkerContextSummary
        workspaceId={workspace.workspace_id}
        executions={workspace.execution_history}
      />

      <section className="worker-item-sidebar__section">
        <h3><ServerCog size={15} aria-hidden="true" />当前 Agent</h3>
        <strong>{runtime?.worker_display_name ?? "尚未接入 Agent"}</strong>
        <span>{runtime?.adapter_type ?? "等待 runtime"}</span>
        <span className="mono-label">WorkerItem {shortId(workerItem.id)}</span>
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

      <section className="worker-item-sidebar__section">
        <h3><Activity size={15} aria-hidden="true" />Execution / Session</h3>
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
      </section>

      <section className="worker-item-sidebar__section">
        <h3><Gauge size={15} aria-hidden="true" />当前推进</h3>
        <dl className="worker-item-facts">
          <div><dt>当前步骤</dt><dd>{workspace.current_step ?? "尚未开始"}</dd></div>
          <div><dt>下一动作</dt><dd>{workspace.next_action ?? "等待计划"}</dd></div>
          <div><dt>Checkpoint</dt><dd>{workspace.latest_checkpoint ? `#${workspace.latest_checkpoint.sequence} · ${workspace.latest_checkpoint.title}` : "暂无"}</dd></div>
        </dl>
      </section>

      <section className="worker-item-sidebar__section">
        <h3><ShieldCheck size={15} aria-hidden="true" />Context</h3>
        <div className={`worker-item-context${contextSynchronized ? " is-synchronized" : " is-pending"}`}>
          {contextSynchronized ? <CheckCircle2 size={15} aria-hidden="true" /> : <CircleAlert size={15} aria-hidden="true" />}
          <strong>context V{workerItem.context_version} · {contextSynchronized ? "已同步" : "待同步"}</strong>
        </div>
        {!contextSynchronized ? <span className="muted-copy">Agent 尚未确认最新工作上下文</span> : null}
      </section>

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

      <section className="worker-item-sidebar__section">
        <h3><Puzzle size={15} aria-hidden="true" />Usage</h3>
        <dl className="worker-item-usage">
          <div><dt>Input tokens</dt><dd>{formatNumber(workspace.usage.input_tokens)}</dd></div>
          <div><dt>Output tokens</dt><dd>{formatNumber(workspace.usage.output_tokens)}</dd></div>
          <div><dt>Cached tokens</dt><dd>{formatNumber(workspace.usage.cached_tokens)}</dd></div>
          <div><dt>Cost</dt><dd>{formatCost(workspace.usage.cost_microunits)}</dd></div>
        </dl>
      </section>

      <section className="worker-item-sidebar__section">
        <h3><PackageCheck size={15} aria-hidden="true" />Artifacts / Evidence</h3>
        {workspace.artifacts.length === 0 ? <span className="muted-copy">还没有 Artifact</span> : null}
        {workspace.artifacts.map((artifact) => (
          <Link
            className="worker-item-sidebar__link"
            key={artifact.artifact_id}
            to={`/workspaces/${workspace.workspace_id}/artifacts/${artifact.artifact_id}`}
          >
            <span>{artifact.artifact_type}</span>
            <small>{artifact.content_available ? "可查看" : "不可用"}</small>
          </Link>
        ))}
        {workspace.evidence.map((evidence) => (
          <div className="worker-item-sidebar__evidence" key={evidence.evidence_id}>
            <strong>{evidence.acceptance_ref}</strong>
            <span>{evidence.conclusion}</span>
          </div>
        ))}
      </section>

      <section className="worker-item-sidebar__section">
        <h3><Route size={15} aria-hidden="true" />Placement history</h3>
        <div className="worker-item-placement-list">
          {workspace.placement_history.map((placement) => (
            <PlacementRow key={`${placement.plan_generation}:${placement.subtask_key}`} placement={placement} />
          ))}
        </div>
      </section>

      {!workspace.can_intervene ? (
        <section className="worker-item-readonly" role="status">
          <CircleAlert size={15} aria-hidden="true" />
          <div>
            <strong>只读</strong>
            <span>{readOnlyReason(workspace.read_only_reason)}</span>
          </div>
        </section>
      ) : null}

      <div className="worker-item-sidebar__footer">
        <History size={13} aria-hidden="true" />
        <span>CURSOR {workspace.last_change_cursor} · {formatTime(workspace.as_of)}</span>
      </div>
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

function PlacementRow({ placement }: { placement: WorkerPlacement }) {
  return (
    <div className="worker-item-placement">
      <span className="mono-label">第 {placement.plan_generation} 代 · {placement.subtask_key}</span>
      <span>{placement.role} · {placement.capability_requirements.join(" / ") || "无特殊能力要求"}</span>
    </div>
  );
}

function workerStatusKind(status: string): StatusMarkKind {
  if (status === "running" || status === "assigned" || status === "queued" || status === "retrying") return "running";
  if (status === "succeeded" || status === "completed") return "completed";
  if (status === "failed") return "failed";
  if (status === "blocked" || status === "waiting_user" || status === "waiting_dependency" || status === "retired") return "stale";
  return "planned";
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
