export type ProjectionStreamStatus =
  | "connecting"
  | "live"
  | "stale"
  | "resyncing"
  | "offline";

export interface ProjectionChange {
  workspace_id: string;
  cursor: number;
  schema_version: number;
  projection_type: string;
  resource_id: string | null;
  change_type: string;
  resource_version: number | null;
  occurred_at: string;
  correlation_id: string;
}

export interface ProjectionStreamSnapshot {
  status: ProjectionStreamStatus;
  cursor: number;
  lastEventAt: number | null;
  error: string | null;
}

interface EventSourceLike {
  onopen: ((event: Event) => void) | null;
  onerror: ((event: Event) => void) | null;
  addEventListener(
    type: string,
    listener: (event: MessageEvent<string>) => void,
  ): void;
  removeEventListener(
    type: string,
    listener: (event: MessageEvent<string>) => void,
  ): void;
  close(): void;
}

interface EventSourceConstructor {
  new (url: string): EventSourceLike;
}

export interface ProjectionStreamOptions {
  eventSource?: EventSourceConstructor;
  onChange?: (change: ProjectionChange) => void;
  onResyncRequired?: (reason: string) => void;
  storage?: Storage;
  now?: () => number;
  requestFrame?: (callback: () => void) => number;
  cancelFrame?: (handle: number) => void;
  setTimer?: (callback: () => void, delayMs: number) => number;
  clearTimer?: (handle: number) => void;
  retryBaseMs?: number;
  retryMaxMs?: number;
  maxRetriesBeforeOffline?: number;
}

export interface ProjectionStream {
  subscribe(listener: () => void): () => void;
  getSnapshot(): ProjectionStreamSnapshot;
  seedSnapshotCursor(cursor: number): void;
  close(): void;
}

const STREAM_SCHEMA_VERSION = 1;
const DEFAULT_RETRY_BASE_MS = 500;
const DEFAULT_RETRY_MAX_MS = 15_000;
const DEFAULT_MAX_RETRIES_BEFORE_OFFLINE = 5;

export function projectionCursorStorageKey(workspaceId: string): string {
  return `sidera:projection-cursor:${workspaceId}`;
}

export function createProjectionStream(
  workspaceId: string,
  options: ProjectionStreamOptions = {},
): ProjectionStream {
  const listeners = new Set<() => void>();
  const eventSourceConstructor: EventSourceConstructor =
    options.eventSource ?? defaultEventSource;
  const storage = options.storage ?? safeLocalStorage();
  const now = options.now ?? (() => Date.now());
  const setTimer = options.setTimer ?? defaultSetTimer;
  const clearTimer = options.clearTimer ?? defaultClearTimer;
  const retryBaseMs = options.retryBaseMs ?? DEFAULT_RETRY_BASE_MS;
  const retryMaxMs = options.retryMaxMs ?? DEFAULT_RETRY_MAX_MS;
  const maxRetriesBeforeOffline =
    options.maxRetriesBeforeOffline ?? DEFAULT_MAX_RETRIES_BEFORE_OFFLINE;

  let snapshot: ProjectionStreamSnapshot = {
    status: "connecting",
    cursor: 0,
    lastEventAt: null,
    error: null,
  };
  let source: EventSourceLike | null = null;
  let retryTimer: number | null = null;
  let retryCount = 0;
  let waitingForSnapshot = true;
  let closed = false;

  const notify = () => {
    for (const listener of listeners) {
      listener();
    }
  };

  const setSnapshot = (next: ProjectionStreamSnapshot) => {
    snapshot = next;
    notify();
  };

  const closeSource = () => {
    if (source === null) {
      return;
    }
    source.close();
    source = null;
  };

  const clearRetryTimer = () => {
    if (retryTimer === null) {
      return;
    }
    clearTimer(retryTimer);
    retryTimer = null;
  };

  const resync = (reason: string) => {
    if (closed) {
      return;
    }
    clearRetryTimer();
    closeSource();
    waitingForSnapshot = true;
    retryCount = 0;
    storage?.removeItem(projectionCursorStorageKey(workspaceId));
    setSnapshot({
      ...snapshot,
      status: "resyncing",
      cursor: 0,
      error: reason,
    });
    options.onResyncRequired?.(reason);
  };

  const handleChange = (event: MessageEvent<string>) => {
    const parsed = parseJson(event.data);
    if (!isProjectionChange(parsed)) {
      resync("invalid_event");
      return;
    }
    if (parsed.workspace_id !== workspaceId) {
      resync("workspace_mismatch");
      return;
    }
    if (parsed.schema_version !== STREAM_SCHEMA_VERSION) {
      resync("schema_mismatch");
      return;
    }

    const eventCursor = parseCursor(event.lastEventId) ?? parsed.cursor;
    if (eventCursor <= snapshot.cursor) {
      return;
    }
    if (eventCursor !== snapshot.cursor + 1 || parsed.cursor !== eventCursor) {
      resync("cursor_gap");
      return;
    }

    storage?.setItem(projectionCursorStorageKey(workspaceId), String(eventCursor));
    setSnapshot({
      status: "live",
      cursor: eventCursor,
      lastEventAt: now(),
      error: null,
    });
    options.onChange?.(parsed);
  };

  const handleResyncEvent = (event: MessageEvent<string>) => {
    const parsed = parseJson(event.data);
    const reason =
      isRecord(parsed) && typeof parsed.reason === "string"
        ? parsed.reason
        : "server_resync";
    resync(reason);
  };

  const connect = () => {
    if (closed || waitingForSnapshot || source !== null) {
      return;
    }
    const url = `/api/v1/workspaces/${encodeURIComponent(
      workspaceId,
    )}/projection-changes?after=${snapshot.cursor}&schema_version=${STREAM_SCHEMA_VERSION}`;
    const nextSource = new eventSourceConstructor(url);
    source = nextSource;
    nextSource.onopen = () => {
      retryCount = 0;
      setSnapshot({ ...snapshot, status: "live", error: null });
    };
    nextSource.onerror = () => {
      if (closed || source !== nextSource) {
        return;
      }
      closeSource();
      retryCount += 1;
      if (retryCount >= maxRetriesBeforeOffline) {
        setSnapshot({ ...snapshot, status: "offline", error: "stream_offline" });
        return;
      }
      const delay = Math.min(
        retryBaseMs * 2 ** (retryCount - 1),
        retryMaxMs,
      );
      setSnapshot({ ...snapshot, status: "stale", error: "stream_stale" });
      retryTimer = setTimer(() => {
        retryTimer = null;
        if (closed || waitingForSnapshot) {
          return;
        }
        setSnapshot({ ...snapshot, status: "connecting", error: null });
        connect();
      }, delay);
    };
    nextSource.addEventListener("projection.changed", handleChange);
    nextSource.addEventListener("resync_required", handleResyncEvent);
  };

  return {
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    getSnapshot() {
      return snapshot;
    },
    seedSnapshotCursor(cursor) {
      if (closed || (!waitingForSnapshot && source !== null)) {
        return;
      }
      clearRetryTimer();
      closeSource();
      retryCount = 0;
      waitingForSnapshot = false;
      setSnapshot({
        ...snapshot,
        status: "connecting",
        cursor: Math.max(0, cursor),
        error: null,
      });
      connect();
    },
    close() {
      if (closed) {
        return;
      }
      closed = true;
      clearRetryTimer();
      closeSource();
      listeners.clear();
    },
  };
}

