import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from "react";

import {
  createProjectionStream,
  RESULT_OVERVIEW_PROJECTION_TYPE,
  type ProjectionChange,
  type ProjectionStream,
  type ProjectionStreamSnapshot,
} from "@/shared/api/projectionStream";
import {
  appendNotification,
  browserNotificationsEnabled,
  hasSentBrowserNotification,
  notificationFromChange,
  notificationFromFact,
  recordSentBrowserNotification,
  readLastNotifiedCursor,
  sendBrowserNotification,
  writeLastNotifiedCursor,
  type WorkspaceNotification,
} from "@/features/attention/notifications";
import {
  getNotificationThread,
  type NotificationThreadDetail,
} from "@/features/notifications/api";
import {
  resolveNotificationOwner,
  type NotificationOwnershipApi,
} from "@/features/attention/notificationOwnership";

interface WorkspaceStreamContextValue {
  streamState: ProjectionStreamSnapshot;
  seedSnapshotCursor: (cursor: number) => void;
  notices: readonly WorkspaceNotification[];
  dismissNotice: (id: string) => void;
}

const WorkspaceStreamContext = createContext<WorkspaceStreamContextValue | null>(null);

interface WorkspaceStreamNotificationDependencies {
  workspaceId: string;
  invalidationQueue: Pick<ReturnType<typeof createInvalidationQueue>, "enqueue">;
  setNotices: (
    updater: (current: WorkspaceNotification[]) => WorkspaceNotification[],
  ) => void;
  ownershipApi?: NotificationOwnershipApi;
  fetchNotificationThread?: (
    workspaceId: string,
    threadId: string,
  ) => Promise<NotificationThreadDetail>;
  sendBrowserNotification?: (notification: WorkspaceNotification) => boolean;
  storage?: Storage;
}

export function handleWorkspaceProjectionChange(
  change: ProjectionChange,
  dependencies: WorkspaceStreamNotificationDependencies,
): void {
  const {
    invalidationQueue,
    ownershipApi,
    fetchNotificationThread,
    sendBrowserNotification: sendBrowserNotificationOverride,
    setNotices,
    storage,
    workspaceId,
  } = dependencies;
  invalidationQueue.enqueue(change);
  const notification = notificationFromChange(workspaceId, change);
  if (change.cursor <= readLastNotifiedCursor(workspaceId, storage)) {
    return;
  }
  writeLastNotifiedCursor(workspaceId, change.cursor, storage);
  if (notification !== null) {
    setNotices((current) => appendNotification(current, notification));
  }

  if (!browserNotificationsEnabled(workspaceId, storage)) {
    return;
  }
  if (change.projection_type !== "notification_thread" || change.resource_id === null) {
    return;
  }
  const fetchThread = fetchNotificationThread ?? ((nextWorkspaceId: string, threadId: string) =>
    getNotificationThread(nextWorkspaceId, threadId));
  void fetchThread(workspaceId, change.resource_id)
    .then((detail) => {
      const fact = detail.facts.items.find((item) => item.id === detail.thread.latest_fact_id)
        ?? detail.facts.items[0];
      if (fact === undefined) {
        return null;
      }
      const freshNotification = notificationFromFact(workspaceId, fact);
      if (
        freshNotification === null
        || hasSentBrowserNotification(workspaceId, freshNotification.id, storage)
      ) {
        return null;
      }
      return resolveNotificationOwner(
        workspaceId,
        projectionChangeFromFact(change, fact),
        ownershipApi,
      ).then((owner) => ({ owner, notification: freshNotification }));
    })
    .then((result) => {
      if (result === null || result.owner !== "web") {
        return;
      }
      try {
        const sent = (sendBrowserNotificationOverride ?? sendBrowserNotification)(result.notification);
        if (sent) {
          recordSentBrowserNotification(workspaceId, result.notification.id, storage);
        }
      } catch {
        // System notifications are best-effort and must not escape the stream callback.
      }
    })
    .catch(() => {
      // A failed detail or owner lookup leaves the server-authoritative Inbox untouched.
    });
}

