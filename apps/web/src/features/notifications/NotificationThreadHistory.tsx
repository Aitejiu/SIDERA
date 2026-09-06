import { ExternalLink, MailOpen, RotateCcw, Archive, Wrench } from "lucide-react";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";

import {
  listNotificationDeliveryAttempts,
  notificationAttemptsQueryKey,
  type NotificationThreadDetail,
} from "./api";
import { notificationReasonLabel } from "./NotificationThreadRow";

export function NotificationThreadHistory({
  workspaceId,
  detail,
  pendingCommand,
  onOpenTarget,
  onMarkRead,
  onArchive,
  onRequeue,
}: {
  workspaceId: string;
  detail: NotificationThreadDetail;
  pendingCommand: string | null;
  onOpenTarget: () => void;
  onMarkRead: () => void;
  onArchive: () => void;
  onRequeue: (deliveryId: string, reason: string) => void;
}) {
  const { thread } = detail;
  const [diagnosticsDeliveryId, setDiagnosticsDeliveryId] = useState<string | null>(null);
  const [requeueDeliveryId, setRequeueDeliveryId] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const attemptsQuery = useQuery({
    queryKey: diagnosticsDeliveryId === null
      ? ["notifications", workspaceId, "delivery", "none", "attempts"]
      : notificationAttemptsQueryKey(workspaceId, diagnosticsDeliveryId),
    queryFn: ({ signal }) => listNotificationDeliveryAttempts(
      workspaceId,
      diagnosticsDeliveryId ?? "",
      { signal },
    ),
    enabled: diagnosticsDeliveryId !== null,
  });

  const canMarkRead = detail.available_commands.includes("mark_read")
    && thread.last_read_fact_id !== thread.latest_fact_id;
  const canArchive = detail.available_commands.includes("archive")
    && thread.business_status === "resolved"
    && thread.organization_status === "active";
  const exhaustedDeliveries = detail.deliveries.filter((delivery) => delivery.status === "exhausted");

  return (
    <section className="notification-thread-history" aria-label={`${thread.title}的线程历史`}>
      <div className="notification-thread-history__toolbar">
        <div>
          <span className="eyebrow">THREAD HISTORY</span>
          <span className="notification-thread-history__reference">
            通知引用 · {detail.notification_ref}
          </span>
        </div>
        <div className="notification-thread-history__actions">
          {thread.deep_link ? (
            <button className="quiet-button quiet-button--primary" type="button" onClick={onOpenTarget}>
              <ExternalLink size={14} aria-hidden="true" />
              {openTargetLabel(thread)}
            </button>
          ) : null}
          {canMarkRead ? (
            <button
              className="quiet-button"
              type="button"
              disabled={pendingCommand !== null}
              onClick={onMarkRead}
            >
              <MailOpen size={14} aria-hidden="true" />
              标记已读
            </button>
          ) : null}
          {canArchive ? (
            <button
              className="quiet-button"
              type="button"
              disabled={pendingCommand !== null}
              onClick={onArchive}
            >
              <Archive size={14} aria-hidden="true" />
              归档线程
            </button>
          ) : null}
        </div>
      </div>

      <div className="notification-thread-history__layout">
        <div className="notification-fact-timeline">
          <div className="notification-fact-timeline__heading">Fact 时间线</div>
          {detail.facts.items.length === 0 ? (
            <p className="notification-thread-history__empty">暂无线性事实记录。</p>
          ) : (
            <ol>
              {detail.facts.items.map((fact) => (
                <li key={fact.id} className="notification-fact">
                  <span className="notification-fact__dot" aria-hidden="true" />
                  <div>
                    <div className="notification-fact__heading">
                      <strong>{fact.title}</strong>
                      <time dateTime={fact.occurred_at}>{formatTime(fact.occurred_at)}</time>
                    </div>
                    <p>{fact.summary}</p>
                    {notificationReasonLabel(fact.topic) ? (
                      <span className="notification-fact__reason">
                        {notificationReasonLabel(fact.topic)}
                      </span>
                    ) : null}
                    {fact.action ? <span className="notification-fact__action">{fact.action}</span> : null}
                  </div>
                </li>
              ))}
            </ol>
          )}
        </div>

        <aside className="notification-delivery-panel" aria-label="投递状态">
          <div className="notification-delivery-panel__heading">投递状态</div>
          {detail.deliveries.length === 0 ? (
            <p className="notification-thread-history__empty">当前线程没有外部投递。</p>
          ) : (
            <ul>
              {detail.deliveries.map((delivery) => (
                <li key={delivery.delivery_id} className="notification-delivery">
                  <div>
                    <strong>{delivery.channel_type}</strong>
                    <span>{deliveryStatusLabel(delivery.status)} · 尝试 {delivery.attempts} 次</span>
                  </div>
                  {delivery.status === "exhausted" ? (
                    <div className="notification-delivery__actions">
                      <button
                        className="quiet-button"
                        type="button"
                        onClick={() => setDiagnosticsDeliveryId(delivery.delivery_id)}
                      >
                        <Wrench size={13} aria-hidden="true" />
                        查看投递诊断
                      </button>
                      <button
                        className="quiet-button"
                        type="button"
                        aria-expanded={requeueDeliveryId === delivery.delivery_id}
                        onClick={() => {
                          setRequeueDeliveryId((current) => current === delivery.delivery_id ? null : delivery.delivery_id);
                          setReason("");
                        }}
                      >
                        <RotateCcw size={13} aria-hidden="true" />
                        重新投递
                      </button>
                    </div>
                  ) : null}
                  {requeueDeliveryId === delivery.delivery_id ? (
                    <form
                      className="notification-requeue-form"
                      onSubmit={(event) => {
                        event.preventDefault();
                        const trimmedReason = reason.trim();
                        if (trimmedReason.length > 0) {
                          onRequeue(delivery.delivery_id, trimmedReason);
                        }
                      }}
                    >
                      <label htmlFor={`notification-requeue-reason-${delivery.delivery_id}`}>
                        重新投递原因
                      </label>
                      <textarea
                        id={`notification-requeue-reason-${delivery.delivery_id}`}
                        value={reason}
                        onChange={(event) => setReason(event.target.value)}
                        rows={2}
                        required
                      />
                      <button className="quiet-button quiet-button--primary" type="submit" disabled={pendingCommand !== null || reason.trim().length === 0}>
                        确认重新投递
                      </button>
                    </form>
                  ) : null}
                  {diagnosticsDeliveryId === delivery.delivery_id ? (
                    <div className="notification-attempts">
                      {attemptsQuery.isPending ? <span>正在读取投递诊断…</span> : null}
                      {attemptsQuery.isError ? <span role="alert">无法读取投递诊断</span> : null}
                      {attemptsQuery.data?.items.map((attempt) => (
                        <div className="notification-attempt" key={`${attempt.delivery_id}-${attempt.attempt_number}`}>
                          <span>第 {attempt.attempt_number} 次</span>
                          <span>{attempt.outcome ?? "未完成"}</span>
                          {attempt.error_code ? <span>{attempt.error_code}</span> : null}
                        </div>
                      ))}
                    </div>
                  ) : null}
                </li>
              ))}
            </ul>
          )}
          {exhaustedDeliveries.length > 0 ? (
            <p className="notification-delivery-panel__note">渠道失败不会暂停任务本身。</p>
          ) : null}
        </aside>
      </div>
    </section>
  );
}

function openTargetLabel(thread: NotificationThreadDetail["thread"]): string {
  if (thread.worker_item_id !== null) {
    return "打开 WorkerItem";
  }
  if (thread.subtask_id !== null) {
    return "打开子任务";
  }
  return "打开任务";
}

function deliveryStatusLabel(status: string): string {
  switch (status) {
    case "delivered":
      return "已送达";
    case "retry_wait":
      return "等待重试";
    case "claimed":
      return "处理中";
    case "exhausted":
      return "已耗尽";
    case "suppressed":
      return "已抑制";
    default:
      return "待投递";
  }
}

function formatTime(timestamp: string): string {
  return new Date(timestamp).toLocaleString("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
