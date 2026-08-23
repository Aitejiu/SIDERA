import { useEffect, useState } from "react";

import type { PlanningRun } from "./types";

export function PlanningRunStatus({
  run,
  onCancel,
}: {
  run: PlanningRun | null;
  onCancel?: (() => void) | undefined;
}) {
  if (run === null) {
    return (
      <section className="planning-run-status" aria-label="规划运行">
        <span className="eyebrow">RUN</span>
        <strong>等待规划运行</strong>
      </section>
    );
  }

  const canCancel = onCancel !== undefined && (run.status === "queued" || run.status === "running");
  const isBlocked = run.status === "blocked";
  const isRunning = run.status === "queued" || run.status === "running";

  return (
    <section className={`planning-run-status planning-run-status--${run.status}`} aria-label="规划运行">
      <div className="planning-run-status__heading">
        <div>
          <span className="eyebrow">RUN</span>
          <strong>{runStatusLabel(run.status)}</strong>
        </div>
        {canCancel ? (
          <button className="quiet-button" type="button" onClick={onCancel}>
            取消规划
          </button>
        ) : null}
      </div>
      <dl className="planning-run-status__facts">
        <div><dt>尝试</dt><dd>第 {run.attempt} 次尝试</dd></div>
        <div><dt>排队</dt><dd>{formatTimestamp(run.queued_at)}</dd></div>
        <div><dt>开始</dt><dd>{run.started_at ? formatTimestamp(run.started_at) : "尚未开始"}</dd></div>
        {isRunning && run.started_at ? <ElapsedFact startedAt={run.started_at} /> : null}
        {run.provider_ref ? <div><dt>Provider ref</dt><dd>{run.provider_ref}</dd></div> : null}
      </dl>
      {isBlocked ? (
        <div className="planning-run-status__notice" role="status">
          <strong>需要运维恢复</strong>
          <span>本次运行没有自动重试，普通用户无需执行恢复命令。</span>
        </div>
      ) : null}
      {run.status === "failed" ? (
        <div className="planning-run-status__notice" role="alert">
          <strong>规划未完成</strong>
          <span>请检查运行记录和请求历史后再决定下一步。</span>
        </div>
      ) : null}
    </section>
  );
}

function ElapsedFact({ startedAt }: { startedAt: string }) {
  const [elapsedSeconds, setElapsedSeconds] = useState(() => elapsedSince(startedAt));

  useEffect(() => {
    const timer = window.setInterval(() => {
      setElapsedSeconds(elapsedSince(startedAt));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [startedAt]);

  return <div><dt>耗时</dt><dd>已运行 {formatDuration(elapsedSeconds)}</dd></div>;
}

export function runStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    queued: "排队中",
    running: "运行中",
    succeeded: "已完成",
    needs_clarification: "等待你的回答",
    failed: "规划失败",
    blocked: "需要运维恢复",
    cancelled: "已取消",
  };
  return labels[status] ?? "规划运行";
}

function elapsedSince(startedAt: string): number {
  return Math.max(0, Math.floor((Date.now() - Date.parse(startedAt)) / 1000));
}

function formatDuration(totalSeconds: number): string {
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  if (minutes < 1) return `${seconds} 秒`;
  const hours = Math.floor(minutes / 60);
  if (hours < 1) return `${minutes} 分钟 ${seconds} 秒`;
  return `${hours} 小时 ${minutes % 60} 分钟`;
}

function formatTimestamp(timestamp: string): string {
  return new Date(timestamp).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}
