import { Handle, Position, type Node, type NodeProps } from "@xyflow/react";

import { StatusMark, type StatusMarkKind } from "@/shared/ui/StatusMark";

import type { SubtaskNode } from "./types";

export type TaskFlowNodeData = SubtaskNode;
export type TaskFlowNode = Node<TaskFlowNodeData, "task">;

export function TaskNode({ data }: NodeProps<TaskFlowNode>) {
  return (
    <>
      <Handle type="target" position={Position.Left} aria-hidden="true" />
      <button
        className="task-flow-node"
        type="button"
        aria-label={`打开子任务：${data.title}`}
      >
        <span className="task-flow-node__topline">
          <span className="eyebrow">{data.subtask_key}</span>
          <StatusMark kind={statusKind(data.state)} />
        </span>
        <strong>{data.title}</strong>
        <span className="task-flow-node__action">
          {data.current_action ?? (data.state === "planned" ? "等待物化" : "等待活动")}
        </span>
        <span className="task-flow-node__meta">
          {data.worker_item_count > 0 ? `${data.worker_item_count} 个 WorkerItem` : "未分配 WorkerItem"}
          <span>{data.evidence_satisfied}/{data.evidence_required} Evidence</span>
        </span>
      </button>
      <Handle type="source" position={Position.Right} aria-hidden="true" />
    </>
  );
}

export function statusKind(state: string): StatusMarkKind {
  switch (state) {
    case "running":
    case "assigned":
    case "queued":
      return "running";
    case "succeeded":
      return "completed";
    case "failed":
      return "failed";
    case "blocked":
    case "waiting_user":
    case "waiting_dependency":
      return "stale";
    case "planned":
      return "planned";
    default:
      return "connecting";
  }
}
