import { MessageCircle, Plus, RefreshCw } from "lucide-react";

import type { MainAgentThread } from "./types";

export function ChatThreadList({
  threads,
  activeThreadId,
  isLoading,
  isError,
  onSelect,
  onCreate,
  onRetry,
  createDisabled = false,
}: {
  threads: MainAgentThread[];
  activeThreadId: string | undefined;
  isLoading: boolean;
  isError: boolean;
  onSelect: (threadId: string) => void;
  onCreate: () => void;
  onRetry: () => void;
  createDisabled?: boolean;
}) {
  return (
    <aside className="chat-thread-list" aria-label="对话列表">
      <div className="chat-pane-heading">
        <div>
          <span className="eyebrow">THREADS</span>
          <h2>对话</h2>
        </div>
        <button
          className="icon-button"
          type="button"
          onClick={onCreate}
          disabled={createDisabled}
          aria-label="新建对话"
          title="新建对话"
        >
          <Plus size={16} aria-hidden="true" />
        </button>
      </div>

      {isLoading ? <div className="chat-pane-state">正在读取对话</div> : null}
      {isError ? (
        <div className="chat-pane-state chat-pane-state--error" role="alert">
          <strong>无法读取对话</strong>
          <button className="quiet-button" type="button" onClick={onRetry}>
            <RefreshCw size={14} aria-hidden="true" />重试读取
          </button>
        </div>
      ) : null}
      {!isLoading && !isError && threads.length === 0 ? (
        <div className="chat-pane-state chat-pane-state--empty">
          <MessageCircle size={18} aria-hidden="true" />
          <strong>暂无对话</strong>
        </div>
      ) : null}
      {!isLoading && !isError && threads.length > 0 ? (
        <div className="chat-thread-list__items">
          {threads.map((thread) => (
            <button
              className={`chat-thread-row${activeThreadId === thread.id ? " is-active" : ""}`}
              key={thread.id}
              type="button"
              aria-pressed={activeThreadId === thread.id}
              onClick={() => onSelect(thread.id)}
            >
              <span className="chat-thread-row__mark" aria-hidden="true">
                <MessageCircle size={15} />
              </span>
              <span className="chat-thread-row__body">
                <strong>{thread.title}</strong>
                <span>{thread.turns.length === 0 ? "尚未开始" : `${thread.turns.length} 轮对话`}</span>
              </span>
              <span className="mono-label">{formatThreadDate(thread.updatedAt)}</span>
            </button>
          ))}
        </div>
      ) : null}
    </aside>
  );
}

function formatThreadDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "--";
  }
  return date.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" });
}
