import { ArrowRight, CheckCircle2, CircleAlert, CircleDashed, LoaderCircle, MoveRight } from "lucide-react";

import { StatusMark, type StatusMarkKind } from "@/shared/ui/StatusMark";

import type { WorkerItemSummary } from "./types";

type WorkerColumnKey = "attention" | "running" | "waiting" | "next" | "completed";

type WorkerColumn = {
  key: WorkerColumnKey;
  title: string;
  eyebrow: string;
  empty: string;
};

const columns: WorkerColumn[] = [
  { key: "attention", title: "需要处理", eyebrow: "ATTENTION", empty: "没有 WorkerItem 卡住" },
  { key: "running", title: "执行中", eyebrow: "RUNNING", empty: "没有正在执行的 WorkerItem" },
  { key: "waiting", title: "等待", eyebrow: "WAITING", empty: "没有等待中的 WorkerItem" },
  { key: "next", title: "下一步", eyebrow: "NEXT", empty: "没有待启动的 WorkerItem" },
  { key: "completed", title: "已完成", eyebrow: "COMPLETED", empty: "还没有完成的 WorkerItem" },
];

export function WorkerBoard({
  workers,
  onOpen,
}: {
  workers: readonly WorkerItemSummary[];
  onOpen: (workerItemId: string) => void;
}) {
  const grouped = new Map<WorkerColumnKey, WorkerItemSummary[]>();
  for (const column of columns) {
    grouped.set(column.key, []);
  }
  for (const worker of workers) {
    grouped.get(columnFor(worker.status))?.push(worker);
  }

  return (
    <section className="worker-board" aria-labelledby="worker-board-title">
      <div className="section-heading">
        <div>
          <span className="eyebrow">WORKERITEM STATUS BOARD</span>
          <h2 id="worker-board-title">Worker 看板</h2>
        </div>
        <span className="mono-label">{workers.length} 个 WorkerItem</span>
      </div>
      {workers.length === 0 ? (
        <div className="worker-board__empty" role="status">
          <strong>这个子任务还没有 WorkerItem</strong>
          <span>等待 WorkerItem 配置</span>
        </div>
      ) : (
        <div className="worker-board__columns">
          {columns.map((column) => {
            const items = grouped.get(column.key) ?? [];
            return (
              <section
                className={`worker-board__column worker-board__column--${column.key}`}
                key={column.key}
                aria-labelledby={`worker-board-${column.key}`}
              >
                <header className="worker-board__column-heading">
                  <div>
                    <span className="eyebrow">{column.eyebrow}</span>
                    <h3 id={`worker-board-${column.key}`}>{column.title}</h3>
                  </div>
                  <span className="worker-board__count">{items.length}</span>
                </header>
                <div className="worker-board__list">
                  {items.length === 0 ? (
                    <div className="worker-board__column-empty">
                      <MoveRight size={14} aria-hidden="true" />
                      {column.empty}
                    </div>
                  ) : null}
                  {items.map((worker) => (
                    <WorkerCard key={worker.worker_item_id} worker={worker} onOpen={onOpen} />
                  ))}
                </div>
              </section>
            );
          })}
        </div>
      )}
    </section>
  );
}

function WorkerCard({
  worker,
  onOpen,
}: {
  worker: WorkerItemSummary;
  onOpen: (workerItemId: string) => void;
}) {
  return (
    <button
      className="worker-board-card"
      type="button"
      onClick={() => onOpen(worker.worker_item_id)}
      aria-label={`打开 Worker：${worker.title}`}
    >
      <span className="worker-board-card__topline">
        <span className="eyebrow">{worker.role}</span>
        <StatusMark kind={workerStatusKind(worker.status)} />
      </span>
      <strong>{worker.title}</strong>
      <span className="worker-board-card__responsibility">{worker.responsibility}</span>
      <span className="worker-board-card__step">
        <span className="eyebrow">当前步骤</span>
        {worker.current_step ?? "尚未开始"}
      </span>
      {worker.plan_sync ? (
        <span className="worker-board-card__sync">
          计划 {syncStatusLabel(worker.plan_sync.status)} · GEN {worker.plan_sync.target_generation}
        </span>
      ) : null}
      {worker.blocker ? <span className="worker-board-card__blocker"><CircleAlert size={13} aria-hidden="true" />{worker.blocker}</span> : null}
      <span className="worker-board-card__meta">
        <span>Agent</span>
        <span>{worker.current_agent_summary ?? "尚未接入"}</span>
      </span>
      <span className="worker-board-card__footer">
        <span>最近活动</span>
        <span>{formatTime(worker.recent_activity_at)}</span>
      </span>
      <span className="worker-board-card__chevron" aria-hidden="true"><ArrowRight size={14} /></span>
    </button>
  );
}

function syncStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    converged: "已收敛",
    converging: "同步中",
    blocked_unknown: "未知结果",
    degraded: "部分失败",
    waiting: "等待",
  };
  return labels[status] ?? status;
}

function columnFor(status: string): WorkerColumnKey {
  if (status === "waiting_user" || status === "blocked" || status === "failed") {
    return "attention";
  }
  if (status === "running" || status === "assigned" || status === "queued" || status === "retrying") {
    return "running";
  }
  if (status === "waiting_dependency") {
    return "waiting";
  }
  if (status === "succeeded" || status === "completed") {
    return "completed";
  }
  return "next";
}

function workerStatusKind(status: string): StatusMarkKind {
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

function formatTime(value: string | null): string {
  if (value === null) {
    return "暂无";
  }
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}

export function workerColumnIcon(column: WorkerColumnKey) {
  if (column === "attention") return <CircleAlert size={14} aria-hidden="true" />;
  if (column === "running") return <LoaderCircle size={14} aria-hidden="true" />;
  if (column === "waiting") return <CircleDashed size={14} aria-hidden="true" />;
  return column === "completed"
    ? <CheckCircle2 size={14} aria-hidden="true" />
    : <MoveRight size={14} aria-hidden="true" />;
}
