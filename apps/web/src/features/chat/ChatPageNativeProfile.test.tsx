import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { WorkspaceTabsProvider } from "@/app/WorkspaceTabs";
import { PROFILE, PROFILE_ID, PROFILE_REF, WORKSPACE_ID as WS, profileAt } from "@/features/agents/testFixtures";
import { server } from "@/test/server";
import { ChatPage } from "./ChatPage";
import type { MainAgentThread, MainAgentTurn } from "./types";

const HERE = `/workspaces/${WS}/chat`, THREAD = "a0000000-0000-0000-0000-000000000001";
const EMPTY: MainAgentThread = { id: THREAD, title: "Reviewed conversation", createdAt: "2026-10-05T12:00:00Z", updatedAt: "2026-10-05T12:00:00Z", contextRefs: [], turns: [], profileRef: PROFILE_REF, runId: null };
const DEFAULTS = `/product/v1/workspaces/${WS}/defaults`, PROFILE_PATH = `/agents/v1/workspaces/${WS}/profiles/${PROFILE_ID}`;
function mount(path = HERE) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const router = createMemoryRouter([{ path: "/workspaces/:workspaceId/chat/:threadId?", element: <WorkspaceTabsProvider workspaceId={WS}><ChatPage /></WorkspaceTabsProvider> }], { initialEntries: [path] });
  render(<QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider>);
  return { router, client };
}
beforeEach(() => {
  window.localStorage.clear(); window.sessionStorage.clear();
  server.use(
    http.get("/main-agent/v1/health", () => HttpResponse.json({ status: "ok", service: "sidera-product", provider: "available" })),
    http.get("/main-agent/v1/threads", () => HttpResponse.json({ data: [], nextCursor: null })),
    http.get(`/main-agent/v1/threads/${THREAD}`, () => HttpResponse.json(EMPTY)),
    http.get(DEFAULTS, () => HttpResponse.json({ workspace_id: WS, version: 1, main_profile_id: PROFILE_ID, planner_profile_id: null, worker_profile_id: null })),
    http.get(PROFILE_PATH, () => HttpResponse.json(PROFILE)),
  );
});

