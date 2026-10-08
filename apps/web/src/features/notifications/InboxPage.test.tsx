import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { server } from "@/test/server";
import type { ProjectionStreamSnapshot } from "@/shared/api/projectionStream";

import type { NotificationThread, NotificationThreadDetail } from "./api";
import { InboxPage } from "./InboxPage";

const { streamState, seedSnapshotCursor } = vi.hoisted(() => ({
  streamState: {
    status: "live" as ProjectionStreamSnapshot["status"],
    cursor: 0,
    lastEventAt: null,
    error: null,
  },
  seedSnapshotCursor: vi.fn(),
}));

vi.mock("@/app/WorkspaceStreamProvider", () => ({
  useWorkspaceStreamOptional: () => ({ streamState, seedSnapshotCursor }),
}));

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const ROOT_TASK_ID = "30000000-0000-0000-0000-000000000001";
const SUBTASK_ID = "40000000-0000-0000-0000-000000000001";
const WORKER_ITEM_ID = "50000000-0000-0000-0000-000000000001";

const summary = {
  open_count: 2,
  unread_count: 1,
  progress_count: 3,
  channel_issue_count: 1,
  last_change_cursor: 42,
  recipient_type: "workspace_operator",
  recipient_id: "aitejiu",
};

function makeThread(
  overrides: Partial<NotificationThread> = {},
): NotificationThread {
  return {
    id: "60000000-0000-0000-0000-000000000001",
    workspace_id: WORKSPACE_ID,
    recipient_type: "workspace_operator",
    recipient_id: "aitejiu",
    category: "action",
    business_status: "open",
    organization_status: "active",
    priority: "high",
    root_task_id: ROOT_TASK_ID,
    root_task_title: "支付重构",
    subtask_id: SUBTASK_ID,
    subtask_title: "通知网关",
    worker_item_id: WORKER_ITEM_ID,
    worker_item_title: "投递通道",
    title: "渠道需要处理",
    summary: "外部渠道连续失败，需要确认是否重新投递。",
    action: "确认恢复路径",
    topic: "notification.delivery",
    notification_ref: "notification-ref-1",
    latest_fact_id: "70000000-0000-0000-0000-000000000001",
    latest_fact_occurred_at: "2026-09-04T08:30:00Z",
    last_read_fact_id: null,
    unread_count: 1,
    version: 4,
    deep_link: `/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}/subtasks/notify-gateway?view=governance`,
    opened_at: "2026-09-04T08:00:00Z",
    resolved_at: null,
    archived_at: null,
    updated_at: "2026-09-04T08:30:00Z",
    ...overrides,
  };
}

function page(items: NotificationThread[]) {
  return { items, next_cursor: null, last_change_cursor: 42 };
}

function makeDetail(
  overrides: Partial<NotificationThreadDetail> = {},
): NotificationThreadDetail {
  const thread = makeThread(overrides.thread);
  return {
    thread,
    facts: {
      items: [{
        id: thread.latest_fact_id,
        thread_id: thread.id,
        policy_version: 1,
        kind: "blocked",
        priority: "high",
        transition: "opened",
        topic: thread.topic,
        root_task_id: thread.root_task_id,
        subtask_id: thread.subtask_id,
        worker_item_id: thread.worker_item_id,
        source_projection_cursor: 10,
        source_projection_type: "worker_item",
        source_resource_id: WORKER_ITEM_ID,
        source_resource_version: 3,
        title: thread.title,
        summary: thread.summary,
        action: thread.action,
        deep_link: thread.deep_link,
        correlation_id: "80000000-0000-0000-0000-000000000001",
        baseline_import: false,
        occurred_at: thread.latest_fact_occurred_at,
        created_at: thread.latest_fact_occurred_at,
      }],
      next_cursor: null,
    },
    deliveries: [{
      delivery_id: "90000000-0000-0000-0000-000000000001",
      route_id: "a0000000-0000-0000-0000-000000000001",
      channel_type: "akashic_private",
      contract_version: 1,
      status: "exhausted",
      attempts: 10,
      next_attempt_at: "2026-09-04T09:00:00Z",
      last_error_code: "provider_unavailable",
      delivered_at: null,
      exhausted_at: "2026-09-04T09:00:00Z",
      created_at: "2026-09-04T08:30:00Z",
      updated_at: "2026-09-04T09:00:00Z",
    }],
    notification_ref: "notification-ref-1",
    available_commands: ["mark_read"],
    deep_link: thread.deep_link,
    last_change_cursor: 42,
    ...overrides,
  };
}

