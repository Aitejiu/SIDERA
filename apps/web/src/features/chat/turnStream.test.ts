import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  subscribeTurnEvents,
  type EventSourceLike,
  type TurnStreamEvent,
  type TurnStreamOptions,
} from "./turnStream";

class FakeEventSource implements EventSourceLike {
  static instances: FakeEventSource[] = [];
  readonly url: string;
  readonly listeners = new Map<string, Set<(event: MessageEvent<string>) => void>>();
  closed = false;
  onerror: ((event: Event) => void) | null = null;
  onopen: ((event: Event) => void) | null = null;

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: (event: MessageEvent<string>) => void) {
    const listeners = this.listeners.get(type) ?? new Set();
    listeners.add(listener);
    this.listeners.set(type, listeners);
  }

  close() {
    this.closed = true;
  }

  emitOpen() {
    this.onopen?.(new Event("open"));
  }

  emit(type: string, data: unknown, lastEventId: string) {
    const event = new MessageEvent(type, {
      data: JSON.stringify(data),
      lastEventId,
    });
    for (const listener of this.listeners.get(type) ?? []) {
      listener(event);
    }
  }

  emitResync(reason: string) {
    this.emit("resync_required", { reason }, "");
  }

  emitError(status?: number) {
    const event = new Event("error") as Event & { status: number | undefined };
    event.status = status;
    this.onerror?.(event);
  }
}

function options(overrides: Partial<TurnStreamOptions> = {}) {
  return {
    eventSource: FakeEventSource,
    retryBaseMs: 1,
    retryMaxMs: 4,
    ...overrides,
  } satisfies TurnStreamOptions;
}

describe("subscribeTurnEvents", () => {
  beforeEach(() => {
    FakeEventSource.instances = [];
    vi.useFakeTimers();
  });

  it("deduplicates event sequences and reports resync", () => {
    const onEvent = vi.fn<(event: TurnStreamEvent) => void>();
    const onResync = vi.fn<(reason: string) => void>();
    const stream = subscribeTurnEvents("thread/1", "turn/1", {
      ...options({ onEvent, onResync }),
    });
    const source = FakeEventSource.instances[0];

    source?.emit("started", { phase: "work" }, "1");
    source?.emit("started", { phase: "duplicate" }, "1");
    source?.emitResync("cursor_expired");

    expect(onEvent).toHaveBeenCalledTimes(1);
    expect(onEvent.mock.calls[0]?.[0]).toMatchObject({ sequence: 1, event: "started" });
    expect(onResync).toHaveBeenCalledWith("cursor_expired");
    expect(source?.closed).toBe(true);
    stream.close();
  });

  it("switches to polling when the gateway returns stream unavailable", () => {
    const onPollingRequired = vi.fn();
    const stream = subscribeTurnEvents("thread-1", "turn-1", {
      ...options({ onPollingRequired }),
    });
    const source = FakeEventSource.instances[0];

    source?.emitError(409);

    expect(onPollingRequired).toHaveBeenCalledTimes(1);
    expect(source?.closed).toBe(true);
    stream.close();
  });

  it("reconnects with the last accepted sequence and closes cleanly", () => {
    const stream = subscribeTurnEvents("thread-1", "turn-1", options());
    const first = FakeEventSource.instances[0];
    first?.emit("item", { text: "one" }, "7");
    first?.emitError();

    vi.advanceTimersByTime(1);

    const replacement = FakeEventSource.instances.at(-1);
    expect(replacement?.url).toContain("after=7");
    stream.close();
    expect(replacement?.closed).toBe(true);
  });

  it("closes the active EventSource on unmount", () => {
    const stream = subscribeTurnEvents("thread-1", "turn-1", options());
    const source = FakeEventSource.instances[0];

    stream.close();

    expect(source?.closed).toBe(true);
  });

  it.each([401, 403])("does not reconnect an unauthorized %s stream", (status) => {
    const denied = vi.fn();
    const stream = subscribeTurnEvents("thread-1", "turn-1", options({ onPollingRequired: denied }));
    FakeEventSource.instances[0]?.emitError(status);
    vi.advanceTimersByTime(1000);
    expect(denied).toHaveBeenCalledWith(status);
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(FakeEventSource.instances[0]?.closed).toBe(true);
    stream.close();
  });
});