describe("native chat uses reviewed Profile pins and explicit local session entry", () => {
  it("creates only an empty thread with the explicitly selected authorized revision", async () => {
    let creates = 0, prompts = 0, defaults = 0;
    server.use(
      http.get(DEFAULTS, () => { defaults += 1; return HttpResponse.error(); }),
      http.get(PROFILE_PATH, ({ request }) => { expect(new URL(request.url).searchParams.get("revision")).toBe("1"); return HttpResponse.json(PROFILE); }),
      http.post("/main-agent/v1/threads", async ({ request }) => { creates += 1; expect(await request.json()).toEqual({ contextRefs: [], title: "新对话", profileRef: PROFILE_REF }); return HttpResponse.json(EMPTY, { status: 201 }); }),
      http.post("/main-agent/v1/threads/*/turns", () => { prompts += 1; return HttpResponse.error(); }),
    );
    const { router } = mount(HERE + `?profile_id=${PROFILE_ID}&profile_revision=1`);
    await screen.findByText("已选 Profile · Reusable reviewer · R1");
    expect(creates).toBe(0); expect(prompts).toBe(0); expect(defaults).toBe(0);
    await userEvent.click(screen.getByRole("button", { name: "使用该 Profile 新建对话" }));
    await waitFor(() => expect(router.state.location.pathname).toBe(HERE + `/${THREAD}`));
    expect(creates).toBe(1); expect(prompts).toBe(0);
  });

  it("pins the configured Main suggestion before opening and ignores a later observed head", async () => {
    let current = PROFILE, reads = 0, creates = 0;
    server.use(
      http.get(PROFILE_PATH, () => { reads += 1; return HttpResponse.json(current); }),
      http.post("/main-agent/v1/threads", async ({ request }) => { creates += 1; expect((await request.json() as { profileRef: typeof PROFILE_REF }).profileRef).toEqual(PROFILE_REF); return HttpResponse.json(EMPTY, { status: 201 }); }),
    );
    const { client } = mount();
    await screen.findByText("Main 默认 Profile · Reusable reviewer · R1");
    current = profileAt(2);
    await client.invalidateQueries({ queryKey: ["main-agent", WS, "new-profile"] });
    await waitFor(() => expect(reads).toBe(2));
    expect(screen.getByText("Main 默认 Profile · Reusable reviewer · R1")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "使用该 Profile 新建对话" }));
    await screen.findByRole("heading", { name: "Reviewed conversation" }); expect(creates).toBe(1);
  });

  it("does not infer a first directory row when Main defaults are missing", async () => {
    let creates = 0, profiles = 0;
    server.use(
      http.get(DEFAULTS, () => HttpResponse.json({ workspace_id: WS, version: 0, main_profile_id: null, planner_profile_id: null, worker_profile_id: null })),
      http.get(`/agents/v1/workspaces/${WS}/profiles`, () => { profiles += 1; return HttpResponse.json({ items: [PROFILE] }); }),
      http.post("/main-agent/v1/threads", () => { creates += 1; return HttpResponse.error(); }),
    );
    mount();
    await screen.findByText("Main 默认 Profile 未设置");
    expect(creates).toBe(0); expect(profiles).toBe(0);
    expect(screen.queryByRole("button", { name: "使用该 Profile 新建对话" })).not.toBeInTheDocument();
  });

  it("preserves an old thread pin without default or directory reads even with new selection params", async () => {
    let defaults = 0, profiles = 0, writes = 0;
    server.use(
      http.get(DEFAULTS, () => { defaults += 1; return HttpResponse.error(); }),
      http.get(PROFILE_PATH, () => { profiles += 1; return HttpResponse.json(profileAt(9)); }),
      http.post("/main-agent/v1/*", () => { writes += 1; return HttpResponse.error(); }),
    );
    mount(HERE + `/${THREAD}?profile_id=${PROFILE_ID}&profile_revision=9`);
    await screen.findByRole("heading", { name: "Reviewed conversation" });
    expect(screen.getByText("Profile R1")).toBeInTheDocument();
    expect(defaults).toBe(0); expect(profiles).toBe(0); expect(writes).toBe(0);
  });

  it("offers local-session entry only for the exact missing-session denial", async () => {
    let writes = 0;
    server.use(
      http.get("/main-agent/v1/health", () => HttpResponse.json({ error: { code: "local_session_required", message: "Local session required", retryable: false } }, { status: 401 })),
      http.get("/main-agent/v1/threads", () => HttpResponse.json({ error: { code: "local_session_required", message: "Local session required", retryable: false } }, { status: 401 })),
      http.get("/product/v1/session", () => HttpResponse.json({ detail: { code: "local_session_required" } }, { status: 401 })),
      http.post("/product/v1/*", () => { writes += 1; return HttpResponse.error(); }),
    );
    mount();
    await userEvent.click(await screen.findByRole("button", { name: "本地会话" }));
    await screen.findByLabelText("CLI 令牌"); expect(writes).toBe(0);
  });

  it("does not invite cookie replacement for authenticated foreign-workspace denial", async () => {
    server.use(http.get("/main-agent/v1/health", () => HttpResponse.json({ error: { code: "permission_denied", message: "Denied", retryable: false } }, { status: 403 })));
    mount();
    await screen.findByText("Main Agent 暂不可用");
    expect(screen.queryByRole("button", { name: "本地会话" })).not.toBeInTheDocument();
  });

  it.each(["matching", "mismatched"] as const)("checks the %s native context empty thread before its first prompt", async (reply) => {
    const contextRef = "task:30000000-0000-0000-0000-000000000001";
    const path = HERE + `?profile_id=${PROFILE_ID}&profile_revision=1&context_ref=${contextRef}`;
    const input = "Discuss the original controlled task";
    const empty = { ...EMPTY, contextRefs: [contextRef], profileRef: { ...PROFILE_REF, revision: reply === "matching" ? 1 : 9 } };
    let creates = 0, prompts = 0, originalOperation = "";
    server.use(
      http.post("/main-agent/v1/threads", async ({ request }) => {
        creates += 1;
        expect(await request.json()).toEqual({ contextRefs: [contextRef], title: "上下文讨论", profileRef: PROFILE_REF });
        return HttpResponse.json(empty, { status: 201 });
      }),
      http.get(`/main-agent/v1/threads/${THREAD}`, () => HttpResponse.json(empty)),
      http.post(`/main-agent/v1/threads/${THREAD}/turns`, async ({ request }) => {
        prompts += 1;
        const body = await request.json() as { input: string; operationId: string };
        expect(creates).toBe(1);
        expect(body.input).toBe(input);
        expect(body.operationId).toMatch(/^[0-9a-f-]{36}$/);
        originalOperation = body.operationId;
        const turn: MainAgentTurn = {
          id: "b0000000-0000-0000-0000-000000000001", threadId: THREAD, status: "completed",
          createdAt: EMPTY.createdAt, startedAt: EMPTY.createdAt, completedAt: EMPTY.createdAt,
          finalResponse: "Original context reply", items: [], usage: null, error: null,
          streamAvailable: false, recoveryMode: null, profileRef: PROFILE_REF,
          operationId: body.operationId, operationState: "accepted",
        };
        return HttpResponse.json(turn);
      }),
    );
    const { router, client } = mount(path);
    await screen.findByText("已选 Profile · Reusable reviewer · R1");
    const composer = screen.getByRole("textbox", { name: "给 Main Agent 的消息" });
    expect(composer).toHaveValue(contextRef);
    expect(creates).toBe(0); expect(prompts).toBe(0);
    await userEvent.clear(composer);
    await userEvent.type(composer, input);
    expect(screen.getByRole("textbox", { name: "给 Main Agent 的消息" })).toHaveValue(input);
    await userEvent.click(screen.getByRole("button", { name: "发送消息" }));
    await waitFor(() => expect(creates).toBe(1));
    if (reply === "matching") {
      await screen.findByText("Original context reply");
      expect(router.state.location.pathname).toBe(HERE + `/${THREAD}`);
      expect(prompts).toBe(1); expect(originalOperation).not.toBe("");
    } else {
      await screen.findByText("新对话创建结果未确认");
      expect(router.state.location.pathname + router.state.location.search).toBe(path);
      expect(screen.getByRole("textbox", { name: "给 Main Agent 的消息" })).toHaveValue(input);
      expect(client.getQueryData(["main-agent", WS, "thread", THREAD])).toBeUndefined();
      expect(prompts).toBe(0);
    }
    expect(creates).toBe(1);
  });
});
