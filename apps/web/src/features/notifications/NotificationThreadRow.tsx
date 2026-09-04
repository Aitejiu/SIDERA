import { AlertCircle, CheckCircle2, ChevronDown, ChevronRight } from "lucide-react";

import type { NotificationThread } from "./api";

export function NotificationThreadRow({
  thread,
  expanded,
  onToggle,
  children,
}: {
  thread: NotificationThread;
  expanded: boolean;
  onToggle: () => void;
  children?: React.ReactNode;
}) {
  const state = threadState(thread);
  return (
    <article className={`notification-thread${thread.unread_count > 0 ? " is-unread" : ""}`}>
      <button
        className="notification-thread__toggle"
        type="button"
        aria-expanded={expanded}
        aria-controls={`notification-thread-history-${thread.id}`}
        aria-label={`${expanded ? "收起" : "展开"}${thread.title}`}
        onClick={onToggle}
      >
        <span className="notification-thread__chevron" aria-hidden="true">
          {expanded ? <ChevronDown size={17} /> : <ChevronRight size={17} />}
        </span>
        <span className="notification-thread__main">
          <span className="notification-thread__path">{notificationPath(thread)}</span>
          <strong>{thread.title}</strong>
          <span className="notification-thread__summary">{thread.summary}</span>
        </span>
        <span className={`notification-thread__state notification-thread__state--${state.kind}`}>
          {state.icon}
          <span>{state.label}</span>
        </span>
        <time className="notification-thread__time" dateTime={thread.latest_fact_occurred_at}>
          {formatTime(thread.latest_fact_occurred_at)}
        </time>
      </button>
      {expanded ? (
        <div id={`notification-thread-history-${thread.id}`} className="notification-thread__history">
          {children}
        </div>
      ) : null}
    </article>
  );
}

export function notificationPath(thread: NotificationThread): string {
  return [thread.root_task_title, thread.subtask_title, thread.worker_item_title]
    .filter((part): part is string => part !== null && part.trim().length > 0)
    .join(" / ");
}

function threadState(thread: NotificationThread): {
  kind: "action" | "progress" | "archived";
  label: string;
  icon: React.ReactNode;
} {
  if (thread.category === "archived") {
    return {
      kind: "archived",
      label: "已归档",
      icon: <CheckCircle2 size={14} aria-hidden="true" />,
    };
  }
  if (thread.category === "progress") {
    return {
      kind: "progress",
      label: "进度通报",
      icon: <CheckCircle2 size={14} aria-hidden="true" />,
    };
  }
  return {
    kind: "action",
    label: "需要处理",
    icon: <AlertCircle size={14} aria-hidden="true" />,
  };
}

function formatTime(timestamp: string): string {
  return new Date(timestamp).toLocaleString("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
