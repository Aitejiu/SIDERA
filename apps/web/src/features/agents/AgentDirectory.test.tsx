import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { WorkspaceTabs, WorkspaceTabsProvider } from "@/app/WorkspaceTabs";
import { server } from "@/test/server";
import { AgentDetailPage } from "./AgentDetailPage";
import { AgentListPage } from "./AgentListPage";
import { DESCRIPTOR, PROFILE, PROFILE_ID, PROFILE_REF, PROFILE_WORK, RUNTIME, WORKSPACE_ID as WS, profileAt } from "./testFixtures";
import type { NativeRunSummary, ProfileWork, ProfileWorkRow } from "./types";

const BASE = `/agents/v1/workspaces/${WS}`;
const HERE = `/workspaces/${WS}/agents`;
const EMPTY: ProfileWork = { conversations: [], task_work: { items: [], next_cursor: null } };
type ProfileConversation = ProfileWork["conversations"][number];

function taskWorkRow(overrides: Partial<ProfileWorkRow> & Pick<ProfileWorkRow, "work_id">): ProfileWorkRow {
  return {
    workspace_id: WS,
    kind: "execution",
    profile_ref: PROFILE_REF,
    title: "Saved task work",
    status: "completed",
    updated_at: "2026-11-02T09:00:00Z",
    ...overrides,
  };
}

function conversationWork(overrides: Partial<ProfileConversation> & Pick<ProfileConversation, "conversation_id">): ProfileConversation {
  return {
    profile_ref: PROFILE_REF,
    run_id: null,
    stop_state: null,
    title: "Saved conversation",
    updated_at: "2026-11-02T09:00:00Z",
    ...overrides,
  };
}

function admittedRecentWork(): ProfileWork {
  const executionRun = "81000000-0000-0000-0000-000000000001";
  const conversationRun = "81000000-0000-0000-0000-000000000002";
  const planningRun = "81000000-0000-0000-0000-000000000003";
  const fourthRun = "81000000-0000-0000-0000-000000000004";
  return {
    task_work: {
      next_cursor: null,
      items: [
        taskWorkRow({
          work_id: "51000000-0000-0000-0000-000000000001",
          run_id: executionRun,
          title: "Stored execution title",
          updated_at: "2026-11-02T09:30:00Z",
          root_task_id: "61000000-0000-0000-0000-000000000001",
          worker_item_id: "51000000-0000-0000-0000-000000000001",
        }),
        taskWorkRow({
          work_id: "51000000-0000-0000-0000-000000000002",
          kind: "planning",
          run_id: planningRun,
          title: "Stored planning title",
          status: "prepared",
          updated_at: "2026-11-02T11:00:00+02:00",
          planning_request_id: "91000000-0000-0000-0000-000000000001",
        }),
        taskWorkRow({
          work_id: "51000000-0000-0000-0000-000000000003",
          run_id: executionRun,
          title: "Older duplicate title",
          updated_at: "2026-11-02T08:30:00Z",
        }),
        taskWorkRow({
          work_id: "51000000-0000-0000-0000-000000000004",
          run_id: fourthRun,
          title: "Fourth distinct run",
          updated_at: "2026-11-02T08:45:00Z",
        }),
        taskWorkRow({
          work_id: "51000000-0000-0000-0000-000000000005",
          run_id: null,
          title: "Work without a Run",
          updated_at: "2026-11-02T12:00:00Z",
        }),
        taskWorkRow({
          work_id: "51000000-0000-0000-0000-000000000006",
          workspace_id: "22000000-0000-0000-0000-000000000002",
          run_id: "81000000-0000-0000-0000-000000000005",
          title: "Foreign workspace work",
          updated_at: "2026-11-02T13:00:00Z",
        }),
        taskWorkRow({
          work_id: "51000000-0000-0000-0000-000000000007",
          profile_ref: { ...PROFILE_REF, profile_id: "30000000-0000-0000-0000-000000000002" },
          run_id: "81000000-0000-0000-0000-000000000006",
          title: "Foreign Profile work",
          updated_at: "2026-11-02T14:00:00Z",
        }),
      ],
    },
    conversations: [
      conversationWork({
        conversation_id: "a1000000-0000-0000-0000-000000000001",
        run_id: conversationRun,
        stop_state: "stopped",
        title: "Stored conversation title",
        updated_at: "2026-11-02T05:15:00-05:00",
      }),
      conversationWork({
        conversation_id: "a1000000-0000-0000-0000-000000000002",
        title: "Conversation without a Run",
      }),
      conversationWork({
        conversation_id: "a1000000-0000-0000-0000-000000000003",
        profile_ref: { ...PROFILE_REF, profile_id: "30000000-0000-0000-0000-000000000002" },
        run_id: "81000000-0000-0000-0000-000000000007",
        title: "Foreign Profile conversation",
        updated_at: "2026-11-02T15:00:00Z",
      }),
    ],
  };
}

