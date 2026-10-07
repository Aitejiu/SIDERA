import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import type { PropsWithChildren } from "react";
import { expect, it } from "vitest";

import { server } from "@/test/server";
import type { NativeTracePin } from "./nativeTrace";
import type { NativeRunEvent } from "./types";
import { useNativeTrace } from "./useNativeTrace";

const pin: NativeTracePin = {
  workspaceId: "20000000-0000-0000-0000-000000000001", workerItemId: "50000000-0000-0000-0000-000000000001",
  executionId: "60000000-0000-0000-0000-000000000001", principalId: "human-A",
  runId: "80000000-0000-0000-0000-000000000001",
  profileRef: { workspace_id: "20000000-0000-0000-0000-000000000001", profile_id: "10000000-0000-0000-0000-000000000001", revision: 1 },
};
function event(sequence: number, selected = pin): NativeRunEvent {
  return { run_id: selected.runId, sequence, kind: "assistant.message", occurred_at: "2026-10-07T00:00:00Z",
    operation_id: null, response_id: null, data: { text: `Event ${sequence}` } };
}
function page(sequences = [3, 8], hasMore = true, selected = pin) {
  return { workspace_id: selected.workspaceId, worker_item_id: selected.workerItemId,
    execution_id: selected.executionId, run_id: selected.runId, profile_ref: selected.profileRef,
    events: sequences.map((n) => event(n, selected)), next_sequence: sequences.at(-1) ?? 0, has_more: hasMore };
}
function setup(selected: NativeTracePin | null = pin) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  const wrapper = ({ children }: PropsWithChildren) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  return { client, ...renderHook(({ selectedPin }) => useNativeTrace({ pin: selectedPin }),
    { wrapper, initialProps: { selectedPin: selected } }) };
}

it("loads only two requested forward pages using cursor 8 and preserves gaps", async () => {
  const cursors: string[] = [];
  server.use(http.all("*", ({ request }) => {
    expect(request.method).toBe("GET"); expect(new URL(request.url).pathname).toContain("/native-trace");
    const after = new URL(request.url).searchParams.get("after_sequence")!; cursors.push(after);
    return HttpResponse.json(after === "8" ? page([12], false) : page());
  }));
  const { result } = setup();
  await waitFor(() => expect(result.current.events.map((e) => e.sequence)).toEqual([3, 8]));
  expect(cursors).toEqual(["0"]);
  await act(async () => { await result.current.readNext(); });
  await waitFor(() => expect(result.current.events.map((e) => e.sequence)).toEqual([3, 8, 12]));
  expect(cursors).toEqual(["0", "8"]); expect(result.current.hasNext).toBe(false);
});

it("exposes an empty terminal page without another request", async () => {
  let reads = 0;
  server.use(http.get("*/native-trace", () => { reads += 1; return HttpResponse.json(page([], false)); }));
  const { result } = setup();
  await waitFor(() => expect(result.current.loaded).toBe(true));
  expect(result.current.events).toEqual([]); expect(result.current.canReadNext).toBe(false);
  await act(async () => { await result.current.readNext(); }); expect(reads).toBe(1);
});

it("rejects a nonadvancing has_more page without a fetch loop", async () => {
  let reads = 0;
  server.use(http.get("*/native-trace", () => { reads += 1; return HttpResponse.json(page([], true)); }));
  const { result } = setup();
  await waitFor(() => expect(result.current.error?.kind).toBe("unavailable"));
  expect(result.current.events).toEqual([]); expect(reads).toBe(1);
});

it.each([[403, "permission_denied", "denied"], [503, "native_trace_unsupported", "unsupported"],
  [503, "service_unavailable", "unavailable"]])("reports safe %i/%s read state and retries the original pin", async (status, code, kind) => {
  const paths: string[] = [];
  let failed = true;
  server.use(http.get("*/native-trace", ({ request }) => {
    paths.push(new URL(request.url).pathname);
    return failed ? HttpResponse.json({ error: { code, message: "private-secret", details: {} } }, { status }) : HttpResponse.json(page([3], false));
  }));
  const { result } = setup();
  await waitFor(() => expect(result.current.error?.kind).toBe(kind));
  expect(result.current.error?.message).not.toContain("private-secret");
  failed = false;
  await act(async () => { await result.current.retry(); });
  await waitFor(() => expect(result.current.events.map((e) => e.sequence)).toEqual([3]));
  expect(paths).toHaveLength(2); expect(paths[0]).toBe(paths[1]);
});

it.each(["duplicate", "cross-Run"])("rejects a %s second page", async (invalid) => {
  server.use(http.get("*/native-trace", ({ request }) => {
    const second = invalid === "duplicate" ? page([8, 12], false)
      : { ...page([12], false), events: [{ ...event(12), run_id: pin.profileRef.profile_id }] };
    return HttpResponse.json(new URL(request.url).searchParams.get("after_sequence") === "8" ? second : page());
  }));
  const { result } = setup();
  await waitFor(() => expect(result.current.canReadNext).toBe(true));
  await act(async () => { await result.current.readNext(); });
  await waitFor(() => expect(result.current.error?.kind).toBe("unavailable"));
  expect(result.current.events).toEqual([]);
});

it("clears A pages on B/principal change and ignores late A callbacks", async () => {
  const pinB = { ...pin, executionId: "60000000-0000-0000-0000-000000000002", runId: "80000000-0000-0000-0000-000000000002", principalId: "human-B" };
  let releaseA: () => void = () => {};
  const pending = new Promise<void>((resolve) => { releaseA = resolve; });
  let started = false; let bReads = 0;
  server.use(http.get("*/native-trace", async ({ request }) => {
    const isB = new URL(request.url).pathname.includes(pinB.executionId);
    if (isB) { bReads += 1; return HttpResponse.json(page([12], false, pinB)); }
    if (new URL(request.url).searchParams.get("after_sequence") === "8") { started = true; await pending; }
    return HttpResponse.json(page());
  }));
  const { result, rerender } = setup();
  await waitFor(() => expect(result.current.canReadNext).toBe(true));
  const oldReadNext = result.current.readNext;
  let aRead: Promise<void>;
  act(() => { aRead = oldReadNext(); });
  await waitFor(() => expect(started).toBe(true));
  rerender({ selectedPin: pinB });
  expect(result.current.events).toEqual([]);
  await waitFor(() => expect(result.current.events.map((e) => e.run_id)).toEqual([pinB.runId]));
  await act(async () => { releaseA(); await aRead; await oldReadNext(); });
  expect(result.current.events.map((e) => e.sequence)).toEqual([12]); expect(bReads).toBe(1);
  rerender({ selectedPin: null }); expect(result.current.events).toEqual([]);
});
