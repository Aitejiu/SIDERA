import { ArrowRight, CheckCircle2, CircleAlert, CircleDashed, LoaderCircle, MoveRight } from "lucide-react";

import { StatusMark } from "@/shared/ui/StatusMark";

import { statusKind } from "./TaskNode";
import type { TaskNode } from "./types";

type BoardColumnKey = "attention" | "running" | "waiting" | "next" | "completed";

type BoardColumn = {
  key: BoardColumnKey;
  title: string;
  eyebrow: string;
  empty: string;
};

const columns: BoardColumn[] = [
  { key: "attention", title: "需要处理", eyebrow: "ATTENTION", empty: "没有卡点" },
  { key: "running", title: "执行中", eyebrow: "RUNNING", empty: "没有正在执行的节点" },
  { key: "waiting", title: "等待依赖", eyebrow: "WAITING", empty: "没有等待中的节点" },
  { key: "next", title: "下一步", eyebrow: "NEXT", empty: "没有待启动节点" },
  { key: "completed", title: "最近完成", eyebrow: "COMPLETED", empty: "还没有完成节点" },
];

export function TaskBoard({
  nodes,
  selectedWorkItemKey,
  onSelect,
}: {
  nodes: readonly TaskNode[];
  selectedWorkItemKey: string | null;
  onSelect: (workItemKey: string) => void;
}) {
  const grouped = new Map<BoardColumnKey, TaskNode[]>();
  for (const column of columns) grouped.set(column.key, []);
  for (const node of nodes) grouped.get(columnFor(node))?.push(node);

  return (
    <section className="task-board" aria-labelledby="task-board-title">
      <div className="section-heading">
        <div>
          <span className="eyebrow">TASK FIRST / STATUS BOARD</span>
          <h2 id="task-board-title">任务状态看板</h2>
        </div>
        <span className="mono-label">{nodes.length} 个子任务</span>
      </div>
      <div className="task-board__columns">
        {columns.map((column) => {
          const items = grouped.get(column.key) ?? [];
          return (
            <section className={`task-board__column task-board__column--${column.key}`} key={column.key} aria-labelledby={`task-board-${column.key}`}>
              <header className="task-board__column-heading">
                <div><span className="eyebrow">{column.eyebrow}</span><h3 id={`task-board-${column.key}`}>{column.title}</h3></div>
                <span className="task-board__count">{items.length}</span>
              </header>
              <div className="task-board__list">
                {items.length === 0 ? <div className="task-board__empty"><MoveRight size={14} aria-hidden="true" />{column.empty}</div> : null}
                {items.map((node) => <TaskBoardCard key={node.work_item_key} node={node} selected={node.work_item_key === selectedWorkItemKey} onSelect={onSelect} />)}
              </div>
            </section>
          );
        })}
      </div>
    </section>
  );
}

function TaskBoardCard({ node, selected, onSelect }: { node: TaskNode; selected: boolean; onSelect: (workItemKey: string) => void }) {
  const action = node.current_action ?? (node.state === "planned" ? "等待物化" : node.state === "succeeded" ? "验收已完成" : "等待活动");
  const isExecution = node.execution_id !== null;
  return (
    <button className={`task-board-card${selected ? " is-selected" : ""}`} type="button" data-selected={selected ? "true" : "false"} onClick={() => onSelect(node.work_item_key)} aria-label={`查看${node.title}`}>
      <span className="task-board-card__topline"><span className="eyebrow">{node.work_item_key}</span><StatusMark kind={statusKind(node.state)} /></span>
      <strong>{node.title}</strong>
      <span className="task-board-card__action">{action}</span>
      <span className="task-board-card__meta">
        <span>{isExecution ? "有执行记录" : node.required_capabilities.join(" · ") || "待配置"}</span>
        <span>{node.evidence_satisfied}/{node.evidence_required}</span>
      </span>
      {node.worker_display_name ? <span className="task-board-card__worker">{node.worker_display_name}</span> : null}
      <span className="task-board-card__chevron" aria-hidden="true"><ArrowRight size={14} /></span>
    </button>
  );
}

function columnFor(node: TaskNode): BoardColumnKey {
  if (node.state === "waiting_user" || node.state === "blocked" || node.state === "failed") return "attention";
  if (node.state === "running" || node.state === "assigned" || node.state === "queued" || node.state === "retrying") return "running";
  if (node.state === "waiting_dependency" || node.waiting_on_plan_dependency) return "waiting";
  if (node.state === "succeeded") return "completed";
  return "next";
}

export function boardColumnIcon(column: BoardColumnKey) {
  if (column === "attention") return <CircleAlert size={14} aria-hidden="true" />;
  if (column === "running") return <LoaderCircle size={14} aria-hidden="true" />;
  if (column === "waiting") return <CircleDashed size={14} aria-hidden="true" />;
  return column === "completed" ? <CheckCircle2 size={14} aria-hidden="true" /> : <MoveRight size={14} aria-hidden="true" />;
}