function mount(detail = false, query = "") {
  const path = detail ? `${HERE}/${PROFILE_ID}` : HERE;
  const router = createMemoryRouter([{
    path: detail ? "/workspaces/:workspaceId/agents/:profileId" : "/workspaces/:workspaceId/agents",
    element: <WorkspaceTabsProvider workspaceId={WS}><WorkspaceTabs workspaceId={WS} />{detail ? <AgentDetailPage /> : <AgentListPage />}</WorkspaceTabsProvider>,
  }], { initialEntries: [path + query] });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider>);
  return { router, client };
}

beforeEach(() => {
  window.sessionStorage.clear();
  server.use(
    http.get(BASE + "/profiles", () => HttpResponse.json({ items: [PROFILE] })),
    http.get(BASE + "/runtimes", () => HttpResponse.json({ items: [RUNTIME] })),
    http.get(BASE + "/runtime-descriptors", () => HttpResponse.json([DESCRIPTOR])),
    http.get(BASE + `/profiles/${PROFILE_ID}`, () => HttpResponse.json(profileAt(3))),
    http.get(BASE + `/profiles/${PROFILE_ID}/validation`, () => HttpResponse.json({ valid: true, issues: [], effective_preview: null })),
    http.get(BASE + `/profiles/${PROFILE_ID}/work`, () => HttpResponse.json(PROFILE_WORK)),
  );
});

describe("reusable Profile directory", () => {
  it("reads actual rows, structural availability and recent work without discovery or execution", async () => {
    let writes = 0;
    server.use(http.post(BASE + "/*", () => { writes += 1; return HttpResponse.json({}, { status: 500 }); }));
    mount();
    const link = await screen.findByRole("link", { name: "Reusable reviewer" });
    expect(link).toHaveAttribute("href", `${HERE}/${PROFILE_ID}`);
    const row = link.closest("tr");
    expect(row).not.toBeNull();
    if (!row) return;
    expect(within(row).getByText("controlled-settings · 1")).toBeInTheDocument();
    expect(await within(row).findByText("配置可用")).toBeInTheDocument();
    expect(await within(row).findByText("Review actual Worker item")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "新建 Profile" })).toHaveAttribute("href", `${HERE}/new`);
    expect(screen.queryByText(/在线|成功率/)).not.toBeInTheDocument();
    expect(writes).toBe(0);
  });

  it("filters explicit enabled state and literal name without selecting the first Profile", async () => {
    const disabled = profileAt(1, { ...PROFILE.profile.draft, name: "Disabled author" });
    disabled.profile.ref = { ...PROFILE_REF, profile_id: "30000000-0000-0000-0000-000000000002" };
    disabled.status = { profile_id: disabled.profile.ref.profile_id, enabled: false, version: 2 };
    server.use(
      http.get(BASE + "/profiles", () => HttpResponse.json({ items: [PROFILE, disabled] })),
      http.get(BASE + `/profiles/${disabled.profile.ref.profile_id}/validation`, () => HttpResponse.json({ valid: false, issues: [], effective_preview: null })),
      http.get(BASE + `/profiles/${disabled.profile.ref.profile_id}/work`, () => HttpResponse.json(EMPTY)),
    );
    const { router } = mount();
    await screen.findByRole("link", { name: "Disabled author" });
    await userEvent.selectOptions(screen.getByLabelText("启用状态"), "enabled");
    expect(screen.queryByRole("link", { name: "Disabled author" })).not.toBeInTheDocument();
    await userEvent.type(screen.getByRole("searchbox", { name: "搜索 Profile" }), "missing");
    expect(screen.getByText("没有匹配的 Profile")).toBeInTheDocument();
    expect(router.state.location.pathname).toBe(HERE);
  });

  it("shows a genuine empty directory only after a successful empty read", async () => {
    server.use(http.get(BASE + "/profiles", () => HttpResponse.json({ items: [] })));
    mount();
    expect(await screen.findByText("尚无 Profile")).toBeInTheDocument();
    expect(screen.queryByText("Profile 目录不可用")).not.toBeInTheDocument();
  });

  it("keeps directory failure distinct from empty and does not display raw errors", async () => {
    server.use(http.get(BASE + "/profiles", () => HttpResponse.json({ detail: { code: "temporary_unavailable", message: "private raw body" } }, { status: 503 })));
    mount();
    expect(await screen.findByText("Profile 目录不可用")).toBeInTheDocument();
    expect(screen.queryByText("尚无 Profile")).not.toBeInTheDocument();
    expect(screen.queryByText("private raw body")).not.toBeInTheDocument();
  });

  it("does not turn failed configuration or Work reads into available or empty facts", async () => {
    server.use(
      http.get(BASE + `/profiles/${PROFILE_ID}/validation`, () => HttpResponse.json({}, { status: 503 })),
      http.get(BASE + `/profiles/${PROFILE_ID}/work`, () => HttpResponse.json({}, { status: 503 })),
    );
    mount();
    await screen.findByRole("link", { name: "Reusable reviewer" });
    expect(await screen.findByText("配置状态未确认")).toBeInTheDocument();
    expect(await screen.findByText("工作记录不可用")).toBeInTheDocument();
    expect(screen.queryByText("暂无工作记录")).not.toBeInTheDocument();
  });
});

