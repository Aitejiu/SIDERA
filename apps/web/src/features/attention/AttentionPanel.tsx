import { ArrowRight, Check, CircleAlert } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";

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
  compact = false,
}: {
  workspaceId: string;
  rootTaskId: string;
  rootTaskVersion: number | undefined;
  rootTaskTitle?: string;
  items: AttentionItem[];
  onAcknowledged?: () => void;
  compact?: boolean;
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
    <section className={`attention-panel${compact ? " attention-panel--compact" : ""}`} aria-labelledby="attention-panel-title">
      <div className="section-heading">
        <div>
          {!compact ? <span className="eyebrow">ATTENTION</span> : null}
          <h2 id="attention-panel-title">需要我</h2>
        </div>
        <span className="mono-label">{visibleItems.length} {compact ? "项待处理" : "ITEMS"}</span>
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
                <Link className="attention-item__open" to={attentionHref(item, workspaceId, rootTaskId)}>
                  <div className="attention-item__heading">
                    <strong>{rootTaskTitle ? `${rootTaskTitle} / ${item.subtask_title}` : item.subtask_title}</strong>
                    <ArrowRight size={14} aria-hidden="true" />
                  </div>
                  <p>{item.reason}</p>
                </Link>
                {compact ? (
                  <details className="attention-item__details">
                    <summary>事件详情</summary>
                    <span className="mono-label">{item.kind} · 持续 {formatElapsed(item.occurred_at)} · SOURCE {item.resource_id.slice(0, 8)} · V{item.resource_version}</span>
                  </details>
                ) : (
                  <span className="mono-label">
                    {item.kind} · 持续 {formatElapsed(item.occurred_at)} · SOURCE {item.resource_id.slice(0, 8)} · V{item.resource_version}
                  </span>
                )}
              </div>
              {item.kind === "collaboration_release_proposal" ? (
                <Link
                  className="quiet-button attention-item__action"
                  to={attentionHref(item, workspaceId, rootTaskId)}
                  aria-label={`查看建议：${item.subtask_title}`}
                >
                  <ArrowRight size={14} aria-hidden="true" />查看建议
                </Link>
              ) : (
                <button
                  className="quiet-button attention-item__action"
                  type="button"
                  onClick={() => void acknowledge(item)}
                  disabled={pendingKey !== null || rootTaskVersion === undefined}
                >
                  <Check size={14} aria-hidden="true" />
                  {pendingKey === item.key ? "正在确认" : "确认已处理"}
                </button>
              )}
            </article>
          ))}
        </div>
      )}
    </section>
  );
}

function attentionHref(
  item: AttentionItem,
  workspaceId: string,
  rootTaskId: string,
): string {
  const hasPageLink = item.deep_link?.startsWith(`/workspaces/${encodeURIComponent(workspaceId)}/`);
  if (item.kind === "collaboration_release_proposal" && hasPageLink) {
    return item.deep_link!;
  }
  if (item.worker_item_id) {
    return `/workspaces/${encodeURIComponent(workspaceId)}/worker-items/${encodeURIComponent(item.worker_item_id)}`;
  }
  if (hasPageLink) return item.deep_link!;
  return `/workspaces/${encodeURIComponent(workspaceId)}/tasks/${encodeURIComponent(rootTaskId)}/subtasks/${encodeURIComponent(item.subtask_key)}`;
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
