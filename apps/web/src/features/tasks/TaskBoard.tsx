import { ArrowRight, CheckCircle2, CircleAlert, CircleDashed, LoaderCircle, MoveRight } from "lucide-react";

import { StatusMark } from "@/shared/ui/StatusMark";

import { statusKind } from "./TaskNode";
import type { SubtaskNode } from "./types";

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
  onOpen,
}: {
  nodes: readonly SubtaskNode[];
  onOpen: (subtaskKey: string) => void;
}) {
  const grouped = new Map<BoardColumnKey, SubtaskNode[]>();
  for (const column of columns) grouped.set(column.key, []);
  for (const node of nodes) grouped.get(columnFor(node))?.push(node);

  return (
    <section className="task-board" aria-labelledby="task-board-title">
      <div className="section-heading">
        <div>
          <span className="eyebrow">SUBTASK STATUS BOARD</span>
          <h2 id="task-board-title">子任务状态看板</h2>
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
                {items.map((node) => <TaskBoardCard key={node.subtask_key} node={node} onOpen={onOpen} />)}
              </div>
            </section>
          );
        })}
      </div>
    </section>
  );
}

function TaskBoardCard({ node, onOpen }: { node: SubtaskNode; onOpen: (subtaskKey: string) => void }) {
  const action = node.current_action ?? (node.state === "planned" ? "等待物化" : node.state === "succeeded" ? "验收已完成" : "等待活动");
  const hasWorkerItems = node.worker_item_count > 0;
  return (
    <button className="task-board-card" type="button" onClick={() => onOpen(node.subtask_key)} aria-label={`打开子任务：${node.title}`}>
      <span className="task-board-card__topline"><span className="eyebrow">{node.subtask_key}</span><StatusMark kind={statusKind(node.state)} /></span>
      <strong>{node.title}</strong>
      <span className="task-board-card__action">{action}</span>
      <span className="task-board-card__meta">
        <span>{hasWorkerItems ? `${node.worker_item_count} 个 WorkerItem` : "待配置 WorkerItem"}</span>
        <span>{node.evidence_satisfied}/{node.evidence_required}</span>
      </span>
      <span className="task-board-card__chevron" aria-hidden="true"><ArrowRight size={14} /></span>
    </button>
  );
}

function columnFor(node: SubtaskNode): BoardColumnKey {
  if (node.state === "waiting_user" || node.state === "blocked" || node.state === "failed") return "attention";
  if (node.state === "running" || node.state === "assigned" || node.state === "queued" || node.state === "retrying") return "running";
  if (node.state === "waiting_dependency") return "waiting";
  if (node.state === "succeeded") return "completed";
  return "next";
}

export function boardColumnIcon(column: BoardColumnKey) {
  if (column === "attention") return <CircleAlert size={14} aria-hidden="true" />;
  if (column === "running") return <LoaderCircle size={14} aria-hidden="true" />;
  if (column === "waiting") return <CircleDashed size={14} aria-hidden="true" />;
  return column === "completed" ? <CheckCircle2 size={14} aria-hidden="true" /> : <MoveRight size={14} aria-hidden="true" />;
}