function installDetailHandler(detail = makeDetail()) {
  server.use(
    http.get(
      `/api/v1/workspaces/${WORKSPACE_ID}/notification-threads/${detail.thread.id}`,
      () => HttpResponse.json(detail),
    ),
  );
}

function installHandlers({
  threads = {
    action: [makeThread()],
    progress: [makeThread({
      id: "60000000-0000-0000-0000-000000000002",
      category: "progress",
      business_status: "resolved",
      title: "节点已恢复",
      summary: "投递节点已恢复工作。",
      action: "",
      unread_count: 0,
      last_read_fact_id: "70000000-0000-0000-0000-000000000002",
    })],
    archived: [makeThread({
      id: "60000000-0000-0000-0000-000000000003",
      category: "archived",
      business_status: "resolved",
      organization_status: "archived",
      title: "历史通知",
      summary: "已归档的通知线程。",
      action: "",
      unread_count: 0,
      last_read_fact_id: "70000000-0000-0000-0000-000000000003",
      archived_at: "2026-09-03T08:30:00Z",
    })],
  },
}: {
  threads?: Record<"action" | "progress" | "archived", NotificationThread[]>;
} = {}) {
  server.use(
    http.get(`/api/v1/workspaces/${WORKSPACE_ID}/notification-summary`, () =>
      HttpResponse.json(summary)),
    http.get(`/api/v1/workspaces/${WORKSPACE_ID}/notification-threads`, ({ request }) => {
      const category = (new URL(request.url).searchParams.get("category") ?? "action") as keyof typeof threads;
      return HttpResponse.json(page(threads[category]));
    }),
  );
}

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const rendered = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/inbox`]}>
        <Routes>
          <Route path="/workspaces/:workspaceId/inbox" element={<InboxPage />} />
          <Route path="*" element={<LocationProbe />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...rendered, queryClient };
}

describe("InboxPage", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    server.resetHandlers();
    server.use(http.get("/main-agent/v1/health", () => HttpResponse.json({ status: "ok", service: "sidera-main-agent-gateway" })));
    streamState.status = "live";
    streamState.error = null;
    seedSnapshotCursor.mockClear();
  });

  it("seeds the projection stream and keeps the last snapshot visible while offline", async () => {
    streamState.status = "offline";
    installHandlers();
    renderPage();

    expect(await screen.findByText("渠道需要处理")).toBeVisible();
    await waitFor(() => expect(seedSnapshotCursor).toHaveBeenCalledWith(42));
    expect(screen.getByRole("status", { name: "信箱实时状态" })).toHaveTextContent("实时连接已断开");
    expect(screen.getByText("外部渠道连续失败，需要确认是否重新投递。")).toBeVisible();
  });

  it("names the exact result input blocker in the inbox", async () => {
    const blocked = makeThread({
      title: "汇总任务",
      summary: "结果输入暂时无法交付，需要处理。",
      topic: "result_inputs_blocked:result_input_unavailable",
    });
    installHandlers({ threads: { action: [blocked], progress: [], archived: [] } });
    renderPage();

    expect(await screen.findByText("选中的结果暂时不可用")).toBeVisible();
    expect(screen.getByText("结果输入暂时无法交付，需要处理。")).toBeVisible();
  });

  it("shows counts and human task path before notification details", async () => {
    installHandlers();
    renderPage();

    expect(await screen.findByRole("heading", { name: "信箱" })).toBeVisible();
    const summaryRegion = screen.getByLabelText("信箱概览");
    expect(await within(summaryRegion).findByText("需要处理 2")).toBeVisible();
    expect(await within(summaryRegion).findByText("今日进度 3")).toBeVisible();
    expect(await within(summaryRegion).findByText("渠道异常 1")).toBeVisible();
    expect(screen.getByRole("tab", { name: "需要处理 2" })).toBeVisible();
    expect(screen.getByText("支付重构 / 通知网关 / 投递通道")).toBeVisible();
    expect(screen.getAllByText("渠道需要处理").length).toBeGreaterThan(0);
    expect(screen.getByText("外部渠道连续失败，需要确认是否重新投递。")).toBeVisible();
    expect(screen.queryByText(WORKER_ITEM_ID)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "展开渠道需要处理" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
  });

  it("uses the three server categories and keeps empty states local to the category", async () => {
    installHandlers({ threads: { action: [], progress: [], archived: [] } });
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByText("当前没有需要处理的事项")).toBeVisible();
    await user.click(screen.getByRole("tab", { name: "进度通报 3" }));
    expect(await screen.findByText("当前没有新的进度通报")).toBeVisible();
    await user.click(screen.getByRole("tab", { name: "已归档" }));
    expect(await screen.findByText("暂无已归档事项")).toBeVisible();
    expect(screen.queryByText("渠道需要处理")).not.toBeInTheDocument();
  });

  it("shows a retryable error without replacing it with a local fallback", async () => {
    let attempts = 0;
    server.use(
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/notification-summary`, () =>
        HttpResponse.json(summary)),
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/notification-threads`, () => {
        attempts += 1;
        return attempts === 1
          ? new HttpResponse(null, { status: 503 })
          : HttpResponse.json(page([makeThread()]));
      }),
    );
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByRole("button", { name: "重试读取信箱" })).toBeVisible();
    expect(screen.getByText("无法读取信箱")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "重试读取信箱" }));
    expect(await screen.findByText("渠道需要处理")).toBeVisible();
    expect(attempts).toBe(2);
  });

  it("keeps a stale list visible while a refresh reports an error", async () => {
    installHandlers();
    const { queryClient } = renderPage();
    expect(await screen.findByText("渠道需要处理")).toBeVisible();

    server.use(
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/notification-threads`, () =>
        new HttpResponse(null, { status: 503 })),
    );
    await queryClient.refetchQueries({
      queryKey: ["notifications", WORKSPACE_ID, "threads"],
      type: "active",
    }).catch(() => undefined);

    expect(screen.getAllByText("渠道需要处理").length).toBeGreaterThan(0);
    expect(await screen.findByText("当前显示的是上次成功读取的信箱快照")).toBeVisible();
  });

  it("expands the thread history inline instead of navigating to a notification detail page", async () => {
    installHandlers();
    server.use(
      http.get(
        `/api/v1/workspaces/${WORKSPACE_ID}/notification-threads/60000000-0000-0000-0000-000000000001`,
        () => HttpResponse.json({
          thread: makeThread(),
          facts: {
            items: [{
              id: "70000000-0000-0000-0000-000000000001",
              thread_id: "60000000-0000-0000-0000-000000000001",
              policy_version: 1,
              kind: "blocked",
              priority: "high",
              transition: "opened",
              topic: "notification.delivery",
              root_task_id: ROOT_TASK_ID,
              subtask_id: SUBTASK_ID,
              worker_item_id: WORKER_ITEM_ID,
              source_projection_cursor: 10,
              source_projection_type: "worker_item",
              source_resource_id: WORKER_ITEM_ID,
              source_resource_version: 3,
              title: "渠道需要处理",
              summary: "外部渠道连续失败，需要确认是否重新投递。",
              action: "确认恢复路径",
              deep_link: `/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}/subtasks/notify-gateway?view=governance`,
              correlation_id: "80000000-0000-0000-0000-000000000001",
              baseline_import: false,
              occurred_at: "2026-09-04T08:30:00Z",
              created_at: "2026-09-04T08:30:00Z",
            }],
            next_cursor: null,
          },
          deliveries: [],
          notification_ref: "notification-ref-1",
          available_commands: ["mark_read"],
          deep_link: `/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}/subtasks/notify-gateway?view=governance`,
          last_change_cursor: 42,
        }),
      ),
    );
    const user = userEvent.setup();
    renderPage();

    const row = await screen.findByText("渠道需要处理");
    expect(screen.queryByText("notification-ref-1")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "展开渠道需要处理" }));

    const history = await screen.findByRole("region", { name: "渠道需要处理的线程历史" });
    expect(history).toBeVisible();
    expect(within(history).getByText(/notification-ref-1/)).toBeVisible();
    expect(within(history).getByText("Fact 时间线")).toBeVisible();
    expect(row).toBeVisible();
  });

  it("shows only API-authorized actions and keeps an open thread in the action list", async () => {
    installHandlers();
    const detail = makeDetail();
    installDetailHandler(detail);
    let commandBody: Record<string, unknown> | undefined;
    server.use(
      http.post("/api/v1/commands", async ({ request }) => {
        commandBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({
          aggregate_id: detail.thread.id,
          aggregate_version: detail.thread.version + 1,
          command_id: commandBody.command_id,
          event_ids: [],
        });
      }),
    );
    const user = userEvent.setup();
    renderPage();

    await screen.findByText("渠道需要处理");
    await user.click(screen.getByRole("button", { name: "展开渠道需要处理" }));
    const history = await screen.findByRole("region", { name: "渠道需要处理的线程历史" });
    expect(within(history).getByRole("button", { name: "标记已读" })).toBeVisible();
    expect(within(history).queryByRole("button", { name: "归档线程" })).not.toBeInTheDocument();
    expect(within(history).getByRole("button", { name: "重新投递" })).toBeVisible();

    await user.click(within(history).getByRole("button", { name: "标记已读" }));
    await waitFor(() => expect(commandBody?.command_type).toBe("MarkNotificationThreadRead"));
    expect(commandBody?.target_id).toBe(detail.thread.id);
    expect(screen.getAllByText("渠道需要处理").length).toBeGreaterThan(0);
  });

  it("requires a reason before requeue and sends the exhausted delivery fence", async () => {
    installHandlers();
    const detail = makeDetail();
    installDetailHandler(detail);
    let commandBody: Record<string, unknown> | undefined;
    server.use(
      http.post("/api/v1/commands", async ({ request }) => {
        commandBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({
          aggregate_id: detail.thread.id,
          aggregate_version: detail.thread.version + 1,
          command_id: commandBody.command_id,
          event_ids: [],
        });
      }),
    );
    const user = userEvent.setup();
    renderPage();

    await screen.findByText("渠道需要处理");
    await user.click(screen.getByRole("button", { name: "展开渠道需要处理" }));
    const history = await screen.findByRole("region", { name: "渠道需要处理的线程历史" });
    await user.click(within(history).getByRole("button", { name: "重新投递" }));
    expect(within(history).getByLabelText("重新投递原因")).toBeVisible();
    expect(within(history).getByRole("button", { name: "确认重新投递" })).toBeDisabled();
    await user.type(within(history).getByLabelText("重新投递原因"), "确认渠道已经恢复");
    await user.click(within(history).getByRole("button", { name: "确认重新投递" }));

    await waitFor(() => expect(commandBody?.command_type).toBe("RequeueNotificationDelivery"));
    expect(commandBody?.target_id).toBe(detail.deliveries[0]?.delivery_id);
    expect(commandBody?.payload).toEqual({
      expected_route_version: 1,
      expected_status: "exhausted",
      reason: "确认渠道已经恢复",
    });
  });

  it("preserves governance deep-link metadata when opening the actual worker target", async () => {
    installHandlers();
    const detail = makeDetail();
    installDetailHandler(detail);
    const user = userEvent.setup();
    renderPage();

    await screen.findByText("渠道需要处理");
    await user.click(screen.getByRole("button", { name: "展开渠道需要处理" }));
    const history = await screen.findByRole("region", { name: "渠道需要处理的线程历史" });
    await user.click(within(history).getByRole("button", { name: "打开 WorkerItem" }));

    expect(await screen.findByTestId("current-route")).toHaveTextContent(
      `/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}/subtasks/notify-gateway?view=governance`,
    );
  });

  it("shows archive only for an API-authorized resolved thread", async () => {
    const resolved = makeThread({
      business_status: "resolved",
      unread_count: 0,
      last_read_fact_id: "70000000-0000-0000-0000-000000000001",
    });
    installHandlers({ threads: { action: [resolved], progress: [], archived: [] } });
    installDetailHandler(makeDetail({
      thread: resolved,
      available_commands: ["archive"],
    }));
    const user = userEvent.setup();
    renderPage();

    await screen.findByText("渠道需要处理");
    await user.click(screen.getByRole("button", { name: "展开渠道需要处理" }));
    const history = await screen.findByRole("region", { name: "渠道需要处理的线程历史" });
    expect(within(history).getByRole("button", { name: "归档线程" })).toBeVisible();
    expect(within(history).queryByRole("button", { name: "标记已读" })).not.toBeInTheDocument();
  });

  it("preserves the actual legacy archive writer under fresh positive gateway health", async () => {
    const resolved = makeThread({ business_status: "resolved" });
    installHandlers({ threads: { action: [resolved], progress: [], archived: [] } });
    installDetailHandler(makeDetail({ thread: resolved, available_commands: ["archive"] }));
    const posted: Record<string, unknown>[] = [];
    server.use(http.post("/api/v1/commands", async ({ request }) => {
      const command = await request.json() as Record<string, unknown>; posted.push(command);
      return HttpResponse.json({ command_id: command.command_id, aggregate_id: resolved.id, aggregate_version: 5, event_ids: [] });
    }));
    renderPage(); const user = userEvent.setup(); await screen.findByText("渠道需要处理");
    await user.click(screen.getByRole("button", { name: "展开渠道需要处理" }));
    const archive = await screen.findByRole("button", { name: "归档线程" }); await waitFor(() => expect(archive).toBeEnabled()); await user.click(archive);
    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]).toMatchObject({ command_type: "ArchiveNotificationThread", actor: { type: "local", id: "aitejiu" }, target_id: resolved.id, expected_version: 4, payload: {} });
  });

  it("fresh legacy health loss denies the old builder before allocating identity", async () => {
    installHandlers(); installDetailHandler(); let healthReads = 0, posts = 0;
    server.use(
      http.get("/main-agent/v1/health", () => { healthReads++; return HttpResponse.json({ status: "ok", service: healthReads === 1 ? "sidera-main-agent-gateway" : "unknown" }); }),
      http.post("/api/v1/commands", () => { posts++; return new HttpResponse(null, { status: 503 }); }),
    );
    renderPage(); const user = userEvent.setup(); await screen.findByText("渠道需要处理");
    await user.click(screen.getByRole("button", { name: "展开渠道需要处理" }));
    const button = await screen.findByRole("button", { name: "标记已读" }); await waitFor(() => expect(button).toBeEnabled());
    const identity = vi.spyOn(crypto, "randomUUID"); await user.click(button); await waitFor(() => expect(healthReads).toBe(2));
    expect(identity).not.toHaveBeenCalled(); expect(posts).toBe(0);
  });

  it("keeps server cursor pagination as GET only", async () => {
    installHandlers(); const cursors: (string | null)[] = []; let posts = 0;
    server.use(
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/notification-threads`, ({ request }) => {
        const cursor = new URL(request.url).searchParams.get("cursor"); cursors.push(cursor);
        return HttpResponse.json({ items: [makeThread(cursor ? { id: "60000000-0000-0000-0000-000000000004", title: "第二页线程" } : {})], next_cursor: cursor ? null : "next-page", last_change_cursor: 42 });
      }),
      http.post("/api/v1/commands", () => { posts++; return new HttpResponse(null, { status: 503 }); }),
    );
    renderPage(); await screen.findByText("渠道需要处理"); await userEvent.setup().click(screen.getByRole("button", { name: "读取更多" }));
    expect(await screen.findByText("第二页线程")).toBeVisible(); expect(cursors).toEqual([null, "next-page"]); expect(posts).toBe(0);
    expect(screen.getByText("渠道需要处理")).toBeVisible();
  });

  it("keeps delivery diagnostics GET only without constructing a command", async () => {
    installHandlers(); const detail = makeDetail(); installDetailHandler(detail); let gets = 0, posts = 0;
    server.use(
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/notification-deliveries/${detail.deliveries[0]!.delivery_id}/attempts`, () => {
        gets++; return HttpResponse.json({ items: [{ delivery_id: detail.deliveries[0]!.delivery_id, attempt_number: 10, outcome: "provider_unavailable", error_code: "provider_unavailable" }], next_cursor: null });
      }),
      http.post("/api/v1/commands", () => { posts++; return new HttpResponse(null, { status: 503 }); }),
    );
    renderPage(); const user = userEvent.setup(); await screen.findByText("渠道需要处理"); await user.click(screen.getByRole("button", { name: "展开渠道需要处理" }));
    const diagnostics = await screen.findByRole("button", { name: "查看投递诊断" }); const identity = vi.spyOn(crypto, "randomUUID"); await user.click(diagnostics);
    expect(await screen.findByText("第 10 次")).toBeVisible(); expect(gets).toBe(1); expect(posts).toBe(0); expect(identity).not.toHaveBeenCalled();
  });
});

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="current-route">{location.pathname}{location.search}</div>;
}
