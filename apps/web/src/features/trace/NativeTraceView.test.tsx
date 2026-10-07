import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { expect, it } from "vitest";

import { server } from "@/test/server";
import { NativeTraceView } from "./NativeTraceView";
import type { NativeTracePin } from "./nativeTrace";

const pin: NativeTracePin = {
  workspaceId: "20000000-0000-0000-0000-000000000001", workerItemId: "50000000-0000-0000-0000-000000000001",
  executionId: "60000000-0000-0000-0000-000000000001", principalId: "human-A", runId: "80000000-0000-0000-0000-000000000001",
  profileRef: { workspace_id: "20000000-0000-0000-0000-000000000001", profile_id: "10000000-0000-0000-0000-000000000001", revision: 1 },
};
function page(sequences: number[] = [3, 8], hasMore = false) {
  return { workspace_id: pin.workspaceId, worker_item_id: pin.workerItemId, execution_id: pin.executionId,
    run_id: pin.runId, profile_ref: pin.profileRef, next_sequence: sequences.at(-1) ?? 0, has_more: hasMore,
    events: sequences.map((sequence) => ({ run_id: pin.runId, sequence, kind: `event.${sequence}`,
      occurred_at: "2026-10-07T00:00:00Z", operation_id: null, response_id: null, data: { text: `Native ${sequence}` } })) };
}
function setup() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(<QueryClientProvider client={client}><NativeTraceView pin={pin} executionStatus="completed" /></QueryClientProvider>);
}

it("shows original pins and Execution status separately with no invented measurements", async () => {
  server.use(http.get("*/native-trace", () => HttpResponse.json(page())));
  setup();
  expect(await screen.findByRole("heading", { name: "event.8" })).toBeVisible();
  expect(screen.getByText("Execution 状态：completed")).toBeVisible();
  expect(screen.getByText("Profile R1")).toBeVisible(); expect(screen.getByText("耗时未提供")).toBeVisible();
  expect(screen.getByText("用量未提供")).toBeVisible();
  await userEvent.setup().click(screen.getByText("原始执行", { selector: "summary" }));
  expect(screen.getByText(pin.runId)).toBeVisible(); expect(screen.getByText(pin.executionId)).toBeVisible();
  expect(screen.queryByText(/0 ms|0 tokens|Checkpoint/)).not.toBeInTheDocument();
});

it("loads a next native page only on request and keeps loaded filters independent", async () => {
  const cursors: string[] = [];
  server.use(http.get("*/native-trace", ({ request }) => {
    const cursor = new URL(request.url).searchParams.get("after_sequence")!; cursors.push(cursor);
    return HttpResponse.json(cursor === "8" ? page([12], false) : page([3, 8], true));
  }));
  const user = userEvent.setup(); setup(); await screen.findByRole("heading", { name: "event.8" });
  await user.type(screen.getByRole("searchbox"), "Native"); expect(cursors).toEqual(["0"]);
  await user.click(screen.getByRole("button", { name: "后续事件" }));
  await screen.findByRole("button", { name: /event.12/ }); expect(cursors).toEqual(["0", "8"]);
  expect(screen.getByRole("heading", { name: "event.8" })).toBeVisible();
});

it.each([
  [403, "permission_denied", "无权读取 Agent Trace"],
  [503, "native_trace_unsupported", "此执行不支持 Agent Trace"],
  [503, "service_unavailable", "Agent Trace 暂不可用"],
])("shows truthful safe %i/%s state and retries the same original path", async (status, code, text) => {
  let failed = true; const paths: string[] = [];
  server.use(http.get("*/native-trace", ({ request }) => {
    paths.push(new URL(request.url).pathname);
    return failed ? HttpResponse.json({ error: { code, message: "private-native-handle-secret", details: {} } }, { status })
      : HttpResponse.json(page());
  }));
  const user = userEvent.setup(); setup();
  expect(await screen.findByRole("alert")).toHaveTextContent(text);
  expect(document.body).not.toHaveTextContent("private-native-handle-secret");
  failed = false; await user.click(screen.getByRole("button", { name: "重试" }));
  await screen.findByRole("heading", { name: "event.8" });
  expect(paths).toHaveLength(2); expect(paths[1]).toBe(paths[0]);
});

it("separates loading from an empty successful trace", async () => {
  let release: () => void = () => {};
  const pending = new Promise<void>((resolve) => { release = resolve; });
  server.use(http.get("*/native-trace", async () => { await pending; return HttpResponse.json(page([])); }));
  setup(); expect(screen.getByRole("status")).toHaveTextContent("读取 Agent Trace");
  release(); await waitFor(() => expect(screen.getByText("暂无轨迹记录")).toBeVisible());
  expect(screen.queryByText("读取 Agent Trace")).not.toBeInTheDocument();
});
