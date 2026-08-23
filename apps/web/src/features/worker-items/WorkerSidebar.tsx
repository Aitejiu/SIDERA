import {
  Activity,
  CheckCircle2,
  CircleAlert,
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

import { ExecutionRoutingPanel } from "./ExecutionRoutingPanel";
import type { WorkerPlacement, WorkerWorkspace } from "./types";

export function WorkerSidebar({
  workspace,
  taskTitle,
  subtaskTitle,
  onRefresh,
}: {
  workspace: WorkerWorkspace;
  taskTitle: string;
  subtaskTitle: string;
  onRefresh: () => Promise<void>;
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

      <ExecutionRoutingPanel
        workspaceId={workspace.workspace_id}
        execution={currentExecution}
        workerItemId={workerItem.id}
        rootTaskId={workerItem.root_task_id}
        subtaskKey={workspace.current_placement.subtask_key}
        onRefresh={onRefresh}
      />

      <section className="worker-item-sidebar__section">
        <h3><ServerCog size={15} aria-hidden="true" />当前 Agent</h3>
        <strong>{runtime?.worker_display_name ?? "尚未接入 Agent"}</strong>
        <span>{runtime?.adapter_type ?? "等待 runtime"}</span>
        <span className="mono-label">WorkerItem {shortId(workerItem.id)}</span>
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
