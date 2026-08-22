import { ExternalLink, FileCheck2, History, PackageCheck, ShieldCheck } from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import { StatusMark } from "@/shared/ui/StatusMark";

import { statusKind } from "./TaskNode";
import type { TaskDetail } from "./types";

export function TaskInspector({
  detail,
  isLoading,
  isError,
  workspaceId,
  rootTaskId,
  onRetry,
}: {
  detail: TaskDetail | undefined;
  isLoading: boolean;
  isError: boolean;
  workspaceId: string;
  rootTaskId: string;
  onRetry: () => void;
}) {
  if (detail === undefined && isLoading) {
    return <aside className="task-inspector" aria-label="任务检查器"><div className="inspector-loading" role="status">正在读取节点细节</div></aside>;
  }
  if (detail === undefined && isError) {
    return (
      <aside className="task-inspector task-inspector--empty" aria-label="任务检查器">
        <span className="eyebrow">INSPECTOR</span>
        <strong>无法读取节点细节</strong>
        <button className="quiet-button" type="button" onClick={onRetry}>重试读取节点</button>
      </aside>
    );
  }
  if (detail === undefined) {
    return (
      <aside className="task-inspector task-inspector--empty" aria-label="任务检查器">
        <span className="eyebrow">INSPECTOR</span>
        <strong>选择一个任务节点</strong>
        <span>图和任务表共用同一个选择状态。</span>
      </aside>
    );
  }

  return (
    <aside className="task-inspector" aria-label="任务检查器">
      <div className="inspector-heading">
        <div>
          <span className="eyebrow">TASK INSPECTOR</span>
          <h2>{detail.node.title}</h2>
          <span className="mono-label">{detail.node.work_item_key}</span>
        </div>
        <StatusMark kind={statusKind(detail.node.state)} />
      </div>

      {isLoading ? <div className="inspector-loading" role="status">正在读取节点细节</div> : null}
      {isError ? (
        <div className="inspector-error" role="alert">
          <span>节点细节暂不可用。</span>
          <button className="quiet-button" type="button" onClick={onRetry}>重试</button>
        </div>
      ) : null}

      <InspectorSection icon={<ShieldCheck size={15} />} title="目标与验收">
        <p>{detail.node.goal_summary}</p>
        <div className="inspector-tags">
          {detail.acceptance_refs.map((ref) => <span key={ref} className="inspector-tag">{ref}</span>)}
        </div>
      </InspectorSection>

      <InspectorSection icon={<History size={15} />} title="执行历史">
        <div className="inspector-facts">
          <span>WorkerSlot <strong>{detail.node.worker_slot_key ?? "未配置"}</strong></span>
          <span>Worker <strong>{detail.node.worker_display_name ?? "未分配"}</strong></span>
          <span>当前动作 <strong>{detail.node.current_action ?? "暂无"}</strong></span>
        </div>
        {detail.executions.map((execution) => (
          <Link
            className="inspector-link"
            key={execution.execution_id}
            to={`/workspaces/${workspaceId}/tasks/${rootTaskId}/executions/${execution.execution_id}/sessions/${execution.session_id ?? "unknown"}`}
          >
            查看执行过程 · {execution.execution_id.slice(0, 8)} · {execution.status}
            <ExternalLink size={13} aria-hidden="true" />
          </Link>
        ))}
      </InspectorSection>

      <InspectorSection icon={<FileCheck2 size={15} />} title="验收证据">
        <div className="inspector-facts">
          <span>完成度 <strong>{detail.node.evidence_satisfied} / {detail.node.evidence_required}</strong></span>
        </div>
        {detail.evidence.map((evidence) => (
          <div className="inspector-evidence" key={evidence.evidence_id}>
            <strong>{evidence.acceptance_ref}</strong>
            <span>{evidence.conclusion}</span>
          </div>
        ))}
      </InspectorSection>

      <InspectorSection icon={<PackageCheck size={15} />} title="Checkpoint">
        {detail.checkpoints.length === 0 ? <span className="muted-copy">还没有 checkpoint</span> : null}
        {detail.checkpoints.map((checkpoint) => (
          <div className="inspector-checkpoint" key={checkpoint.sequence}>
            <span className="mono-label">#{checkpoint.sequence}</span>
            <strong>{checkpoint.title}</strong>
            <span>{formatTime(checkpoint.occurred_at)}</span>
          </div>
        ))}
      </InspectorSection>

      <InspectorSection icon={<PackageCheck size={15} />} title="Artifacts">
        {detail.artifacts.length === 0 ? <span className="muted-copy">还没有 Artifact</span> : null}
        {detail.artifacts.map((artifact) => (
          <Link className="inspector-link" key={artifact.artifact_id} to={`/workspaces/${workspaceId}/artifacts/${artifact.artifact_id}`}>
            {artifact.artifact_type} · {artifact.media_type}
            <ExternalLink size={13} aria-hidden="true" />
          </Link>
        ))}
      </InspectorSection>
    </aside>
  );
}

function InspectorSection({
  icon,
  title,
  children,
}: {
  icon: ReactNode;
  title: string;
  children: ReactNode;
}) {
  return (
    <section className="inspector-section">
      <h3>{icon}{title}</h3>
      {children}
    </section>
  );
}

function formatTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}
