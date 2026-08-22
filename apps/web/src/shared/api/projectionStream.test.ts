import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  createProjectionStream,
  type ProjectionChange,
  type ProjectionStreamOptions,
} from "./projectionStream";

class FakeEventSource {
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

  removeEventListener(type: string, listener: (event: MessageEvent<string>) => void) {
    this.listeners.get(type)?.delete(listener);
  }

  close() {
    this.closed = true;
  }

  emitOpen() {
    this.onopen?.(new Event("open"));
  }

  emitChange(change: ProjectionChange, lastEventId = String(change.cursor)) {
    const event = new MessageEvent("projection.changed", {
      data: JSON.stringify(change),
      lastEventId,
    });
    for (const listener of this.listeners.get("projection.changed") ?? []) {
      listener(event);
    }
  }

  emitResync(reason: string) {
    const event = new MessageEvent("resync_required", {
      data: JSON.stringify({ reason }),
    });
    for (const listener of this.listeners.get("resync_required") ?? []) {
      listener(event);
    }
  }

  emitError() {
    this.onerror?.(new Event("error"));
  }
}

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";

function change(cursor: number, projectionType = "task_overview"): ProjectionChange {
  return {
    workspace_id: WORKSPACE_ID,
    cursor,
    schema_version: 1,
    projection_type: projectionType,
    resource_id: "30000000-0000-0000-0000-000000000001",
    change_type: "updated",
    resource_version: cursor,
    occurred_at: "2026-08-21T08:00:00Z",
    correlation_id: "50000000-0000-0000-0000-000000000001",
  };
}

function createOptions(overrides: Partial<ProjectionStreamOptions> = {}) {
  return {
    eventSource: FakeEventSource,
    retryBaseMs: 1,
    retryMaxMs: 4,
    requestFrame: (callback: FrameRequestCallback) => window.setTimeout(callback, 0),
    cancelFrame: (handle: number) => window.clearTimeout(handle),
    ...overrides,
  } satisfies ProjectionStreamOptions;
}

describe("ProjectionStream", () => {
  beforeEach(() => {
    FakeEventSource.instances = [];
    window.localStorage.clear();
    vi.useFakeTimers();
  });

  it("starts from the HTTP snapshot cursor and persists contiguous changes", () => {
    const onChange = vi.fn();
    const stream = createProjectionStream(WORKSPACE_ID, createOptions({ onChange }));
    const unsubscribe = stream.subscribe(() => undefined);

    expect(stream.getSnapshot().status).toBe("connecting");
    expect(FakeEventSource.instances).toHaveLength(0);
    stream.seedSnapshotCursor(7);

    const source = FakeEventSource.instances[0];
    expect(source?.url).toContain("after=7");
    source?.emitOpen();
    expect(stream.getSnapshot().status).toBe("live");
    source?.emitChange(change(8));
    source?.emitChange(change(9));
    vi.runAllTimers();

    expect(stream.getSnapshot().cursor).toBe(9);
    expect(window.localStorage.getItem(`sidera:projection-cursor:${WORKSPACE_ID}`)).toBe("9");
    expect(onChange).toHaveBeenCalledTimes(2);
    unsubscribe();
    stream.close();
  });

  it("ignores duplicate cursors and resyncs on gaps or schema mismatches", () => {
    const onResyncRequired = vi.fn();
    const stream = createProjectionStream(
      WORKSPACE_ID,
      createOptions({ onResyncRequired }),
    );
    stream.subscribe(() => undefined);
    stream.seedSnapshotCursor(7);
    const source = FakeEventSource.instances[0];
    source?.emitOpen();
    source?.emitChange(change(8));
    source?.emitChange(change(8));
    expect(stream.getSnapshot().cursor).toBe(8);
    expect(onResyncRequired).not.toHaveBeenCalled();

    source?.emitChange(change(10));
    expect(stream.getSnapshot().status).toBe("resyncing");
    expect(onResyncRequired).toHaveBeenCalledWith("cursor_gap");
    expect(window.localStorage.getItem(`sidera:projection-cursor:${WORKSPACE_ID}`)).toBeNull();

    stream.seedSnapshotCursor(10);
    const replacement = FakeEventSource.instances.at(-1);
    replacement?.emitOpen();
    replacement?.emitChange({ ...change(11), schema_version: 99 });
    expect(stream.getSnapshot().status).toBe("resyncing");
    expect(onResyncRequired).toHaveBeenCalledWith("schema_mismatch");
    stream.close();
  });

  it("reports stale and offline states while retrying a closed native source", () => {
    const stream = createProjectionStream(
      WORKSPACE_ID,
      createOptions({ maxRetriesBeforeOffline: 2 }),
    );
    stream.subscribe(() => undefined);
    stream.seedSnapshotCursor(0);
    const source = FakeEventSource.instances[0];
    source?.emitOpen();
    source?.emitError();
    expect(source?.closed).toBe(true);
    expect(stream.getSnapshot().status).toBe("stale");
    vi.advanceTimersByTime(1);
    expect(FakeEventSource.instances).toHaveLength(2);
    FakeEventSource.instances[1]?.emitError();
    vi.advanceTimersByTime(2);
    expect(stream.getSnapshot().status).toBe("offline");
    stream.close();
  });
});
