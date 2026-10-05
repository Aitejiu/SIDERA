import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import { act, render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { createMemoryRouter, RouterProvider, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { server } from "@/test/server";

import { ChatPage } from "./ChatPage";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const THREAD_ID = "thread-1";
const TURN_ID = "turn-1";
const PLANNING_REQUEST_ID = "40000000-0000-0000-0000-000000000001";
const TASK_CONTEXT_ID = "30000000-0000-0000-0000-000000000001";

class TestEventSource {
  static instances: TestEventSource[] = [];
  onopen: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  closed = false;
  private readonly listeners = new Map<string, (event: MessageEvent<string>) => void>();

  constructor(public readonly url: string) {
    TestEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: (event: MessageEvent<string>) => void): void {
    this.listeners.set(type, listener);
  }

  close(): void { this.closed = true; }

  emit(type: string, data: unknown, lastEventId: string): void {
    this.listeners.get(type)?.({
      data: JSON.stringify(data),
      lastEventId,
      type,
    } as MessageEvent<string>);
  }

  fail(status: number): void {
    this.onerror?.(Object.assign(new Event("error"), { status }));
  }
}

function turn(overrides: Record<string, unknown> = {}) {
  return {
    id: TURN_ID,
    threadId: THREAD_ID,
    status: "completed",
    createdAt: "2026-08-24T04:00:00Z",
    startedAt: "2026-08-24T04:00:01Z",
    completedAt: "2026-08-24T04:00:03Z",
    finalResponse: "服务端已恢复的回答",
    items: [
      { id: "user-item", type: "userMessage", data: { content: "之前的需求" } },
      { id: "assistant-item", type: "assistantMessage", data: { content: "服务端已恢复的回答" } },
    ],
    usage: { inputTokens: 4, outputTokens: 8 },
    error: null,
    streamAvailable: false,
    recoveryMode: null,
    interruptAvailable: false,
    interruptReasonCode: "cancel_unconfirmed",
    ...overrides,
  };
}

function thread(overrides: Record<string, unknown> = {}) {
  return {
    id: THREAD_ID,
    title: "控制面会话",
    createdAt: "2026-08-24T03:00:00Z",
    updatedAt: "2026-08-24T04:00:03Z",
    contextRefs: [],
    turns: [],
    ...overrides,
  };
}

function renderChat(path: string) {
  const router = createMemoryRouter(
    [
      {
        path: "/workspaces/:workspaceId/chat",
        element: <TestRoute />,
      },
      {
        path: "/workspaces/:workspaceId/chat/:threadId",
        element: <TestRoute />,
      },
    ],
    { initialEntries: [path] },
  );
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const rendered = render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return { router, unmount: rendered.unmount };
}

function TestRoute() {
  const location = useLocation();
  return (
    <>
      <output data-testid="location">{location.pathname}</output>
      <ChatPage />
    </>
  );
}

function useBaseHandlers(options: {
  threads?: unknown[];
  thread?: unknown;
  health?: "available" | "unavailable";
} = {}) {
  server.use(
    http.get("/main-agent/v1/health", () =>
      HttpResponse.json({
        status: "ok",
        service: "sidera-main-agent-gateway",
        provider: options.health ?? "available",
      }),
    ),
    http.get("/main-agent/v1/threads", () =>
      HttpResponse.json({ data: options.threads ?? [], nextCursor: null }),
    ),
    http.get("/main-agent/v1/threads/:threadId", () =>
      HttpResponse.json(options.thread ?? thread()),
    ),
  );
}

describe("Main Agent chat workspace", () => {
  beforeEach(() => {
    TestEventSource.instances = [];
    vi.stubGlobal("EventSource", TestEventSource);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("keeps one stream across consecutive same-turn snapshots and reconnects from its actual cursor", async () => {
    useBaseHandlers({ thread: thread({ turns: [turn({ status: "in_progress", completedAt: null, finalResponse: null, items: [], streamAvailable: true })] }) });
    renderChat(`/workspaces/${WORKSPACE_ID}/chat/${THREAD_ID}`);
    await waitFor(() => expect(TestEventSource.instances).toHaveLength(1));
    const source = TestEventSource.instances[0]!;
    source.emit("item", { item: { id: "response:assistant", type: "assistantMessage", data: { text: "one" } } }, "7");
    expect(await screen.findByText("one")).toBeInTheDocument();
    source.emit("item", { item: { id: "response:assistant", type: "assistantMessage", data: { text: "one two" } } }, "11");
    expect(await screen.findByText("one two")).toBeInTheDocument();
    expect(TestEventSource.instances).toHaveLength(1);
    expect(source.closed).toBe(false);
    source.fail(500);
    await waitFor(() => expect(TestEventSource.instances).toHaveLength(2));
    expect(TestEventSource.instances[1]!.url).toContain("after=11");
  });

  it("refreshes trusted receipts with one two-second read-only timer even while SSE is healthy", async () => {
    useBaseHandlers({ thread: thread({ turns: [turn({ status: "in_progress", completedAt: null, finalResponse: null, items: [], streamAvailable: true })] }) });
    let reads = 0, writes = 0;
    server.use(
      http.get(`/main-agent/v1/threads/${THREAD_ID}/turns/${TURN_ID}`, () => {
        reads += 1;
        return HttpResponse.json(turn({ status: "in_progress", completedAt: null, finalResponse: null, streamAvailable: true, items: [{
          id: "product-receipt:50000000-0000-0000-0000-000000000001", type: "toolCall", data: {
            receiptSource: "sidera.product.task-receipt.v1", receiptId: "50000000-0000-0000-0000-000000000001",
            name: "sidera_submit_goal", verified: true, logicalRequestId: "60000000-0000-0000-0000-000000000001",
            commandId: "70000000-0000-0000-0000-000000000001", runId: "80000000-0000-0000-0000-000000000001",
            responseId: "90000000-0000-0000-0000-000000000001", outcome: "applied", status: "submitted",
            errorCode: null, rootTaskId: TASK_CONTEXT_ID, planningRequestId: null, webUrl: `/workspaces/${WORKSPACE_ID}/tasks/${TASK_CONTEXT_ID}`,
          },
        }] }));
      }),
      http.post(`/main-agent/v1/threads/${THREAD_ID}/turns`, () => { writes += 1; return HttpResponse.error(); }),
    );
    const { unmount } = renderChat(`/workspaces/${WORKSPACE_ID}/chat/${THREAD_ID}`);
    expect(await screen.findByText("原命令已应用：submitted", {}, { timeout: 3500 })).toBeInTheDocument();
    expect(reads).toBe(1);
    expect(writes).toBe(0);
    expect(TestEventSource.instances).toHaveLength(1);
    unmount();
    expect(TestEventSource.instances[0]!.closed).toBe(true);
  });

  it.each([401, 403])("stops the stream and shared refresh after a %s view denial", async (status) => {
    useBaseHandlers({ thread: thread({ turns: [turn({ status: "in_progress", completedAt: null, finalResponse: null, items: [], streamAvailable: true })] }) });
    let reads = 0;
    server.use(http.get(`/main-agent/v1/threads/${THREAD_ID}/turns/${TURN_ID}`, () => {
      reads += 1;
      return HttpResponse.json({ error: { code: "local_session_required", message: "denied", retryable: false } }, { status });
    }));
    renderChat(`/workspaces/${WORKSPACE_ID}/chat/${THREAD_ID}`);
    await waitFor(() => expect(TestEventSource.instances).toHaveLength(1));
    TestEventSource.instances[0]!.fail(409);
    await waitFor(() => expect(reads).toBe(1));
    await new Promise((resolve) => globalThis.setTimeout(resolve, 2100));
    expect(reads).toBe(1);
    expect(TestEventSource.instances[0]!.closed).toBe(true);
    expect(TestEventSource.instances).toHaveLength(1);
  });

  it("aborts the current view read and ignores old response frames after navigation", async () => {
    useBaseHandlers({ thread: thread({ turns: [turn({ status: "in_progress", completedAt: null, finalResponse: null, items: [], streamAvailable: true })] }) });
    let readSignal: AbortSignal | undefined;
    let release: (() => void) | undefined;
    server.use(
      http.get(`/main-agent/v1/threads/${THREAD_ID}/turns/${TURN_ID}`, async ({ request }) => {
        readSignal = request.signal;
        await new Promise<void>((resolve) => { release = resolve; });
        return HttpResponse.json(turn());
      }),
      http.get("/main-agent/v1/threads/thread-new", () => HttpResponse.json(thread({ id: "thread-new", turns: [] }))),
    );
    const { router } = renderChat(`/workspaces/${WORKSPACE_ID}/chat/${THREAD_ID}`);
    try {
      await waitFor(() => expect(TestEventSource.instances).toHaveLength(1));
      const old = TestEventSource.instances[0]!;
      old.fail(409);
      await waitFor(() => expect(readSignal).toBeDefined());
      await act(async () => { await router.navigate(`/workspaces/${WORKSPACE_ID}/chat/thread-new`); });
      expect(readSignal!.aborted).toBe(true);
      expect(old.closed).toBe(true);
      old.emit("item", { item: { id: "old-response", type: "assistantMessage", data: { text: "STALE_OLD_RESPONSE" } } }, "12");
      expect(screen.queryByText("STALE_OLD_RESPONSE")).not.toBeInTheDocument();
    } finally {
      release?.();
    }
  });

  it("aborts held no-SSE reading on unmount without a later timer, stream, or write", async () => {
    useBaseHandlers({ thread: thread({ turns: [turn({ status: "queued", completedAt: null, finalResponse: null, streamAvailable: false })] }) });
    let signal: AbortSignal | undefined;
    let release: (() => void) | undefined;
    let reads = 0, writes = 0;
    server.use(
      http.get(`/main-agent/v1/threads/${THREAD_ID}/turns/${TURN_ID}`, async ({ request }) => {
        signal = request.signal;
        reads += 1;
        await new Promise<void>((resolve) => { release = resolve; });
        return HttpResponse.json(turn());
      }),
      http.post(`/main-agent/v1/threads/${THREAD_ID}/turns`, () => {
        writes += 1;
        return HttpResponse.error();
      }),
    );
    const { unmount } = renderChat(`/workspaces/${WORKSPACE_ID}/chat/${THREAD_ID}`);
    try {
      await waitFor(() => expect(signal).toBeDefined());
      unmount();
      expect(signal!.aborted).toBe(true);
      vi.useFakeTimers();
      release?.();
      await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
      expect(reads).toBe(1);
      expect(writes).toBe(0);
      expect(TestEventSource.instances).toHaveLength(0);
    } finally {
      release?.();
      vi.useRealTimers();
      unmount();
    }
  });

  it("disables one-response interruption when the native turn does not confirm it", async () => {
    useBaseHandlers({ thread: thread({ turns: [turn({ status: "in_progress", completedAt: null, finalResponse: null, streamAvailable: true, interruptAvailable: false, interruptReasonCode: "cancel_unconfirmed" })] }) });
    renderChat(`/workspaces/${WORKSPACE_ID}/chat/${THREAD_ID}`);
    const action = await screen.findByRole("button", { name: "中断当前回复" });
    expect(action).toBeDisabled();
    expect(action).toHaveAttribute("title", "cancel_unconfirmed");
  });

  it("retains the draft and original operation for a durable UNKNOWN acknowledgement without resend", async () => {
    const user = userEvent.setup();
    useBaseHandlers();
    vi.stubGlobal("crypto", { randomUUID: () => "operation-durable-unknown" });
    const operations: unknown[] = [];
    server.use(
      http.post(`/main-agent/v1/threads/${THREAD_ID}/turns`, async ({ request }) => {
        const body = await request.json() as Record<string, unknown>;
        operations.push(body.operationId);
        return HttpResponse.json(turn({ status: "queued", items: [{ id: "original:user", type: "userMessage", data: { text: body.input } }], completedAt: null, finalResponse: null, streamAvailable: false, operationId: body.operationId, operationState: "unknown" }), { status: 202 });
      }),
      http.get(`/main-agent/v1/threads/${THREAD_ID}/turns/${TURN_ID}`, () => HttpResponse.json(turn({ status: "queued", completedAt: null, finalResponse: null, streamAvailable: false, operationState: "unknown" }))),
    );
    renderChat(`/workspaces/${WORKSPACE_ID}/chat/${THREAD_ID}`);
    const composer = await screen.findByRole("textbox", { name: "给 Main Agent 的消息" });
    await user.type(composer, "original unknown text");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    await screen.findByRole("status", { name: "发送结果恢复状态" });
    expect(composer).toHaveValue("original unknown text");
    expect(operations).toEqual(["operation-durable-unknown"]);
  });

  it("retains a proven BUSY draft without polling or resend and explicitly retries the original operation", async () => {
    const user = userEvent.setup();
    useBaseHandlers();
    vi.stubGlobal("crypto", { randomUUID: () => "operation-proven-busy" });
    const bodies: unknown[] = [];
    let reads = 0;
    server.use(
      http.post(`/main-agent/v1/threads/${THREAD_ID}/turns`, async ({ request }) => {
        const body = await request.json() as Record<string, unknown>;
        bodies.push(body);
        return HttpResponse.json(turn({
          status: bodies.length === 1 ? "failed" : "completed",
          items: [{ id: "original:user", type: "userMessage", data: { text: body.input } }],
          startedAt: null, completedAt: null, finalResponse: null, streamAvailable: false,
          operationId: body.operationId, operationState: bodies.length === 1 ? "busy" : "accepted",
          error: bodies.length === 1 ? { code: "resource_busy", retryable: true } : null,
        }), { status: 202 });
      }),
      http.get(`/main-agent/v1/threads/${THREAD_ID}/turns/${TURN_ID}`, () => {
        reads += 1;
        return HttpResponse.json(turn());
      }),
    );
    const { unmount } = renderChat(`/workspaces/${WORKSPACE_ID}/chat/${THREAD_ID}`);
    try {
      const composer = await screen.findByRole("textbox", { name: "给 Main Agent 的消息" });
      await user.type(composer, "original busy text");
      await user.click(screen.getByRole("button", { name: "发送消息" }));
      await screen.findByText("未受理");
      expect(composer).toHaveValue("original busy text");
      expect(composer).toBeEnabled();
      expect(TestEventSource.instances).toHaveLength(0);
      vi.useFakeTimers();
      await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
      expect(reads).toBe(0);
      expect(bodies).toEqual([{ input: "original busy text", operationId: "operation-proven-busy" }]);
      vi.useRealTimers();
      await user.click(screen.getByRole("button", { name: "稍后重试" }));
      await waitFor(() => expect(bodies).toHaveLength(2));
      expect(bodies[1]).toEqual(bodies[0]);
    } finally {
      vi.useRealTimers();
      unmount();
    }
  });

  it("retries saved BUSY history with its exact original text and operation without local pending state", async () => {
    const user = userEvent.setup();
    const original = " original saved busy ";
    const saved = turn({
      status: "failed", startedAt: null, completedAt: null, finalResponse: null,
      items: [{ id: "saved:user", type: "userMessage", data: { text: original } }],
      operationId: "saved:busy:original", operationState: "busy",
      error: { code: "resource_busy", retryable: true },
    });
    useBaseHandlers({ thread: thread({ turns: [saved] }) });
    const bodies: unknown[] = [];
    server.use(http.post(`/main-agent/v1/threads/${THREAD_ID}/turns`, async ({ request }) => {
      bodies.push(await request.json());
      return HttpResponse.json(turn({ operationId: "saved:busy:original", operationState: "accepted" }), { status: 202 });
    }));
    renderChat(`/workspaces/${WORKSPACE_ID}/chat/${THREAD_ID}`);
    expect(await screen.findByText("未受理")).toBeInTheDocument();
    expect(screen.queryByRole("status", { name: "发送结果恢复状态" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "稍后重试" }));
    await waitFor(() => expect(bodies).toEqual([{ input: original, operationId: "saved:busy:original" }]));
    expect(TestEventSource.instances).toHaveLength(0);
  });

  it("never offers a fresh retry action for a saved UNKNOWN operation", async () => {
    useBaseHandlers({ thread: thread({ turns: [turn({
      status: "failed", completedAt: null, finalResponse: null,
      operationId: "unknown:original", operationState: "unknown",
      error: { code: "native_operation_unknown", retryable: false },
    })] }) });
    let writes = 0;
    server.use(http.post(`/main-agent/v1/threads/${THREAD_ID}/turns`, () => {
      writes += 1;
      return HttpResponse.error();
    }));
    renderChat(`/workspaces/${WORKSPACE_ID}/chat/${THREAD_ID}`);
    expect(await screen.findByText("之前的需求")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /重试/ })).not.toBeInTheDocument();
    expect(writes).toBe(0);
  });

  it("keeps an accepted queued UNKNOWN response query-only after refresh with no fresh retry or second POST", async () => {
    const pending = turn({
      status: "queued", completedAt: null, finalResponse: null, startedAt: "2026-08-24T04:00:01Z",
      operationId: "input:unknown:original", operationState: "accepted", responseId: "original-response",
      streamAvailable: false, recoveryMode: "poll",
      error: { code: "native_run_unknown", retryable: false },
    });
    useBaseHandlers({ thread: thread({ turns: [pending] }) });
    let reads = 0, writes = 0;
    server.use(
      http.get(`/main-agent/v1/threads/${THREAD_ID}/turns/${TURN_ID}`, () => {
        reads += 1;
        return HttpResponse.json(pending);
      }),
      http.post(`/main-agent/v1/threads/${THREAD_ID}/turns`, () => {
        writes += 1;
        return HttpResponse.error();
      }),
    );
    renderChat(`/workspaces/${WORKSPACE_ID}/chat/${THREAD_ID}`);
    await waitFor(() => expect(reads).toBe(1));
    expect(screen.getByRole("textbox", { name: "给 Main Agent 的消息" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: /重试/ })).not.toBeInTheDocument();
    expect(screen.getByText("结果待确认")).toBeInTheDocument();
    expect(TestEventSource.instances).toHaveLength(0);
    expect(writes).toBe(0);
  });

  it("creates an empty conversation only after an explicit click", async () => {
    const user = userEvent.setup();
    useBaseHandlers();
    server.use(
      http.post("/main-agent/v1/threads", () =>
        HttpResponse.json(thread({ title: "新对话" }), { status: 201 }),
      ),
    );

    const { router } = renderChat(`/workspaces/${WORKSPACE_ID}/chat`);

    expect(await screen.findByText("暂无对话")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "新建对话" })).not.toHaveLength(0);
    await user.click(screen.getAllByRole("button", { name: "新建对话" })[0]!);

    await waitFor(() => {
      expect(router.state.location.pathname).toBe(`/workspaces/${WORKSPACE_ID}/chat/${THREAD_ID}`);
    });
  });

  it("keeps a context draft request-free until the first send", async () => {
    const user = userEvent.setup();
    const contextRef = `planning_request:${PLANNING_REQUEST_ID}`;
    useBaseHandlers();
    let createCount = 0;
    let startCount = 0;
    server.use(
      http.post("/main-agent/v1/threads", () => {
        createCount += 1;
        return HttpResponse.json(thread({ id: "thread-context", title: "规划讨论" }), { status: 201 });
      }),
      http.post("/main-agent/v1/threads/:threadId/turns", () => {
        startCount += 1;
        return HttpResponse.json(turn({ id: "turn-context" }), { status: 202 });
      }),
    );

    const { router } = renderChat(`/workspaces/${WORKSPACE_ID}/chat?context_ref=${contextRef}`);

    const composer = await screen.findByRole("textbox", { name: "给 Main Agent 的消息" });
    expect(composer).toHaveValue(contextRef);
    expect(screen.getAllByText(contextRef)).not.toHaveLength(0);
    expect(createCount).toBe(0);
    expect(startCount).toBe(0);

    await user.click(screen.getByRole("button", { name: "发送消息" }));

    await waitFor(() => expect(router.state.location.pathname).toBe(`/workspaces/${WORKSPACE_ID}/chat/thread-context`));
    expect(createCount).toBe(1);
    expect(startCount).toBe(1);
  });

  it("keeps a context draft after thread creation when the first turn fails", async () => {
    const user = userEvent.setup();
    const contextRef = `task:${TASK_CONTEXT_ID}`;
    useBaseHandlers();
    let createCount = 0;
    server.use(
      http.post("/main-agent/v1/threads", () => {
        createCount += 1;
        return HttpResponse.json(thread({ id: "thread-created", title: "任务讨论" }), { status: 201 });
      }),
      http.post("/main-agent/v1/threads/:threadId/turns", () =>
        HttpResponse.json({ error: { code: "provider_unavailable", message: "offline", retryable: true } }, { status: 503 }),
      ),
    );

    const { router } = renderChat(`/workspaces/${WORKSPACE_ID}/chat?context_ref=${contextRef}`);
    await user.click(await screen.findByRole("button", { name: "发送消息" }));

    await waitFor(() => expect(router.state.location.pathname).toBe(`/workspaces/${WORKSPACE_ID}/chat/thread-created`));
    expect(createCount).toBe(1);
    expect(screen.getByRole("textbox", { name: "给 Main Agent 的消息" })).toHaveValue(contextRef);
  });

  it("restores server history, sends only on click, and disables unconfirmed response interruption", async () => {
    const user = userEvent.setup();
    useBaseHandlers({ thread: thread({ turns: [turn()] }) });
    let startCount = 0;
    server.use(
      http.post("/main-agent/v1/threads/:threadId/turns", async () => {
        startCount += 1;
        return HttpResponse.json(
          turn({
            id: "turn-2",
            status: "in_progress",
            completedAt: null,
            finalResponse: null,
            items: [],
            streamAvailable: true,
          }),
          { status: 202 },
        );
      }),
      http.get("/main-agent/v1/threads/:threadId/turns/turn-2", () =>
        HttpResponse.json(turn({ id: "turn-2", status: "completed" })),
      ),
    );

    renderChat(`/workspaces/${WORKSPACE_ID}/chat/${THREAD_ID}`);

    expect(await screen.findByText("服务端已恢复的回答")).toBeInTheDocument();
    const composer = screen.getByRole("textbox", { name: "给 Main Agent 的消息" });
    await user.type(composer, "新增需求");
    expect(startCount).toBe(0);
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    await waitFor(() => expect(startCount).toBe(1));
    expect(screen.getByRole("textbox", { name: "给 Main Agent 的消息" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "中断当前回复" })).toBeDisabled();
  });

  it("retries a message when the Gateway reports a transient busy thread", async () => {
    const user = userEvent.setup();
    useBaseHandlers();
    let startCount = 0;
    server.use(
      http.post("/main-agent/v1/threads/:threadId/turns", () => {
        startCount += 1;
        if (startCount === 1) {
          return HttpResponse.json(
            { error: { code: "thread_busy", message: "busy", retryable: true } },
            { status: 409 },
          );
        }
        return HttpResponse.json(turn({ id: "turn-2" }), { status: 202 });
      }),
    );

    renderChat(`/workspaces/${WORKSPACE_ID}/chat/${THREAD_ID}`);
    await user.type(await screen.findByRole("textbox", { name: "给 Main Agent 的消息" }), "重试忙碌线程");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    await waitFor(() => expect(startCount).toBe(2));
    expect(screen.queryByText("消息未发送，输入内容已保留。请检查 Gateway 后重试。")).not.toBeInTheDocument();
  });

  it("reuses the same operation id after a lost start response", async () => {
    const user = userEvent.setup();
    useBaseHandlers();
    const randomUUID = vi.fn()
      .mockReturnValueOnce("operation-lost-1")
      .mockReturnValueOnce("operation-lost-2");
    vi.stubGlobal("crypto", { randomUUID });
    const operationIds: unknown[] = [];
    let startCount = 0;
    server.use(
      http.post("/main-agent/v1/threads/:threadId/turns", async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        operationIds.push(body.operationId);
        startCount += 1;
        if (startCount === 1) {
          return HttpResponse.error();
        }
        return HttpResponse.json(turn({ id: "turn-recovered" }), { status: 202 });
      }),
    );

    renderChat(`/workspaces/${WORKSPACE_ID}/chat/${THREAD_ID}`);
    const composer = await screen.findByRole("textbox", { name: "给 Main Agent 的消息" });
    await user.type(composer, "响应丢失后继续");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    expect(await screen.findByRole("status", { name: "发送结果恢复状态" })).toHaveTextContent(
      "结果待确认",
    );
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    await waitFor(() => expect(screen.getByText("服务端已恢复的回答")).toBeInTheDocument());
    expect(operationIds).toEqual(["operation-lost-1", "operation-lost-1"]);
    expect(randomUUID).toHaveBeenCalledTimes(1);
  });

  it("keeps the operation id when text changes, then requires an explicit new send", async () => {
    const user = userEvent.setup();
    useBaseHandlers();
    const randomUUID = vi.fn()
      .mockReturnValueOnce("operation-conflict-1")
      .mockReturnValueOnce("operation-conflict-2");
    vi.stubGlobal("crypto", { randomUUID });
    const operationIds: unknown[] = [];
    let startCount = 0;
    server.use(
      http.post("/main-agent/v1/threads/:threadId/turns", async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        operationIds.push(body.operationId);
        startCount += 1;
        if (startCount === 1) return HttpResponse.error();
        if (startCount === 2) {
          return HttpResponse.json(
            { error: { code: "operation_identity_conflict", message: "conflict", retryable: false } },
            { status: 409 },
          );
        }
        return HttpResponse.json(turn({ id: "turn-new-operation" }), { status: 202 });
      }),
    );

    renderChat(`/workspaces/${WORKSPACE_ID}/chat/${THREAD_ID}`);
    const composer = await screen.findByRole("textbox", { name: "给 Main Agent 的消息" });
    await user.type(composer, "第一次内容");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    await screen.findByRole("status", { name: "发送结果恢复状态" });

    await user.clear(composer);
    await user.type(composer, "修改后的内容");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    await waitFor(() => expect(screen.getByText("原发送结果仍待确认，修改后的内容未发送。请先结束这次恢复，再发起新的发送。")).toBeInTheDocument());
    expect(operationIds).toEqual(["operation-conflict-1", "operation-conflict-1"]);

    await user.click(screen.getByRole("button", { name: "开始新的发送" }));
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    await waitFor(() => expect(screen.getByText("服务端已恢复的回答")).toBeInTheDocument());
    expect(operationIds).toEqual([
      "operation-conflict-1",
      "operation-conflict-1",
      "operation-conflict-2",
    ]);
    expect(randomUUID).toHaveBeenCalledTimes(2);
  });

  it("never dispatches interruption without current native confirmation", async () => {
    const user = userEvent.setup();
    useBaseHandlers({
      thread: thread({
        turns: [turn({ status: "in_progress", completedAt: null, finalResponse: null, streamAvailable: true })],
      }),
    });
    let interruptCount = 0;
    server.use(
      http.post("/main-agent/v1/threads/:threadId/turns/:turnId/interrupt", () => {
        interruptCount += 1;
        return HttpResponse.json(turn({ status: "interrupted", streamAvailable: false }));
      }),
    );

    renderChat(`/workspaces/${WORKSPACE_ID}/chat/${THREAD_ID}`);
    const action = await screen.findByRole("button", { name: "中断当前回复" });
    expect(action).toBeDisabled();
    await user.click(action);

    expect(interruptCount).toBe(0);
    expect(screen.queryByText("已中断")).not.toBeInTheDocument();
  });

  it("polls the same turn when the event stream is unavailable", async () => {
    const user = userEvent.setup();
    useBaseHandlers();
    let startCount = 0;
    let readCount = 0;
    server.use(
      http.post("/main-agent/v1/threads/:threadId/turns", () => {
        startCount += 1;
        return HttpResponse.json(
          turn({ id: "turn-2", status: "in_progress", completedAt: null, finalResponse: null, items: [], streamAvailable: true }),
          { status: 202 },
        );
      }),
      http.get("/main-agent/v1/threads/:threadId/turns/turn-2", () => {
        readCount += 1;
        return HttpResponse.json(turn({ id: "turn-2", status: "completed" }));
      }),
    );

    renderChat(`/workspaces/${WORKSPACE_ID}/chat/${THREAD_ID}`);
    await user.type(await screen.findByRole("textbox", { name: "给 Main Agent 的消息" }), "轮询恢复");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    await waitFor(() => expect(TestEventSource.instances.length).toBeGreaterThan(0));
    TestEventSource.instances.at(-1)?.fail(409);

    await waitFor(() => expect(screen.getByText("服务端已恢复的回答")).toBeInTheDocument());
    expect(startCount).toBe(1);
    expect(readCount).toBeGreaterThan(0);
  });

  it("explains polling recovery without offering an automatic replay", async () => {
    useBaseHandlers({
      thread: thread({
        turns: [turn({
          status: "in_progress",
          completedAt: null,
          finalResponse: null,
          streamAvailable: false,
          recoveryMode: "poll",
        })],
      }),
    });

    renderChat(`/workspaces/${WORKSPACE_ID}/chat/${THREAD_ID}`);

    expect(await screen.findByRole("status", { name: "对话恢复状态" })).toHaveTextContent(
      "正在恢复原回合状态",
    );
    expect(screen.getByRole("status", { name: "对话恢复状态" })).toHaveTextContent(
      "不会自动重发",
    );
    expect(screen.queryByRole("button", { name: "重试" })).not.toBeInTheDocument();
  });

  it("keeps the original input for an explicit retry after failure", async () => {
    const user = userEvent.setup();
    useBaseHandlers();
    let startCount = 0;
    server.use(
      http.post("/main-agent/v1/threads/:threadId/turns", () => {
        startCount += 1;
        return HttpResponse.json(
          turn({
            id: `turn-${startCount}`,
            status: "failed",
            completedAt: "2026-08-24T04:00:03Z",
            finalResponse: null,
            items: [],
            error: { code: "provider_unavailable" },
            streamAvailable: false,
          }),
          { status: 202 },
        );
      }),
    );

    renderChat(`/workspaces/${WORKSPACE_ID}/chat/${THREAD_ID}`);
    await user.type(await screen.findByRole("textbox", { name: "给 Main Agent 的消息" }), "请重试这条需求");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    await user.click(await screen.findByRole("button", { name: "重试" }));

    await waitFor(() => expect(startCount).toBe(2));
  });

  it("keeps task navigation available while the Gateway is offline", async () => {
    useBaseHandlers({ health: "unavailable" });
    server.use(
      http.get("/main-agent/v1/threads", () =>
        HttpResponse.json(
          { error: { code: "provider_unavailable", message: "offline", retryable: true } },
          { status: 503 },
        ),
      ),
    );

    renderChat(`/workspaces/${WORKSPACE_ID}/chat`);

    expect(await screen.findByText("Main Agent 暂不可用")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "返回任务" })).toHaveAttribute(
      "href",
      `/workspaces/${WORKSPACE_ID}/tasks`,
    );
  });

  it("shows the offline state when the Gateway health request fails", async () => {
    useBaseHandlers();
    server.use(
      http.get("/main-agent/v1/health", () => HttpResponse.error()),
    );

    renderChat(`/workspaces/${WORKSPACE_ID}/chat`);

    expect(await screen.findByText("Main Agent 暂不可用")).toBeInTheDocument();
    expect(screen.getByText("Gateway offline")).toBeInTheDocument();
    expect(screen.getByText("任务控制面仍可继续查看。")).toBeInTheDocument();
  });

  it("keeps restored history visible while the Gateway is unavailable", async () => {
    useBaseHandlers({
      health: "unavailable",
      thread: thread({ turns: [turn()] }),
    });

    renderChat(`/workspaces/${WORKSPACE_ID}/chat/${THREAD_ID}`);

    expect(await screen.findByText("服务端已恢复的回答")).toBeInTheDocument();
    expect(screen.getByRole("status", { name: "历史只读提示" })).toHaveTextContent(
      "当前显示已恢复历史",
    );
    expect(screen.getByRole("textbox", { name: "给 Main Agent 的消息" })).toBeDisabled();
  });

  it("renders a tool result and resolves its task reference in the context rail", async () => {
    const taskId = "30000000-0000-0000-0000-000000000099";
    useBaseHandlers({
      thread: thread({
        turns: [turn({
          items: [
            { id: "tool-user", type: "userMessage", data: { content: "查任务" } },
            {
              id: "product-receipt:50000000-0000-0000-0000-000000000001",
              type: "toolCall",
              data: {
                receiptSource: "sidera.product.task-receipt.v1", receiptId: "50000000-0000-0000-0000-000000000001",
                name: "sidera_submit_goal", verified: true, logicalRequestId: "60000000-0000-0000-0000-000000000001",
                commandId: "70000000-0000-0000-0000-000000000001", runId: "80000000-0000-0000-0000-000000000001",
                responseId: "90000000-0000-0000-0000-000000000001", outcome: "applied", status: "submitted",
                errorCode: null, rootTaskId: taskId, planningRequestId: null, webUrl: `/workspaces/${WORKSPACE_ID}/tasks/${taskId}`,
              },
            },
          ],
        })],
      }),
    });
    server.use(
      http.get(`/api/v1/tasks/${taskId}/graph`, () =>
        HttpResponse.json({
          overview: {
            title: "控制面任务",
            completed_subtask_count: 2,
            planned_subtask_count: 4,
          },
        }),
      ),
    );

    renderChat(`/workspaces/${WORKSPACE_ID}/chat/${THREAD_ID}`);

    expect(await screen.findByText("原命令已应用：submitted")).toBeInTheDocument();
    expect(await screen.findByText("控制面任务")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "打开任务" })).toHaveAttribute(
      "href",
      `/workspaces/${WORKSPACE_ID}/tasks/${taskId}`,
    );
  });
});
