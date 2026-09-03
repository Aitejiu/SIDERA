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
  type ProjectionChange,
  type ProjectionStream,
  type ProjectionStreamSnapshot,
} from "@/shared/api/projectionStream";
import {
  appendNotification,
  browserNotificationsEnabled,
  hasSentBrowserNotification,
  notificationFromChange,
  recordSentBrowserNotification,
  readLastNotifiedCursor,
  sendBrowserNotification,
  writeLastNotifiedCursor,
  type WorkspaceNotification,
} from "@/features/attention/notifications";
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
    sendBrowserNotification: sendBrowserNotificationOverride,
    setNotices,
    storage,
    workspaceId,
  } = dependencies;
  invalidationQueue.enqueue(change);
  const notification = notificationFromChange(workspaceId, change);
  if (notification === null) {
    return;
  }
  if (change.cursor <= readLastNotifiedCursor(workspaceId, storage)) {
    return;
  }
  writeLastNotifiedCursor(workspaceId, change.cursor, storage);
  setNotices((current) => appendNotification(current, notification));

  if (!browserNotificationsEnabled(workspaceId, storage)) {
    return;
  }
  void resolveNotificationOwner(workspaceId, change, ownershipApi).then((owner) => {
    if (
      owner !== "web"
      || hasSentBrowserNotification(workspaceId, notification.id, storage)
    ) {
      return;
    }
    try {
      const sent = (sendBrowserNotificationOverride ?? sendBrowserNotification)(notification);
      if (sent) {
        recordSentBrowserNotification(workspaceId, notification.id, storage);
      }
    } catch {
      // System notifications are best-effort and must not escape the stream callback.
    }
  }).catch(() => {
    // Owner lookup failures leave the notice in the tray without guessing its source.
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
      queryClient.invalidateQueries({
        predicate: (query) => query.queryKey.includes(workspaceId),
      });
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
      return queryType === "worker-item-workspace" && matchesResource();
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
      return queryType === "context-package" && (
        change.resource_id === null
        || queryKey[2] === change.resource_id
        || queryKey[2] === "execution"
        || queryKey[2] === "planning-run"
      );
    case "knowledge_candidate_submission":
      // The CP projection is keyed by submission aggregate, while its task query
      // is a relation lookup. Keep this invalidation inside that query family.
      return queryType === "context-knowledge-submissions";
    default:
      return false;
  }
}

function createInvalidationQueue(queryClient: QueryClient, workspaceId: string) {
  const pendingChanges = new Map<string, ProjectionChange>();
  let frameHandle: number | null = null;

  const flush = () => {
    frameHandle = null;
    const changes = [...pendingChanges.values()];
    pendingChanges.clear();
    for (const change of changes) {
      queryClient.invalidateQueries({
        predicate: (query) => matchesProjectionQuery(query.queryKey, workspaceId, change),
      });
    }
  };

  return {
    enqueue(change: ProjectionChange) {
      const key = `${change.projection_type}:${change.resource_id ?? "workspace"}`;
      pendingChanges.set(key, change);
      if (frameHandle === null) {
        frameHandle = requestAnimationFrameSafe(flush);
      }
    },
    close() {
      if (frameHandle !== null) {
        cancelAnimationFrameSafe(frameHandle);
      }
      frameHandle = null;
      pendingChanges.clear();
    },
  };
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
