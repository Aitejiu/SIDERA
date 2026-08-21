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
  notificationFromChange,
  readLastNotifiedCursor,
  sendBrowserNotification,
  writeLastNotifiedCursor,
  type WorkspaceNotification,
} from "@/features/attention/notifications";

interface WorkspaceStreamContextValue {
  streamState: ProjectionStreamSnapshot;
  seedSnapshotCursor: (cursor: number) => void;
  notices: readonly WorkspaceNotification[];
  dismissNotice: (id: string) => void;
}

const WorkspaceStreamContext = createContext<WorkspaceStreamContextValue | null>(null);

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
      invalidationQueue.enqueue(change);
      const notification = notificationFromChange(workspaceId, change);
      if (notification === null) {
        return;
      }
      if (change.cursor <= readLastNotifiedCursor(workspaceId)) {
        return;
      }
      writeLastNotifiedCursor(workspaceId, change.cursor);
      if (browserNotificationsEnabled(workspaceId)) {
        sendBrowserNotification(notification);
      }
      setNotices((current) => appendNotification(current, notification));
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

function matchesProjectionQuery(
  queryKey: readonly unknown[],
  workspaceId: string,
  change: ProjectionChange,
): boolean {
  if (!queryKey.includes(workspaceId)) {
    return false;
  }
  if (change.projection_type === "task_first" || change.projection_type === "task_overview") {
    if (queryKey[0] === "task-overviews") {
      return true;
    }
    return queryKey[0] === "task-overview" &&
      (change.resource_id === null || queryKey[2] === change.resource_id);
  }
  if (change.projection_type === "worker_trace") {
    if (queryKey[0] === "worker-traces") {
      return true;
    }
    return queryKey[0] === "worker-trace" &&
      (change.resource_id === null || queryKey[2] === change.resource_id);
  }
  return false;
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
