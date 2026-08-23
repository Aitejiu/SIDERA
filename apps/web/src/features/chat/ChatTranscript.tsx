import { AlertTriangle, Bot, RotateCcw, UserRound, Wrench } from "lucide-react";

import type { JsonValue, MainAgentItem, MainAgentTurn } from "./types";

const TERMINAL_STATUSES = new Set(["completed", "interrupted", "failed", "cancelled"]);

export function ChatTranscript({
  turns,
  onRetry,
}: {
  turns: MainAgentTurn[];
  onRetry: (turnId: string) => void;
}) {
  if (turns.length === 0) {
    return (
      <div className="chat-transcript chat-transcript--empty">
        <span className="chat-transcript__empty-mark" aria-hidden="true">·</span>
        <strong>还没有消息</strong>
      </div>
    );
  }

  return (
    <div className="chat-transcript" aria-label="对话内容">
      {turns.map((turn) => (
        <article className={`chat-turn chat-turn--${turn.status}`} key={turn.id}>
          <div className="chat-turn__heading">
            <span className="chat-turn__identity">
              {turnHasUserMessage(turn) ? <UserRound size={15} aria-hidden="true" /> : <Bot size={15} aria-hidden="true" />}
              <strong>{turnHasUserMessage(turn) ? "你" : "Main Agent"}</strong>
            </span>
            <span className={`chat-turn__status chat-turn__status--${turn.status}`}>
              {turnStatusLabel(turn.status)}
            </span>
          </div>
          <div className="chat-turn__items">
            {turn.items.map((item) => <TranscriptItem item={item} key={item.id} />)}
            {turn.items.length === 0 && turn.finalResponse ? (
              <p className="chat-message chat-message--assistant">{turn.finalResponse}</p>
            ) : null}
          </div>
          {turn.error ? (
            <div className="chat-turn__error" role="alert">
              <AlertTriangle size={14} aria-hidden="true" />
              <span>这轮对话没有完成</span>
            </div>
          ) : null}
          {TERMINAL_STATUSES.has(turn.status) && turn.status !== "completed" ? (
            <button className="quiet-button chat-turn__retry" type="button" onClick={() => onRetry(turn.id)}>
              <RotateCcw size={14} aria-hidden="true" />重试
            </button>
          ) : null}
        </article>
      ))}
    </div>
  );
}

function TranscriptItem({ item }: { item: MainAgentItem }) {
  const text = itemText(item.data);
  if (item.type === "userMessage") {
    return <p className="chat-message chat-message--user">{text}</p>;
  }
  if (item.type === "assistantMessage") {
    return <p className="chat-message chat-message--assistant">{text}</p>;
  }
  if (item.type === "toolCall") {
    return (
      <div className="chat-tool-event">
        <Wrench size={14} aria-hidden="true" />
        <span>{text || "工具执行"}</span>
      </div>
    );
  }
  return (
    <div className="chat-turn__error" role="alert">
      <AlertTriangle size={14} aria-hidden="true" />
      <span>{text || "发生错误"}</span>
    </div>
  );
}

function turnHasUserMessage(turn: MainAgentTurn): boolean {
  return turn.items.some((item) => item.type === "userMessage");
}

function itemText(data: Record<string, JsonValue>): string {
  const candidates = [data.content, data.text, data.message, data.input, data.summary];
  const value = candidates.find((candidate): candidate is string => typeof candidate === "string");
  if (value !== undefined) {
    return value;
  }
  return "";
}

function turnStatusLabel(status: MainAgentTurn["status"]): string {
  const labels: Record<MainAgentTurn["status"], string> = {
    queued: "排队中",
    in_progress: "执行中",
    completed: "已完成",
    interrupted: "已中断",
    failed: "失败",
    cancelled: "已取消",
  };
  return labels[status];
}

export function getTurnInput(turn: MainAgentTurn): string | null {
  const userItem = turn.items.find((item) => item.type === "userMessage");
  if (userItem === undefined) {
    return null;
  }
  const value = userItem.data.content ?? userItem.data.text ?? userItem.data.input;
  return typeof value === "string" && value.trim() !== "" ? value : null;
}