describe("Profile detail and original Work", () => {
  it("shows the current Profile revision and distinct runtime and configuration facts", async () => {
    mount(true);
    expect(await screen.findByRole("heading", { name: "Reusable reviewer" })).toBeInTheDocument();
    expect(screen.getByText(PROFILE_ID)).toBeInTheDocument();
    expect(screen.getByText("R3")).toBeInTheDocument();
    expect(await screen.findByText("配置可用")).toBeInTheDocument();
    expect(screen.getByText("协议已就绪")).toBeInTheDocument();
    expect(screen.getByText("认证已就绪")).toBeInTheDocument();
    expect(screen.queryByText(/模型回复成功|在线/)).not.toBeInTheDocument();
  });

  it("keeps historical Work pins and reads the exact assignment discriminator only on request", async () => {
    let reads = 0;
    const original = PROFILE_WORK.task_work.items[0];
    if (original === undefined) throw new Error("Missing controlled Work row");
    const summary: NativeRunSummary = {
      available: true, reason_code: null, workspace_id: WS, kind: "assignment", work_id: original.work_id,
      run_id: original.run_id ?? null,
      run: { state: "running", reason_code: null, input_open: true, profile_ref: PROFILE_REF,
        runtime_ref: RUNTIME.ref, config_hash: "a".repeat(64), actual_runtime_version: "1",
        selected_model: "configured-model", observed_model: null, applied_fields: ["mcp"] },
    };
    server.use(http.get(`/api/v1/workspaces/${WS}/profile-work/assignment/${original.work_id}/run`, ({ request }) => {
      reads += 1;
      expect(new URL(request.url).searchParams.get("target_type")).toBe("worker_item");
      return HttpResponse.json(summary);
    }));
    mount(true, "?view=work");
    await screen.findByText("Review actual Worker item");
    expect(screen.getByText("Profile R1 · 绑定 V2")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "打开 WorkerItem" })).toHaveAttribute("href", `/workspaces/${WS}/worker-items/${original.worker_item_id}`);
    expect(reads).toBe(0);
    await userEvent.click(screen.getByRole("button", { name: "读取原 Run" }));
    expect(await screen.findByText("模型未观察到")).toBeInTheDocument();
    expect(screen.getByText("configured-model")).toBeInTheDocument();
    expect(reads).toBe(1);
  });

  it("shows native admission uncertainty explicitly without inventing a Run or replaying Start", async () => {
    let reads = 0, writes = 0;
    const original = PROFILE_WORK.task_work.items[0];
    if (original === undefined) throw new Error("Missing controlled Work row");
    server.use(
      http.get(`/api/v1/workspaces/${WS}/profile-work/assignment/${original.work_id}/run`, () => {
        reads += 1;
        return HttpResponse.json({ available: false, reason_code: "run_admission_unconfirmed", run_id: null, run: null, workspace_id: WS, kind: "assignment", work_id: original.work_id });
      }),
      http.post(BASE + "/*", () => { writes += 1; return HttpResponse.json({}); }),
    );
    mount(true, "?view=work");
    await screen.findByText("Review actual Worker item");
    await userEvent.click(screen.getByRole("button", { name: "读取原 Run" }));
    expect(await screen.findByText("原 Run 接纳未确认")).toBeInTheDocument();
    expect(screen.queryByText("尚未接纳 Run")).not.toBeInTheDocument();
    expect(reads).toBe(1); expect(writes).toBe(0);
  });

  it("does not mount an editable form when descriptor metadata is unavailable", async () => {
    server.use(http.get(BASE + "/runtime-descriptors", () => HttpResponse.json({}, { status: 503 })));
    mount(true, "?view=settings");
    await screen.findByRole("heading", { name: "Reusable reviewer" });
    expect(await screen.findByText("配置元数据不可用")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "保存 Profile" })).not.toBeInTheDocument();
  });

  it("uses the actual cursor for explicit next-page Work reads without native writes", async () => {
    const cursors: (string | null)[] = [];
    server.use(http.get(BASE + `/profiles/${PROFILE_ID}/work`, ({ request }) => {
      const cursor = new URL(request.url).searchParams.get("cursor");
      cursors.push(cursor);
      return HttpResponse.json(cursor === null ? { ...PROFILE_WORK, task_work: { ...PROFILE_WORK.task_work, next_cursor: "original/cursor 1" } } : EMPTY);
    }));
    mount(true, "?view=work");
    await screen.findByText("Review actual Worker item");
    await userEvent.click(screen.getByRole("button", { name: "下一页工作" }));
    await waitFor(() => expect(cursors).toEqual([null, "original/cursor 1"]));
    expect(await screen.findByText("暂无工作记录")).toBeInTheDocument();
  });

  it("overview recent runs: filters, orders, deduplicates, and links only saved Run rows", async () => {
    const work = admittedRecentWork();
    let posts = 0;
    let runReads = 0;
    server.use(
      http.get(BASE + `/profiles/${PROFILE_ID}/work`, () => HttpResponse.json(work)),
      http.post("*", () => { posts += 1; return HttpResponse.json({}); }),
      http.get(`/api/v1/workspaces/${WS}/profile-work/*/*/run`, () => {
        runReads += 1;
        return HttpResponse.json({});
      }),
    );

    mount(true);

    expect(await screen.findByRole("heading", { name: "Reusable reviewer" })).toBeInTheDocument();
    expect(screen.getByText("R3")).toBeInTheDocument();
    const region = await screen.findByRole("region", { name: "最近运行" });
    const rows = Array.from(region.querySelectorAll(".agent-work-row"));
    expect(rows).toHaveLength(3);
    expect(rows.map(row => row.querySelector("a, strong")?.textContent?.trim())).toEqual([
      "Stored conversation title",
      "Stored execution title",
      "Stored planning title",
    ]);
    for (const row of rows) expect(row.textContent).toContain("Profile R1");

    expect(within(region).getByRole("link", { name: "Stored conversation title" })).toHaveAttribute(
      "href",
      `/workspaces/${WS}/chat/a1000000-0000-0000-0000-000000000001`,
    );
    expect(within(region).getByRole("link", { name: "Stored execution title" })).toHaveAttribute(
      "href",
      `/workspaces/${WS}/worker-items/51000000-0000-0000-0000-000000000001`,
    );
    expect(within(region).getByRole("link", { name: "打开任务" })).toHaveAttribute(
      "href",
      `/workspaces/${WS}/tasks/61000000-0000-0000-0000-000000000001`,
    );
    expect(within(region).getByRole("link", { name: "Stored planning title" })).toHaveAttribute(
      "href",
      `/workspaces/${WS}/planning/91000000-0000-0000-0000-000000000001`,
    );
    expect(within(region).getByText("81000000-0000-0000-0000-000000000001", { exact: true })).toBeInTheDocument();
    expect(within(region).queryByText("Older duplicate title")).not.toBeInTheDocument();
    expect(within(region).queryByText("Fourth distinct run")).not.toBeInTheDocument();
    expect(within(region).queryByText("Work without a Run")).not.toBeInTheDocument();
    expect(within(region).queryByText("Foreign workspace work")).not.toBeInTheDocument();
    expect(within(region).queryByText("Foreign Profile work")).not.toBeInTheDocument();
    expect(within(region).queryByText("Conversation without a Run")).not.toBeInTheDocument();
    expect(within(region).queryByText("Foreign Profile conversation")).not.toBeInTheDocument();
    expect(posts).toBe(0);
    expect(runReads).toBe(0);
  });

  it("overview recent runs: reports a failed public Work read without leaking details", async () => {
    let posts = 0;
    let runReads = 0;
    server.use(
      http.get(BASE + `/profiles/${PROFILE_ID}/work`, () => HttpResponse.json({ detail: "private raw body" }, { status: 503 })),
      http.post("*", () => { posts += 1; return HttpResponse.json({}); }),
      http.get(`/api/v1/workspaces/${WS}/profile-work/*/*/run`, () => {
        runReads += 1;
        return HttpResponse.json({});
      }),
    );

    mount(true);

    expect(await screen.findByText("运行记录不可用")).toBeInTheDocument();
    expect(screen.queryByText("近期暂无运行记录")).not.toBeInTheDocument();
    expect(screen.queryByText("private raw body")).not.toBeInTheDocument();
    expect(posts).toBe(0);
    expect(runReads).toBe(0);
  });

  it("overview recent runs: keeps loading distinct from a successful scoped empty result", async () => {
    const noRuns: ProfileWork = {
      task_work: {
        next_cursor: "unseen/next-page",
        items: [taskWorkRow({ work_id: "51000000-0000-0000-0000-000000000008", run_id: null })],
      },
      conversations: [conversationWork({ conversation_id: "a1000000-0000-0000-0000-000000000004" })],
    };
    let releaseRead: (() => void) | undefined;
    const readGate = new Promise<void>(resolve => { releaseRead = resolve; });
    server.use(http.get(BASE + `/profiles/${PROFILE_ID}/work`, async () => {
      await readGate;
      return HttpResponse.json(noRuns);
    }));

    mount(true);

    expect(await screen.findByText("正在读取运行记录")).toBeInTheDocument();
    expect(screen.queryByText("近期暂无运行记录")).not.toBeInTheDocument();
    if (releaseRead === undefined) throw new Error("Work response gate was not installed");
    releaseRead();

    expect(await screen.findByText("近期暂无运行记录")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "全部工作" })).toHaveAttribute("href", `${HERE}/${PROFILE_ID}?view=work`);
  });

  it("overview recent runs: refreshes the same public Work query after closing an empty test dialog", async () => {
    const afterClose: ProfileWork = {
      task_work: {
        next_cursor: null,
        items: [taskWorkRow({
          work_id: "51000000-0000-0000-0000-000000000009",
          run_id: "81000000-0000-0000-0000-000000000008",
          title: "Known Run after empty test dialog",
          worker_item_id: "51000000-0000-0000-0000-000000000009",
        })],
      },
      conversations: [],
    };
    let reads = 0;
    let posts = 0;
    server.use(
      http.get(BASE + `/profiles/${PROFILE_ID}/work`, () => {
        reads += 1;
        return HttpResponse.json(reads === 1 ? EMPTY : afterClose);
      }),
      http.post("*", () => { posts += 1; return HttpResponse.json({}); }),
    );

    mount(true);

    expect(await screen.findByText("近期暂无运行记录")).toBeInTheDocument();
    expect(reads).toBe(1);
    await userEvent.click(screen.getByRole("button", { name: "测试已保存 Profile" }));
    const dialog = await screen.findByRole("dialog", { name: "测试 Profile" });
    expect(within(dialog).getByRole("textbox", { name: "测试文本" })).toHaveValue("");
    expect(within(dialog).getByRole("button", { name: "开始测试" })).toBeDisabled();
    expect(within(dialog).queryByText(/会话\s+[0-9a-f-]{36}/i)).not.toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "关闭测试" }));

    expect(await screen.findByText("Known Run after empty test dialog")).toBeInTheDocument();
    await waitFor(() => expect(reads).toBe(2));
    expect(posts).toBe(0);
    expect(screen.queryByRole("dialog", { name: "测试 Profile" })).not.toBeInTheDocument();
  });
});
