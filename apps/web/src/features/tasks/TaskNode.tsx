import { Handle, Position, type Node, type NodeProps } from "@xyflow/react";

import { StatusMark, type StatusMarkKind } from "@/shared/ui/StatusMark";

import type { TaskNode as TaskNodeProjection } from "./types";

export type TaskFlowNodeData = TaskNodeProjection & { selected: boolean };
export type TaskFlowNode = Node<TaskFlowNodeData, "task">;

export function TaskNode({ data }: NodeProps<TaskFlowNode>) {
  return (
    <>
      <Handle type="target" position={Position.Left} aria-hidden="true" />
      <button
        className={`task-flow-node${data.selected ? " is-selected" : ""}`}
        type="button"
        data-selected={data.selected ? "true" : "false"}
        aria-label={`选择 ${data.work_item_key}`}
      >
        <span className="task-flow-node__topline">
          <span className="eyebrow">{data.work_item_key}</span>
          <StatusMark kind={statusKind(data.state)} />
        </span>
        <strong>{data.title}</strong>
        <span className="task-flow-node__action">
          {data.current_action ?? (data.state === "planned" ? "等待物化" : "等待活动")}
        </span>
        <span className="task-flow-node__meta">
          {data.worker_display_name ?? (data.required_capabilities.join(" · ") || "未分配 Worker")}
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