const defaultEventSource: EventSourceConstructor = class {
  private readonly native: EventSource;

  constructor(url: string) {
    const NativeEventSource = globalThis.EventSource;
    if (typeof NativeEventSource !== "function") {
      throw new Error("EventSource is unavailable in this environment");
    }
    this.native = new NativeEventSource(url);
  }

  get onopen(): ((event: Event) => void) | null {
    return this.native.onopen;
  }

  set onopen(handler: ((event: Event) => void) | null) {
    this.native.onopen = handler;
  }

  get onerror(): ((event: Event) => void) | null {
    return this.native.onerror;
  }

  set onerror(handler: ((event: Event) => void) | null) {
    this.native.onerror = handler;
  }

  addEventListener(type: string, listener: (event: MessageEvent<string>) => void) {
    this.native.addEventListener(type, listener as EventListener);
  }

  removeEventListener(type: string, listener: (event: MessageEvent<string>) => void) {
    this.native.removeEventListener(type, listener as EventListener);
  }

  close() {
    this.native.close();
  }
};

function defaultSetTimer(callback: () => void, delayMs: number): number {
  return globalThis.setTimeout(callback, delayMs) as unknown as number;
}

function defaultClearTimer(handle: number): void {
  globalThis.clearTimeout(handle);
}

function safeLocalStorage(): Storage | undefined {
  try {
    return globalThis.localStorage;
  } catch {
    return undefined;
  }
}

function parseJson(value: string): unknown {
  try {
    return JSON.parse(value) as unknown;
  } catch {
    return undefined;
  }
}

function parseCursor(value: string): number | null {
  if (!value.trim()) {
    return null;
  }
  const cursor = Number(value);
  return Number.isSafeInteger(cursor) && cursor >= 0 ? cursor : null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isProjectionChange(value: unknown): value is ProjectionChange {
  return (
    isRecord(value) &&
    typeof value.workspace_id === "string" &&
    typeof value.cursor === "number" &&
    Number.isSafeInteger(value.cursor) &&
    typeof value.schema_version === "number" &&
    typeof value.projection_type === "string" &&
    (typeof value.resource_id === "string" || value.resource_id === null) &&
    typeof value.change_type === "string" &&
    (typeof value.resource_version === "number" || value.resource_version === null) &&
    typeof value.occurred_at === "string" &&
    typeof value.correlation_id === "string"
  );
}
