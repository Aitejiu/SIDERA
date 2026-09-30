import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertCircle, CheckCircle2, CircleDashed, Mail, PackageOpen, Radio } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

import { useWorkspaceStreamOptional } from "@/app/WorkspaceStreamProvider";
import { AsyncState } from "@/shared/ui/AsyncState";

import {
  buildArchiveNotificationThreadCommand,
  buildMarkNotificationThreadReadCommand,
  buildRequeueNotificationDeliveryCommand,
  executeNotificationCommand,
  getNotificationSummary,
  getNotificationThread,
  listNotificationThreads,
  notificationSummaryQueryKey,
  notificationThreadQueryKey,
  notificationThreadsQueryKey,
  type NotificationThreadCategory,
} from "./api";
import { NotificationThreadHistory } from "./NotificationThreadHistory";
import { NotificationThreadRow } from "./NotificationThreadRow";

const DEFAULT_WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";

export function InboxPage() {
  const workspaceId = useParams<{ workspaceId: string }>().workspaceId ?? DEFAULT_WORKSPACE_ID;
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const stream = useWorkspaceStreamOptional();
  const [category, setCategory] = useState<NotificationThreadCategory>("action");
  const [expandedThreadId, setExpandedThreadId] = useState<string | null>(null);
  const [commandLabel, setCommandLabel] = useState<string | null>(null);
  const [lastCommandLabel, setLastCommandLabel] = useState<string | null>(null);
  const summaryQuery = useQuery({
    queryKey: notificationSummaryQueryKey(workspaceId),
    queryFn: ({ signal }) => getNotificationSummary(workspaceId, signal),
    enabled: workspaceId.length > 0,
  });
  const threadsQuery = useInfiniteQuery({
    queryKey: notificationThreadsQueryKey(workspaceId, { category }),
    initialPageParam: undefined as string | undefined,
    queryFn: ({ signal, pageParam }) => listNotificationThreads(workspaceId, {
      category,
      ...(pageParam === undefined ? {} : { cursor: pageParam }),
      signal,
    }),
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    enabled: workspaceId.length > 0,
  });
  const threadQuery = useQuery({
    queryKey: expandedThreadId === null
      ? notificationThreadQueryKey(workspaceId, "none")
      : notificationThreadQueryKey(workspaceId, expandedThreadId),
    queryFn: ({ signal }) => getNotificationThread(workspaceId, expandedThreadId ?? "", { signal }),
    enabled: expandedThreadId !== null,
  });
  const commandMutation = useMutation({
    mutationFn: executeNotificationCommand,
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: notificationSummaryQueryKey(workspaceId) }),
        queryClient.invalidateQueries({ queryKey: ["notifications", workspaceId, "threads"] }),
        ...(expandedThreadId === null
          ? []
          : [queryClient.invalidateQueries({ queryKey: notificationThreadQueryKey(workspaceId, expandedThreadId) })]),
      ]);
    },
    onSettled: () => {
      setCommandLabel(null);
    },
  });
  const threads = useMemo(
    () => threadsQuery.data?.pages.flatMap((page) => page.items) ?? [],
    [threadsQuery.data?.pages],
  );
  const summary = summaryQuery.data;
  const snapshotCursor = summary?.last_change_cursor
    ?? threadsQuery.data?.pages.at(-1)?.last_change_cursor;
  useEffect(() => {
    if (stream !== null && snapshotCursor !== undefined) {
      stream.seedSnapshotCursor(snapshotCursor);
    }
  }, [snapshotCursor, stream]);

  const streamStatus = stream?.streamState.status;
  const stale = threadsQuery.isRefetchError
    || (threadsQuery.isError && threadsQuery.data !== undefined);

  const runCommand = (label: string, command: Parameters<typeof executeNotificationCommand>[0]) => {
    setCommandLabel(label);
    setLastCommandLabel(label);
    commandMutation.mutate(command);
  };

  const openTarget = () => {
    const deepLink = threadQuery.data?.deep_link ?? threadQuery.data?.thread.deep_link;
    if (deepLink === undefined || deepLink.length === 0) {
      return;
    }
    try {
      const target = new URL(deepLink, window.location.origin);
      if (target.origin !== window.location.origin) {
        return;
      }
      navigate(`${target.pathname}${target.search}${target.hash}`);
    } catch {
      return;
    }
  };

  return (
    <section className="workspace-page notification-inbox" aria-labelledby="notification-inbox-title">
      <div className="breadcrumb">工作区 / 信箱</div>
      <header className="notification-inbox__heading">
        <div className="workspace-page__title">
          <span className="workspace-page__icon"><Mail size={18} aria-hidden="true" /></span>
          <div>
            <span className="eyebrow">NOTIFICATION LEDGER</span>
            <h1 id="notification-inbox-title">信箱</h1>
          </div>
        </div>
        <span className="mono-label">SERVER AUTHORITATIVE</span>
      </header>

      <div className="notification-inbox__summary" aria-label="信箱概览">
        <SummaryMetric label="需要处理" value={summary?.open_count} tone="action" />
        <SummaryMetric label="今日进度" value={summary?.progress_count} tone="progress" />
        <SummaryMetric label="渠道异常" value={summary?.channel_issue_count} tone="issue" />
      </div>

      <div className="notification-inbox__categories" role="tablist" aria-label="信箱分类">
        <CategoryTab
          active={category === "action"}
          label="需要处理"
          count={summary?.open_count}
          onClick={() => {
            setCategory("action");
            setExpandedThreadId(null);
          }}
        />
        <CategoryTab
          active={category === "progress"}
          label="进度通报"
          count={summary?.progress_count}
          onClick={() => {
            setCategory("progress");
            setExpandedThreadId(null);
          }}
        />
        <CategoryTab
          active={category === "archived"}
          label="已归档"
          onClick={() => {
            setCategory("archived");
            setExpandedThreadId(null);
          }}
        />
      </div>

      {summaryQuery.isError ? (
        <div className="notification-inbox__summary-warning" role="alert">
          <AlertCircle size={15} aria-hidden="true" />
          <span>汇总暂时不可用，列表仍以服务端线程为准。</span>
        </div>
      ) : null}
      {stale ? (
        <div className="notification-inbox__stale" role="status">
          <CircleDashed size={15} aria-hidden="true" />
          <span>当前显示的是上次成功读取的信箱快照</span>
          <button className="quiet-button" type="button" onClick={() => void threadsQuery.refetch()}>
            重试读取
          </button>
        </div>
      ) : null}
      {streamStatus !== undefined && streamStatus !== "live" && streamStatus !== "connecting" ? (
        <div className="notification-inbox__stream-state" role="status" aria-label="信箱实时状态">
          <CircleDashed size={15} aria-hidden="true" />
          <span>{notificationStreamStatusLabel(streamStatus)}</span>
        </div>
      ) : null}

      <AsyncState
        isLoading={threadsQuery.isPending}
        isError={threadsQuery.isError && threadsQuery.data === undefined}
        isEmpty={false}
        errorMessage="无法读取信箱"
        retryLabel="重试读取信箱"
        onRetry={() => void threadsQuery.refetch()}
      >
        {threads.length === 0 ? (
          <EmptyNotificationState category={category} />
        ) : (
          <div className="notification-thread-list">
            {threads.map((thread) => (
              <NotificationThreadRow
                key={thread.id}
                thread={thread}
                expanded={expandedThreadId === thread.id}
                onToggle={() => setExpandedThreadId((current) => current === thread.id ? null : thread.id)}
              >
                {threadQuery.isPending && expandedThreadId === thread.id ? (
                  <div className="notification-thread-history__loading">正在读取线程历史…</div>
                ) : null}
                {threadQuery.isError && expandedThreadId === thread.id ? (
                  <div className="notification-thread-history__error" role="alert">
                    无法读取线程历史
                    <button className="quiet-button" type="button" onClick={() => void threadQuery.refetch()}>
                      重试读取
                    </button>
                  </div>
                ) : null}
                {threadQuery.data && expandedThreadId === thread.id ? (
                  <NotificationThreadHistory
                    key={threadQuery.data.thread.id}
                    workspaceId={workspaceId}
                    detail={threadQuery.data}
                    pendingCommand={commandMutation.isPending ? commandLabel : null}
                    onOpenTarget={openTarget}
                    onMarkRead={() => runCommand(
                      "标记已读",
                      buildMarkNotificationThreadReadCommand({
                        workspaceId,
                        threadId: threadQuery.data.thread.id,
                        expectedVersion: threadQuery.data.thread.version,
                        expectedLatestFactId: threadQuery.data.thread.latest_fact_id,
                      }),
                    )}
                    onArchive={() => runCommand(
                      "归档线程",
                      buildArchiveNotificationThreadCommand({
                        workspaceId,
                        threadId: threadQuery.data.thread.id,
                        expectedVersion: threadQuery.data.thread.version,
                      }),
                    )}
                    onRequeue={(deliveryId, reason) => runCommand(
                      "重新投递",
                      buildRequeueNotificationDeliveryCommand({
                        workspaceId,
                        deliveryId,
                        expectedRouteVersion: 1,
                        reason,
                      }),
                    )}
                  />
                ) : null}
              </NotificationThreadRow>
            ))}
          </div>
        )}
      </AsyncState>
      {threadsQuery.hasNextPage ? (
        <button className="quiet-button notification-inbox__load-more" type="button" disabled={threadsQuery.isFetchingNextPage} onClick={() => void threadsQuery.fetchNextPage()}>
          {threadsQuery.isFetchingNextPage ? "正在读取更多" : "读取更多"}
        </button>
      ) : null}
      {commandMutation.isError ? (
        <div className="notification-inbox__command-error" role="alert">
          <Radio size={15} aria-hidden="true" />
          <span>{commandMutation.error instanceof Error ? commandMutation.error.message : "操作未完成，请刷新后重试"}</span>
        </div>
      ) : null}
      {commandMutation.isSuccess ? (
        <div className="notification-inbox__command-success" role="status">
          <CheckCircle2 size={15} aria-hidden="true" />
          <span>{lastCommandLabel ?? commandLabel ?? "操作"}已提交</span>
        </div>
      ) : null}
    </section>
  );
}

