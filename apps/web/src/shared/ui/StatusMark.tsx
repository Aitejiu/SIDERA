import { Circle, CircleAlert, CircleCheck, CircleX, LoaderCircle } from "lucide-react";

export type StatusMarkKind =
  | "connecting"
  | "live"
  | "stale"
  | "resyncing"
  | "offline"
  | "planned"
  | "running"
  | "completed"
  | "failed";

const labels: Record<StatusMarkKind, string> = {
  connecting: "连接中",
  live: "实时",
  stale: "连接陈旧",
  resyncing: "重新同步",
  offline: "离线",
  planned: "计划中",
  running: "运行中",
  completed: "已完成",
  failed: "失败",
};

const executionLabels: Readonly<Record<string, string>> = {
  planned: "未开始",
  assigned: "已分配",
  queued: "排队中",
  running: "执行中",
  retrying: "重试中",
  waiting_user: "待你处理",
  waiting_dependency: "等待依赖",
  blocked: "已阻塞",
  failed: "失败",
  succeeded: "已完成",
  completed: "已完成",
  cancelled: "已取消",
  stopped: "已停止",
};

export function executionStatusLabel(state: string): string {
  return executionLabels[state] ?? state;
}

export function StatusMark({ kind, label }: { kind: StatusMarkKind; label?: string }) {
  return (
    <span className={`status-mark status-mark--${kind}`}>
      <StatusIcon kind={kind} />
      <span>{label ?? labels[kind]}</span>
    </span>
  );
}

function StatusIcon({ kind }: { kind: StatusMarkKind }) {
  switch (kind) {
    case "live":
    case "completed":
      return <CircleCheck size={14} aria-hidden="true" />;
    case "failed":
    case "offline":
      return <CircleX size={14} aria-hidden="true" />;
    case "stale":
    case "resyncing":
      return <CircleAlert size={14} aria-hidden="true" />;
    case "connecting":
    case "running":
      return <LoaderCircle size={14} aria-hidden="true" />;
    case "planned":
      return <Circle size={14} aria-hidden="true" />;
  }
}
