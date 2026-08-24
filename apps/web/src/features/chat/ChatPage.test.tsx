import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import { render, screen, waitFor } from "@testing-library/react";
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
  private readonly listeners = new Map<string, (event: MessageEvent<string>) => void>();

  constructor(public readonly url: string) {
    TestEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: (event: MessageEvent<string>) => void): void {
    this.listeners.set(type, listener);
  }

  close(): void {}

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
  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return { router };
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

  it("restores server history, sends only on click, and exposes the active stop action", async () => {
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
    expect(screen.getByRole("button", { name: "停止当前对话" })).toBeEnabled();
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

  it("interrupts only the current active turn", async () => {
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
    await user.click(await screen.findByRole("button", { name: "停止当前对话" }));

    await waitFor(() => expect(interruptCount).toBe(1));
    expect(screen.getByText("已中断")).toBeInTheDocument();
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

  it("renders a tool result and resolves its task reference in the context rail", async () => {
    const taskId = "30000000-0000-0000-0000-000000000099";
    useBaseHandlers({
      thread: thread({
        turns: [turn({
          items: [
            { id: "tool-user", type: "userMessage", data: { content: "查任务" } },
            {
              id: "tool-result",
              type: "toolCall",
              data: {
                name: "sidera_get_task_status",
                resultPreview: JSON.stringify({
                  outcome: "ok",
                  summary: "Task progress loaded.",
                  resource_ref: `task:${taskId}`,
                  web_url: `/workspaces/${WORKSPACE_ID}/tasks/${taskId}`,
                }),
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

    expect(await screen.findByText("Task progress loaded.")).toBeInTheDocument();
    expect(await screen.findByText("控制面任务")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "打开任务" })).toHaveAttribute(
      "href",
      `/workspaces/${WORKSPACE_ID}/tasks/${taskId}`,
    );
  });
});
