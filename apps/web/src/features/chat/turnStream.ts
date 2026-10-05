import type { JsonValue } from "./types";

export interface EventSourceLike {
  onopen: ((event: Event) => void) | null;
  onerror: ((event: Event) => void) | null;
  addEventListener(
    type: string,
    listener: (event: MessageEvent<string>) => void,
  ): void;
  close(): void;
}

export interface EventSourceConstructor {
  new (url: string): EventSourceLike;
}

export interface TurnStreamEvent {
  sequence: number;
  event: string;
  data: Record<string, JsonValue>;
}

export interface TurnStreamOptions {
  after?: number;
  eventSource?: EventSourceConstructor;
  onEvent?: (event: TurnStreamEvent) => void;
  onResync?: (reason: string) => void;
  onPollingRequired?: (status: number) => void;
  onError?: (error: Error) => void;
  setTimer?: (callback: () => void, delayMs: number) => number;
  clearTimer?: (handle: number) => void;
  retryBaseMs?: number;
  retryMaxMs?: number;
}

export interface TurnStreamSubscription {
  close(): void;
}

const EVENT_NAMES = [
  "queued",
  "started",
  "item",
  "completed",
  "failed",
  "interrupted",
  "cancelled",
  "stream_lost",
  "resync_required",
];

export function subscribeTurnEvents(
  threadId: string,
  turnId: string,
  options: TurnStreamOptions = {},
): TurnStreamSubscription {
  const eventSourceConstructor = options.eventSource ?? defaultEventSource;
  const setTimer = options.setTimer ?? defaultSetTimer;
  const clearTimer = options.clearTimer ?? defaultClearTimer;
  const retryBaseMs = options.retryBaseMs ?? 500;
  const retryMaxMs = options.retryMaxMs ?? 15_000;
  let lastSequence = Math.max(0, Math.floor(options.after ?? 0));
  let source: EventSourceLike | null = null;
  let retryTimer: number | null = null;
  let retryCount = 0;
  let closed = false;

  const clearRetry = () => {
    if (retryTimer === null) {
      return;
    }
    clearTimer(retryTimer);
    retryTimer = null;
  };

  const closeSource = () => {
    source?.close();
    source = null;
  };

  const close = () => {
    if (closed) {
      return;
    }
    closed = true;
    clearRetry();
    closeSource();
  };

  const handleResync = (event: MessageEvent<string>) => {
    const parsed = parseJson(event.data);
    const reason = isRecord(parsed) && typeof parsed.reason === "string"
      ? parsed.reason
      : "server_resync";
    options.onResync?.(reason);
    close();
  };

  const handleEvent = (event: MessageEvent<string>) => {
    const sequence = Number.parseInt(event.lastEventId, 10);
    if (!Number.isSafeInteger(sequence) || sequence <= lastSequence) {
      return;
    }
    const parsed = parseJson(event.data);
    if (!isRecord(parsed)) {
      options.onError?.(new Error("Invalid turn stream event payload"));
      return;
    }
    lastSequence = sequence;
    options.onEvent?.({
      sequence,
      event: event.type,
      data: parsed as Record<string, JsonValue>,
    });
  };

  const connect = () => {
    if (closed || source !== null) {
      return;
    }
    const query = new URLSearchParams({ after: String(lastSequence) });
    const url =
      `/main-agent/v1/threads/${encodeURIComponent(threadId)}/turns/` +
      `${encodeURIComponent(turnId)}/events?${query.toString()}`;
    const nextSource = new eventSourceConstructor(url);
    source = nextSource;
    nextSource.onopen = () => {
      retryCount = 0;
    };
    nextSource.onerror = (error) => {
      if (closed || source !== nextSource) {
        return;
      }
      closeSource();
      const status = (error as Event & { status?: number }).status;
      if (status === 409 || status === 401 || status === 403) {
        options.onPollingRequired?.(status);
        return;
      }
      retryCount += 1;
      const delay = Math.min(
        retryBaseMs * 2 ** (retryCount - 1),
        retryMaxMs,
      );
      retryTimer = setTimer(() => {
        retryTimer = null;
        connect();
      }, delay);
    };
    nextSource.addEventListener("resync_required", handleResync);
    for (const eventName of EVENT_NAMES) {
      if (eventName !== "resync_required") {
        nextSource.addEventListener(eventName, handleEvent);
      }
    }
  };

  connect();
  return { close };
}

const defaultEventSource: EventSourceConstructor = class {
  private readonly native: EventSource;

  constructor(url: string) {
    if (typeof globalThis.EventSource !== "function") {
      throw new Error("EventSource is unavailable in this environment");
    }
    this.native = new globalThis.EventSource(url);
  }

  get onopen() {
    return this.native.onopen;
  }

  set onopen(handler: ((event: Event) => void) | null) {
    this.native.onopen = handler;
  }

  get onerror() {
    return this.native.onerror;
  }

  set onerror(handler: ((event: Event) => void) | null) {
    this.native.onerror = handler;
  }

  addEventListener(
    type: string,
    listener: (event: MessageEvent<string>) => void,
  ) {
    this.native.addEventListener(type, listener as EventListener);
  }

  close() {
    this.native.close();
  }
};

function parseJson(value: string): unknown {
  try {
    return JSON.parse(value) as unknown;
  } catch {
    return undefined;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function defaultSetTimer(callback: () => void, delayMs: number): number {
  return globalThis.setTimeout(callback, delayMs) as unknown as number;
}

function defaultClearTimer(handle: number): void {
  globalThis.clearTimeout(handle);
}