function SummaryMetric({
  label,
  value,
  tone,
}: {
  label: string;
  value: number | undefined;
  tone: "action" | "progress" | "issue";
}) {
  return (
    <div className={`notification-summary-metric notification-summary-metric--${tone}`}>
      <span>{label} {value ?? "—"}</span>
    </div>
  );
}

function CategoryTab({
  active,
  label,
  count,
  onClick,
}: {
  active: boolean;
  label: string;
  count?: number | undefined;
  onClick: () => void;
}) {
  return (
    <button
      className={`notification-category-tab${active ? " is-active" : ""}`}
      type="button"
      role="tab"
      aria-selected={active}
      onClick={onClick}
    >
      {label}{count === undefined ? "" : ` ${count}`}
    </button>
  );
}

function EmptyNotificationState({ category }: { category: NotificationThreadCategory }) {
  const copy = category === "action"
    ? { title: "当前没有需要处理的事项", icon: <CircleDashed size={18} aria-hidden="true" /> }
    : category === "progress"
      ? { title: "当前没有新的进度通报", icon: <CheckCircle2 size={18} aria-hidden="true" /> }
      : { title: "暂无已归档事项", icon: <PackageOpen size={18} aria-hidden="true" /> };
  return (
    <div className="notification-empty" role="status">
      <span className="notification-empty__icon">{copy.icon}</span>
      <strong>{copy.title}</strong>
    </div>
  );
}

function notificationStreamStatusLabel(
  status: NonNullable<ReturnType<typeof useWorkspaceStreamOptional>>["streamState"]["status"],
): string {
  switch (status) {
    case "offline":
      return "实时连接已断开，当前仍显示最后一次成功快照";
    case "resyncing":
      return "正在重新同步服务端信箱快照";
    case "stale":
      return "实时连接暂时不可用，当前仍显示最后一次成功快照";
    default:
      return "正在建立实时连接";
  }
}
