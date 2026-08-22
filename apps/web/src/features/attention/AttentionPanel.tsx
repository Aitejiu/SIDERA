import { Check, CircleAlert } from "lucide-react";
import { useState } from "react";

import {
  ApiProblem,
  apiCommand,
  createCommandIdentity,
} from "@/shared/api/client";
import type { components } from "@/shared/api/schema";

type AttentionItem = components["schemas"]["TaskConsoleAttentionProjectionResponse"];

export function AttentionPanel({
  workspaceId,
  rootTaskId,
  rootTaskVersion,
  rootTaskTitle,
  items,
  onAcknowledged,
}: {
  workspaceId: string;
  rootTaskId: string;
  rootTaskVersion: number | undefined;
  rootTaskTitle?: string;
  items: AttentionItem[];
  onAcknowledged?: () => void;
}) {
  const [acknowledgedKeys, setAcknowledgedKeys] = useState<Set<string>>(() => new Set());
  const [pendingKey, setPendingKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const visibleItems = items.filter((item) => !acknowledgedKeys.has(item.key));

  const acknowledge = async (item: AttentionItem) => {
    if (rootTaskVersion === undefined || pendingKey !== null) {
      return;
    }
    setPendingKey(item.key);
    setError(null);
    const identity = createCommandIdentity();
    try {
      await apiCommand("/api/v1/commands", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          command_id: identity.commandId,
          workspace_id: workspaceId,
          command_type: "AcknowledgeAttentionItem",
          target_type: "task",
          actor: { type: "local", id: "aitejiu" },
          target_id: rootTaskId,
          expected_version: rootTaskVersion,
          idempotency_key: identity.idempotencyKey,
          correlation_id: crypto.randomUUID(),
          payload: {
            attention_key: item.key,
            source_correlation_id: item.correlation_id,
            source_resource_version: item.resource_version,
          },
        }),
      });
      setAcknowledgedKeys((current) => new Set(current).add(item.key));
      onAcknowledged?.();
    } catch (cause) {
      setError(cause instanceof ApiProblem ? cause.detail : "确认失败，请刷新后重试");
    } finally {
      setPendingKey(null);
    }
  };

  return (
    <section className="attention-panel" aria-labelledby="attention-panel-title">
      <div className="section-heading">
        <div>
          <span className="eyebrow">ATTENTION</span>
          <h2 id="attention-panel-title">需要我</h2>
        </div>
        <span className="mono-label">{visibleItems.length} ITEMS</span>
      </div>
      {error ? <div className="attention-panel__error" role="alert"><CircleAlert size={14} aria-hidden="true" />{error}</div> : null}
      {visibleItems.length === 0 ? (
        <p className="muted-copy" role="status">当前没有待确认事项</p>
      ) : (
        <div className="attention-list">
          {visibleItems.map((item) => (
            <article className="attention-item" key={item.key}>
              <div className="attention-item__marker" aria-hidden="true"><CircleAlert size={15} /></div>
              <div className="attention-item__body">
                <div className="attention-item__heading">
                  <strong>{rootTaskTitle ? `${rootTaskTitle} / ${item.subtask_title}` : item.subtask_title}</strong>
                  <span className="mono-label">{item.kind}</span>
                </div>
                <p>{item.reason}</p>
                <span className="mono-label">
                  持续 {formatElapsed(item.occurred_at)} · SOURCE {item.resource_id.slice(0, 8)} · V{item.resource_version}
                </span>
              </div>
              <button
                className="quiet-button attention-item__action"
                type="button"
                onClick={() => void acknowledge(item)}
                disabled={pendingKey !== null || rootTaskVersion === undefined}
              >
                <Check size={14} aria-hidden="true" />
                {pendingKey === item.key ? "正在确认" : "确认已处理"}
              </button>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}

function formatElapsed(value: string): string {
  const occurredAt = new Date(value).getTime();
  if (Number.isNaN(occurredAt)) {
    return "未知";
  }
  const elapsedMinutes = Math.max(0, Math.floor((Date.now() - occurredAt) / 60_000));
  if (elapsedMinutes < 1) {
    return "刚刚";
  }
  if (elapsedMinutes < 60) {
    return `${elapsedMinutes} 分钟`;
  }
  const elapsedHours = Math.floor(elapsedMinutes / 60);
  if (elapsedHours < 24) {
    return `${elapsedHours} 小时`;
  }
  return `${Math.floor(elapsedHours / 24)} 天`;
}