export function WorkspaceStreamProvider({
  workspaceId,
  children,
}: {
  workspaceId: string;
  children: ReactNode;
}) {
  const queryClient = useQueryClient();
  const [notices, setNotices] = useState<WorkspaceNotification[]>([]);

  const invalidationQueue = useMemo(
    () => createInvalidationQueue(queryClient, workspaceId),
    [queryClient, workspaceId],
  );

  const handleChange = useCallback(
    (change: ProjectionChange) => {
      handleWorkspaceProjectionChange(change, {
        invalidationQueue,
        setNotices,
        workspaceId,
      });
    },
    [invalidationQueue, workspaceId],
  );

  const handleResync = useCallback(
    () => {
      invalidateWorkspaceSnapshot(queryClient, workspaceId);
    },
    [queryClient, workspaceId],
  );

  const stream = useMemo<ProjectionStream>(
    () =>
      createProjectionStream(workspaceId, {
        onChange: handleChange,
        onResyncRequired: handleResync,
      }),
    [handleChange, handleResync, workspaceId],
  );

  const streamState = useSyncExternalStore(
    stream.subscribe,
    stream.getSnapshot,
    stream.getSnapshot,
  );

  useEffect(() => {
    return () => {
      stream.close();
      invalidationQueue.close();
    };
  }, [invalidationQueue, stream]);

  const value = useMemo<WorkspaceStreamContextValue>(
    () => ({
      streamState,
      seedSnapshotCursor: stream.seedSnapshotCursor,
      notices,
      dismissNotice: (id: string) =>
        setNotices((current) => current.filter((notice) => notice.id !== id)),
    }),
    [notices, stream, streamState],
  );

  return (
    <WorkspaceStreamContext.Provider value={value}>
      {children}
    </WorkspaceStreamContext.Provider>
  );
}

export function useWorkspaceStream(): WorkspaceStreamContextValue {
  const context = useContext(WorkspaceStreamContext);
  if (context === null) {
    throw new Error("useWorkspaceStream must be used inside WorkspaceStreamProvider");
  }
  return context;
}

export function useWorkspaceStreamOptional(): WorkspaceStreamContextValue | null {
  return useContext(WorkspaceStreamContext);
}

export function matchesProjectionQuery(
  queryKey: readonly unknown[],
  workspaceId: string,
  change: ProjectionChange,
  queryData?: unknown,
): boolean {
  if (!queryKey.includes(workspaceId)) {
    return false;
  }
  const queryType = queryKey[0];
  const matchesResource = (resourceQueryIndex = 2) =>
    change.resource_id === null || queryKey[resourceQueryIndex] === change.resource_id;

  switch (change.projection_type) {
    case "workers":
      return queryType === "workers"
        || (queryType === "worker" && matchesResource())
        || (queryType === "worker-runtime" && matchesResource());
    case "worker_runtime":
      return queryType === "workers"
        || (queryType === "worker" && matchesResource())
        || (queryType === "worker-runtime" && matchesResource());
    case "executions":
      return queryType === "executions"
        || (queryType === "execution" && matchesResource());
    case "task_overview":
      return queryType === "task-overviews" ||
        (queryType === "task-overview" && matchesResource());
    case "task_graph":
      return queryType === "task-graph" && matchesResource();
    case "subtask_workspace":
      return queryType === "subtask-workspace" && matchesResource();
    case "worker_item_workspace":
      return ((queryType === "worker-item-workspace"
        || queryType === "native-worker-context") && matchesResource())
        || (queryType === "agent-assignment"
          && queryKey[2] === "worker_item" && matchesResource(3));
    case "worker_item_activity":
      return queryType === "worker-item-activity" && matchesResource();
    case "collaboration_governance":
      return queryType === "collaboration-detail"
        || queryType === "collaboration-decisions"
        || (queryType === "collaboration-governance-worker-item" && matchesResource())
        || (queryType === "collaboration-governance-proposal" && matchesResource());
    case "worker_trace":
      return queryType === "worker-traces" ||
        (queryType === "worker-trace" && matchesResource());
    case RESULT_OVERVIEW_PROJECTION_TYPE:
      return queryType === "task-results" && matchesResource();
    case "planning_request":
      return queryType === "planning-requests"
        || (queryType === "planning-request" && matchesResource())
        || (queryType === "planning-briefs" && matchesResource())
        || (queryType === "planning-runs" && matchesResource())
        || (queryType === "planning-clarifications" && matchesResource())
        || (queryType === "planning-proposal" && matchesResource())
        || (queryType === "planning-materialization" && matchesResource());
    case "work_context":
      return queryType === "work-context" && (
        change.resource_id === null
        || queryKey[2] === change.resource_id
        || queryKey[2] === "root-task"
        || queryKey[2] === "planning-request"
      );
    case "context_package":
      return queryType === "worker-result-inputs"
        || (queryType === "context-package" && (
          change.resource_id === null
          || queryKey[2] === change.resource_id
          || queryKey[2] === "execution"
          || queryKey[2] === "planning-run"
        ));
    case "knowledge_candidate_submission":
      // The CP projection is keyed by submission aggregate, while its task query
      // is a relation lookup. Keep this invalidation inside that query family.
      return queryType === "context-knowledge-submissions";
    case "notification_thread":
      return queryType === "notifications"
        && (
          queryKey[2] === "summary"
          || queryKey[2] === "threads"
          || (
            queryKey[2] === "thread"
            && (change.resource_id === null || queryKey[3] === change.resource_id)
          )
        );
    case "notification_delivery":
      return queryType === "notifications"
        && (
          queryKey[2] === "summary"
          || queryKey[2] === "threads"
          || queryKey[2] === "readiness"
          || queryKey[2] === "modules"
          || (
            queryKey[2] === "delivery"
            && queryKey[3] === change.resource_id
            && queryKey[4] === "attempts"
          )
          || (
            queryKey[2] === "thread"
            && (change.resource_id === null || hasDelivery(queryData, change.resource_id))
          )
        );
    default:
      return false;
  }
}

