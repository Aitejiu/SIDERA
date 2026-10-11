import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { useState } from "react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { WorkspaceTabsProvider } from "@/app/WorkspaceTabs";
import type { MainAgentThread, MainAgentTurn } from "@/features/chat/types";
import { server } from "@/test/server";
import { ProfileTestDialog } from "./ProfileTestDialog";
import { readProfile } from "./api";
import { PROFILE, PROFILE_DRAFT, PROFILE_ID, PROFILE_REF, RUNTIME, WORKSPACE_ID as WS, profileAt } from "./testFixtures";

const THREAD = "a0000000-0000-0000-0000-000000000001", TURN = "b0000000-0000-0000-0000-000000000001";
const BASE = "/main-agent/v1/threads";
const EMPTY: MainAgentThread = { id: THREAD, title: "Pinned Profile test", createdAt: "2026-10-05T12:00:00Z", updatedAt: "2026-10-05T12:00:00Z", contextRefs: [], turns: [], profileRef: PROFILE_REF, runId: null };
function reply(operationId: string, completed = true): MainAgentTurn {
  return { id: TURN, threadId: THREAD, status: completed ? "completed" : "queued", createdAt: EMPTY.createdAt, startedAt: EMPTY.createdAt, completedAt: completed ? EMPTY.createdAt : null, finalResponse: completed ? "Original controlled reply" : null, items: [], usage: null, error: null, streamAvailable: false, recoveryMode: completed ? null : "poll", profileRef: PROFILE_REF, runId: "c0000000-0000-0000-0000-000000000001", responseId: null, operationId, operationState: "accepted" };
}
function mount(refresh = false) {
  function Page() {
    const [open, setOpen] = useState(true);
    const [profile, setProfile] = useState(refresh ? profileAt(1, { ...PROFILE_DRAFT, model_id: "reviewed-model" }) : PROFILE);
    return <>{refresh ? <button onClick={() => void readProfile(WS, PROFILE_ID).then(setProfile)}>读取当前 Profile head</button> : null}{open ? <ProfileTestDialog profile={profile} runtime={RUNTIME} onClose={() => setOpen(false)} /> : <p>Test dialog closed</p>}</>;
  }
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = createMemoryRouter([{ path: `/workspaces/${WS}/agents/${PROFILE_ID}`, element: <WorkspaceTabsProvider workspaceId={WS}><Page /></WorkspaceTabsProvider> }], { initialEntries: [`/workspaces/${WS}/agents/${PROFILE_ID}`] });
  render(<QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider>);
}
beforeEach(() => {
  window.localStorage.clear(); window.sessionStorage.clear();
  server.use(http.get(`/agents/v1/workspaces/${WS}/profiles/${PROFILE_ID}/validation`, () => HttpResponse.json({ valid: true, issues: [], effective_preview: null })));
});

