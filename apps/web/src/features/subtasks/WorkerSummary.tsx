import { CheckCircle2, FileCheck2, PackageCheck, ShieldAlert } from "lucide-react";
import { Link } from "react-router-dom";

import { StatusMark, type StatusMarkKind } from "@/shared/ui/StatusMark";

import type { SubtaskWorkspace } from "./types";

export function WorkerSummary({ workspace }: { workspace: SubtaskWorkspace }) {
  const { subtask } = workspace;
  return (
    <aside className="subtask-summary" aria-label="子任务摘要">
      <div className="subtask-summary__heading">
        <div>
          <h2>子任务摘要</h2>
        </div>
        <StatusMark kind={subtaskStatusKind(subtask.state)} />
      </div>

      <section className="subtask-summary__section">
        <h3><ShieldAlert size={15} aria-hidden="true" />目标与验收</h3>
        <p>{subtask.goal_summary}</p>
        <div className="subtask-summary__progress">
          <strong>{subtask.evidence_satisfied} / {subtask.evidence_required}</strong>
          <span>验收证据</span>
        </div>
        <div className="subtask-summary__tags">
          {subtask.acceptance_refs.map((reference) => (
            <span key={reference}>{reference}</span>
          ))}
        </div>
      </section>

      <section className="subtask-summary__section">
        <h3><PackageCheck size={15} aria-hidden="true" />Artifacts</h3>
        {workspace.artifacts.length === 0 ? <span className="muted-copy">还没有 Artifact</span> : null}
        {workspace.artifacts.map((artifact) => (
          <Link
            className="subtask-summary__link"
            key={artifact.artifact_id}
            to={`/workspaces/${workspace.workspace_id}/artifacts/${artifact.artifact_id}`}
          >
            <span>
              <strong>{artifact.artifact_type}</strong>
              <small>{artifact.media_type} · {formatBytes(artifact.size_bytes)}</small>
            </span>
            <span>{artifact.content_available ? "可查看" : "不可用"}</span>
          </Link>
        ))}
      </section>

      <section className="subtask-summary__section">
        <h3><FileCheck2 size={15} aria-hidden="true" />Evidence</h3>
        {workspace.evidence.length === 0 ? <span className="muted-copy">还没有 Evidence</span> : null}
        {workspace.evidence.map((evidence) => (
          <div className="subtask-summary__evidence" key={evidence.evidence_id}>
            <strong>{evidence.acceptance_ref}</strong>
            <span>{evidence.conclusion}</span>
          </div>
        ))}
      </section>

      <section className="subtask-summary__section">
        <h3><CheckCircle2 size={15} aria-hidden="true" />Checkpoint</h3>
        {workspace.checkpoints.length === 0 ? <span className="muted-copy">还没有 checkpoint</span> : null}
        {workspace.checkpoints.map((checkpoint) => (
          <div className="subtask-summary__checkpoint" key={`${checkpoint.execution_id}:${checkpoint.sequence}`}>
            <span className="mono-label">#{checkpoint.sequence}</span>
            <strong>{checkpoint.title}</strong>
            <small>{formatTime(checkpoint.occurred_at)}</small>
          </div>
        ))}
      </section>
    </aside>
  );
}

function subtaskStatusKind(status: string): StatusMarkKind {
  if (status === "running" || status === "assigned" || status === "queued" || status === "retrying") {
    return "running";
  }
  if (status === "succeeded" || status === "completed") {
    return "completed";
  }
  if (status === "failed") {
    return "failed";
  }
  if (status === "blocked" || status === "waiting_user" || status === "waiting_dependency") {
    return "stale";
  }
  return "planned";
}

function formatBytes(value: number): string {
  if (value < 1024) {
    return `${value} B`;
  }
  return `${Math.round(value / 1024)} KB`;
}

function formatTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}