export function invalidateWorkspaceSnapshot(
  queryClient: QueryClient,
  workspaceId: string,
): void {
  void queryClient.invalidateQueries({
    predicate: (query) => query.queryKey.includes(workspaceId),
    refetchType: "active",
  });
}

export function createInvalidationQueue(queryClient: QueryClient, workspaceId: string) {
  const pendingChanges = new Map<string, ProjectionChange>();
  let frameHandle: number | null = null;
  let lastEnqueuedCursor = 0;
  let closed = false;
  let inFlight = false;

  const schedule = () => {
    if (!closed && !inFlight && pendingChanges.size > 0 && frameHandle === null) {
      frameHandle = requestAnimationFrameSafe(flush);
    }
  };

  const flush = () => {
    frameHandle = null;
    if (closed || inFlight || pendingChanges.size === 0) {
      return;
    }
    const changes = [...pendingChanges.values()];
    pendingChanges.clear();
    inFlight = true;
    const fetchingQueries = queryClient.getQueryCache().findAll({
      type: "active",
      fetchStatus: "fetching",
    }).filter((query) => !query.isDisabled() && !query.isStatic());
    // A joined read may predate the event, so retain one fresh follow-up.
    for (const change of changes) {
      if (fetchingQueries.some((query) => matchesProjectionQuery(
        query.queryKey,
        workspaceId,
        change,
        query.state.data,
      ))) {
        const key = `${change.projection_type}:${change.resource_id ?? "workspace"}`;
        pendingChanges.set(key, change);
      }
    }
    const finish = () => {
      inFlight = false;
      schedule();
    };
    void Promise.resolve(queryClient.invalidateQueries({
      predicate: (query) => changes.some((change) => matchesProjectionQuery(
        query.queryKey,
        workspaceId,
        change,
        query.state.data,
      )),
    }, { cancelRefetch: false })).then(finish, finish);
  };

  return {
    enqueue(change: ProjectionChange) {
      if (closed || change.cursor <= lastEnqueuedCursor) {
        return;
      }
      lastEnqueuedCursor = change.cursor;
      const key = `${change.projection_type}:${change.resource_id ?? "workspace"}`;
      pendingChanges.set(key, change);
      schedule();
    },
    close() {
      closed = true;
      if (frameHandle !== null) {
        cancelAnimationFrameSafe(frameHandle);
      }
      frameHandle = null;
      pendingChanges.clear();
    },
  };
}

function projectionChangeFromFact(
  fallback: ProjectionChange,
  fact: NotificationThreadDetail["facts"]["items"][number],
): ProjectionChange {
  return {
    ...fallback,
    cursor: fact.source_projection_cursor,
    projection_type: fact.source_projection_type,
    resource_id: fact.source_resource_id,
    change_type: fact.kind,
    resource_version: fact.source_resource_version,
    occurred_at: fact.occurred_at,
    correlation_id: fact.correlation_id,
  };
}

function hasDelivery(data: unknown, deliveryId: string | null): boolean {
  if (deliveryId === null || !isRecord(data) || !Array.isArray(data.deliveries)) {
    return false;
  }
  return data.deliveries.some(
    (delivery): boolean => isRecord(delivery) && delivery.delivery_id === deliveryId,
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function requestAnimationFrameSafe(callback: () => void): number {
  if (typeof window.requestAnimationFrame === "function") {
    return window.requestAnimationFrame(callback);
  }
  return window.setTimeout(callback, 0);
}

function cancelAnimationFrameSafe(handle: number): void {
  if (typeof window.cancelAnimationFrame === "function") {
    window.cancelAnimationFrame(handle);
    return;
  }
  window.clearTimeout(handle);
}