describe("explicit pinned Profile test through the accepted N19 engine", () => {
  it("opens one exact empty thread before one explicit prompt and keeps model evidence truthful", async () => {
    const order: string[] = [];
    server.use(
      http.post(BASE, async ({ request }) => { order.push("empty"); expect(await request.json()).toEqual({ contextRefs: [], title: "Profile test · R1", profileRef: PROFILE_REF }); return HttpResponse.json(EMPTY, { status: 201 }); }),
      http.post(BASE + `/${THREAD}/turns`, async ({ request }) => { order.push("prompt"); const body = await request.json() as { input: string; operationId: string }; expect(body.input).toBe("Explicit reviewed text"); expect(body.operationId.length).toBeGreaterThan(0); return HttpResponse.json(reply(body.operationId), { status: 202 }); }),
    );
    mount();
    await screen.findByText("结构验证通过"); expect(order).toEqual([]);
    await userEvent.type(screen.getByLabelText("测试文本"), "Explicit reviewed text");
    await userEvent.dblClick(screen.getByRole("button", { name: "开始测试" }));
    await screen.findByText("Original controlled reply");
    expect(order).toEqual(["empty", "prompt"]);
    expect(screen.getByText("模型未观察到")).toBeInTheDocument();
    expect(screen.getByText("运行环境元数据 · controlled-settings · 1")).toBeInTheDocument();
    expect(screen.getByText("Profile R1")).toBeInTheDocument();
  });

  it("recovers a lost prompt reply from the exact original thread and opaque operation without resend", async () => {
    let creates = 0, prompts = 0, reads = 0, operationId = "";
    server.use(
      http.post(BASE, () => { creates += 1; return HttpResponse.json(EMPTY, { status: 201 }); }),
      http.post(BASE + `/${THREAD}/turns`, async ({ request }) => { prompts += 1; operationId = (await request.json() as { operationId: string }).operationId; return HttpResponse.error(); }),
      http.get(BASE + `/${THREAD}`, () => { reads += 1; return HttpResponse.json({ ...EMPTY, turns: [reply(operationId)] }); }),
    );
    mount(); await userEvent.type(await screen.findByLabelText("测试文本"), "One original prompt");
    await userEvent.click(screen.getByRole("button", { name: "开始测试" }));
    await screen.findByText("测试结果未确认");
    await userEvent.click(screen.getByRole("button", { name: "读取原测试结果" }));
    await screen.findByText("Original controlled reply");
    expect(creates).toBe(1); expect(prompts).toBe(1); expect(reads).toBe(1);
    expect(screen.getByRole("button", { name: "开始测试" })).toBeDisabled();
  });

  it("reads a known pending turn by its original thread and turn IDs", async () => {
    let operationId = "", reads = 0, prompts = 0;
    server.use(
      http.post(BASE, () => HttpResponse.json(EMPTY, { status: 201 })),
      http.post(BASE + `/${THREAD}/turns`, async ({ request }) => { prompts += 1; operationId = (await request.json() as { operationId: string }).operationId; return HttpResponse.json(reply(operationId, false), { status: 202 }); }),
      http.get(BASE + `/${THREAD}/turns/${TURN}`, () => { reads += 1; return HttpResponse.json(reply(operationId)); }),
    );
    mount(); await userEvent.type(await screen.findByLabelText("测试文本"), "Explicit pending text");
    await userEvent.click(screen.getByRole("button", { name: "开始测试" }));
    await screen.findByText("测试状态 · queued");
    await userEvent.click(screen.getByRole("button", { name: "读取原测试结果" }));
    await screen.findByText("Original controlled reply"); expect(reads).toBe(1); expect(prompts).toBe(1);
  });

  it("does not recreate an unknown empty thread or infer its ID from another route", async () => {
    let creates = 0, prompts = 0, reads = 0;
    server.use(
      http.post(BASE, () => { creates += 1; return HttpResponse.error(); }),
      http.post(BASE + "/*/turns", () => { prompts += 1; return HttpResponse.error(); }),
      http.get(BASE + "*", () => { reads += 1; return HttpResponse.json({ data: [], nextCursor: null }); }),
    );
    mount(); await userEvent.type(await screen.findByLabelText("测试文本"), "Explicit text");
    await userEvent.click(screen.getByRole("button", { name: "开始测试" }));
    await screen.findByText("测试会话创建结果未确认");
    expect(screen.getByRole("button", { name: "开始测试" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "关闭测试" })).toBeDisabled();
    expect(creates).toBe(1); expect(prompts).toBe(0); expect(reads).toBe(0);
  });

  it("retains an unknown individual Stop and closes only after same-thread confirmed cleanup", async () => {
    let stops = 0, prompts = 0;
    server.use(
      http.post(BASE, () => HttpResponse.json(EMPTY, { status: 201 })),
      http.post(BASE + `/${THREAD}/turns`, async ({ request }) => { prompts += 1; return HttpResponse.json(reply((await request.json() as { operationId: string }).operationId), { status: 202 }); }),
      http.post(BASE + `/${THREAD}/stop`, () => { stops += 1; return HttpResponse.json({ conversation_id: THREAD, cleanup_confirmed: stops > 1, stop_state: stops > 1 ? "stopped" : "unknown", reason_code: stops > 1 ? null : "cleanup_pending" }, { status: stops > 1 ? 200 : 202 }); }),
    );
    mount(); await userEvent.type(await screen.findByLabelText("测试文本"), "Explicit original text");
    await userEvent.click(screen.getByRole("button", { name: "开始测试" }));
    await screen.findByText("Original controlled reply");
    await userEvent.click(screen.getByRole("button", { name: "关闭测试" }));
    await screen.findByText("原停止结果未确认");
    expect(screen.getByRole("dialog", { name: "测试 Profile" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "查询原停止结果" }));
    await screen.findByText("本次测试清理已确认");
    await userEvent.click(screen.getByRole("button", { name: "关闭测试" }));
    await screen.findByText("Test dialog closed");
    expect(stops).toBe(2); expect(prompts).toBe(1);
  });

  it("rejects mismatched original history without replacement prompt or Profile pin", async () => {
    let prompts = 0;
    server.use(
      http.post(BASE, () => HttpResponse.json(EMPTY, { status: 201 })),
      http.post(BASE + `/${THREAD}/turns`, () => { prompts += 1; return HttpResponse.error(); }),
      http.get(BASE + `/${THREAD}`, () => HttpResponse.json({ ...EMPTY, profileRef: { ...PROFILE_REF, revision: 9 } })),
    );
    mount(); await userEvent.type(await screen.findByLabelText("测试文本"), "Original text");
    await userEvent.click(screen.getByRole("button", { name: "开始测试" }));
    await screen.findByText("测试结果未确认");
    await userEvent.click(screen.getByRole("button", { name: "读取原测试结果" }));
    await screen.findByText("原测试结果不可用");
    expect(screen.getByText("Profile R1")).toBeInTheDocument();
    await waitFor(() => expect(prompts).toBe(1));
  });

  it("retains reviewed R1 config and original recovery when its real parent reads head R2", async () => {
    let creates = 0, prompts = 0, operationId = "", originalReads = 0, headReads = 0;
    const validationRevisions: string[] = [];
    server.use(
      http.get(`/agents/v1/workspaces/${WS}/profiles/${PROFILE_ID}`, () => { headReads += 1; return HttpResponse.json(profileAt(2, { ...PROFILE_DRAFT, model_id: "new-head-model" })); }),
      http.get(`/agents/v1/workspaces/${WS}/profiles/${PROFILE_ID}/validation`, ({ request }) => { validationRevisions.push(new URL(request.url).searchParams.get("revision") ?? ""); return HttpResponse.json({ valid: true, issues: [], effective_preview: null }); }),
      http.post(BASE, async ({ request }) => { creates += 1; expect((await request.json() as { profileRef: typeof PROFILE_REF }).profileRef).toEqual(PROFILE_REF); return HttpResponse.json(EMPTY, { status: 201 }); }),
      http.post(BASE + `/${THREAD}/turns`, async ({ request }) => { prompts += 1; operationId = (await request.json() as { operationId: string }).operationId; return HttpResponse.error(); }),
      http.get(BASE + `/${THREAD}`, () => { originalReads += 1; return HttpResponse.json({ ...EMPTY, turns: [reply(operationId)] }); }),
    );
    mount(true);
    await userEvent.type(await screen.findByLabelText("测试文本"), "Original R1 text");
    await userEvent.click(screen.getByRole("button", { name: "开始测试" }));
    await screen.findByText("测试结果未确认");
    await userEvent.click(screen.getByRole("button", { name: "读取当前 Profile head" }));
    await waitFor(() => expect(headReads).toBe(1));
    await userEvent.click(screen.getByRole("button", { name: "读取原测试结果" }));
    await screen.findByText("Original controlled reply");
    expect(screen.getByText("Profile R1")).toBeInTheDocument();
    expect(screen.getByText("reviewed-model")).toBeInTheDocument();
    expect(screen.queryByText("new-head-model")).not.toBeInTheDocument();
    expect(creates).toBe(1); expect(prompts).toBe(1); expect(originalReads).toBe(1);
    expect(validationRevisions).toEqual(["1"]);
  });
});
