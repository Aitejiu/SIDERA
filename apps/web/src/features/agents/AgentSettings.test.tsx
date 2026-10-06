import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { createMemoryRouter, Outlet, RouterProvider } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { WorkspaceTabs, WorkspaceTabsProvider, useWorkspaceTabs } from "@/app/WorkspaceTabs";
import { initialTabs, loadTabs, openTab, reduceTabs, saveTabs, storageKey, type ConfigurationRecovery } from "@/app/workspaceTabsState";
import { server } from "@/test/server";
import { AgentSettings } from "./AgentSettings";
import { listDescriptors, listRuntimes } from "./api";
import { DESCRIPTOR, PROFILE, PROFILE_DRAFT, PROFILE_ID, PROFILE_REF, RUNTIME, RUNTIME_ID, WORKSPACE_ID as WS, profileAt } from "./testFixtures";
import type { ConfigurationResult, ProfileDraft, ProfileView } from "./types";

const BASE = `/agents/v1/workspaces/${WS}`;
const HERE = `/workspaces/${WS}/agents/${PROFILE_ID}`;
const THERE = `/workspaces/${WS}/tasks/task-b`;
const savedRef = { ...PROFILE_REF, revision: 2 };

function result(operationId: string, state: ConfigurationResult["state"] = "succeeded"): ConfigurationResult {
  return { operationId, state, target_ref: savedRef, result_ref: state === "succeeded" || state === "reconciled" ? savedRef : null, reason_code: state === "rejected" ? "revision_conflict" : null };
}

function mount(options: { recovery?: ConfigurationRecovery; profile?: ProfileView | null; actualMetadata?: boolean } = {}) {
  const creating = options.profile === null;
  const href = creating ? `/workspaces/${WS}/agents/new` : HERE;
  let state = reduceTabs(initialTabs(WS), openTab({ id: "root-task:task-b", kind: "root-task", title: "Task B", href: THERE, closeable: true }));
  state = reduceTabs(state, openTab({ id: creating ? "agent:new" : `agent:${PROFILE_ID}`, kind: "agent", title: "Reviewer", href, closeable: true, ...(options.recovery ? { configurationRecovery: options.recovery } : {}) }));
  saveTabs(state);
  const onSaved = vi.fn();
  function Shell() {
    const tabs = useWorkspaceTabs();
    return <><WorkspaceTabs workspaceId={WS} /><button onClick={() => tabs.activate("root-task:task-b")}>Task tab</button><output aria-label="selected tab">{tabs.state.activeTabId}</output><Outlet /></>;
  }
  function SettingsRoute() {
    const runtimes = useQuery({ queryKey: ["settings-runtimes", WS], queryFn: () => listRuntimes(WS), enabled: options.actualMetadata === true });
    const descriptors = useQuery({ queryKey: ["settings-descriptors", WS], queryFn: () => listDescriptors(WS), enabled: options.actualMetadata === true });
    if (options.actualMetadata && (runtimes.isPending || descriptors.isPending)) return <p role="status">正在读取配置元数据</p>;
    if (runtimes.isError || descriptors.isError) return <p role="alert">配置元数据不可用</p>;
    return <AgentSettings workspaceId={WS} profile={options.profile === undefined ? PROFILE : options.profile} runtimes={runtimes.data?.items ?? [RUNTIME]} descriptors={descriptors.data ?? [DESCRIPTOR]} onSaved={onSaved} />;
  }
  const router = createMemoryRouter([{
    element: <WorkspaceTabsProvider workspaceId={WS}><Shell /></WorkspaceTabsProvider>,
    children: [
      { path: href, element: <SettingsRoute /> },
      { path: THERE, element: <p>Task B content</p> },
    ],
  }], { initialEntries: [href] });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const view = render(<QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider>);
  return { router, onSaved, client, unmount: () => { view.unmount(); router.dispose(); client.clear(); } };
}

beforeEach(() => {
  window.sessionStorage.clear(); window.localStorage.clear();
  server.use(http.get("/api/v1/tasks/task-b/graph", () => HttpResponse.json({ detail: "unavailable" }, { status: 503 })));
});

describe("reviewed Profile settings", () => {
  it("uses finite descriptor controls and explicit inherit without executing metadata writes", async () => {
    let writes = 0;
    server.use(http.all(BASE + "/*", ({ request }) => { if (request.method !== "GET") writes += 1; return HttpResponse.json({}); }));
    mount();
    expect(screen.getByRole("textbox", { name: "模型" })).toHaveValue("");
    expect(screen.getByRole("textbox", { name: "模型" })).toHaveAttribute("placeholder", "继承运行环境默认值");
    expect(screen.getByRole("combobox", { name: "思考等级" })).toBeDisabled();
    expect(screen.getByRole("combobox", { name: "思考等级" })).toHaveAccessibleDescription("thinking_not_confirmed");
    expect(screen.getByText("thinking_not_confirmed")).toBeInTheDocument();
    expect(screen.getByRole("spinbutton", { name: "Temperature" })).toHaveAttribute("max", "2");
    expect(screen.getByRole("checkbox", { name: "Compact" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Mode" })).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: /JSON/ })).not.toBeInTheDocument();
    expect(writes).toBe(0);
  });

  it("saves one complete reviewed draft and original parent while the reply is held", async () => {
    let writes = 0, operationId = "";
    let release: (() => void) | undefined;
    const held = new Promise<void>(resolve => { release = resolve; });
    let written: ProfileDraft = PROFILE_DRAFT;
    server.use(
      http.post(BASE + `/profiles/${PROFILE_ID}/revisions`, async ({ request }) => {
        writes += 1;
        const body = await request.json() as { operationId: string; draft: ProfileDraft; expected_ref: typeof PROFILE_REF };
        operationId = body.operationId; written = body.draft;
        expect(body.expected_ref).toEqual(PROFILE_REF);
        expect(body.draft).toEqual({ ...PROFILE_DRAFT, instructions: "Reviewed instructions local draft" });
        await held;
        return HttpResponse.json(result(operationId));
      }),
      http.get(BASE + `/profiles/${PROFILE_ID}`, ({ request }) => { expect(new URL(request.url).searchParams.get("revision")).toBe("2"); return HttpResponse.json(profileAt(2, written)); }),
    );
    const { onSaved } = mount();
    await userEvent.type(screen.getByLabelText("指令"), " local draft");
    const submitButton = screen.getByRole("button", { name: "保存 Profile" });
    await userEvent.dblClick(submitButton);
    await waitFor(() => expect(writes).toBe(1));
    expect(submitButton).toBeDisabled();
    expect(operationId).not.toBe("");
    release?.();
    await screen.findByText("配置已保存 · 仅用于新安排");
    expect(onSaved).toHaveBeenCalledOnce();
    expect(onSaved).toHaveBeenCalledWith(profileAt(2, written));
    expect(writes).toBe(1);
    expect(window.sessionStorage.getItem(storageKey(WS))).not.toContain("local draft");
  });

  it("queries the original uncertain save and exact immutable revision without resending", async () => {
    let writes = 0, reads = 0, operationId = "";
    server.use(
      http.post(BASE + `/profiles/${PROFILE_ID}/revisions`, async ({ request }) => { writes += 1; operationId = (await request.json() as { operationId: string }).operationId; return HttpResponse.error(); }),
      http.get(`/product/v1/workspaces/${WS}/configuration-requests`, ({ request }) => { reads += 1; expect(new URL(request.url).searchParams.get("operationId")).toBe(operationId); return HttpResponse.json(result(operationId, "reconciled")); }),
      http.get(BASE + `/profiles/${PROFILE_ID}`, ({ request }) => { expect(new URL(request.url).searchParams.get("revision")).toBe("2"); return HttpResponse.json(profileAt(2, { ...PROFILE_DRAFT, instructions: "Reviewed instructions original draft" })); }),
    );
    mount();
    await userEvent.type(screen.getByLabelText("指令"), " original draft");
    await userEvent.click(screen.getByRole("button", { name: "保存 Profile" }));
    await screen.findByText("保存结果未确认");
    expect(screen.getByLabelText("指令")).toBeDisabled();
    const metadata = window.sessionStorage.getItem(storageKey(WS));
    expect(metadata).toContain(operationId);
    expect(metadata).not.toContain("original draft");
    await userEvent.click(screen.getByRole("button", { name: "查询原保存结果" }));
    await screen.findByText("已找到原配置 · 仅用于新安排");
    expect(writes).toBe(1); expect(reads).toBe(1);
  });

  it("reloads a pending safe operation by GET before reading its saved snapshot", async () => {
    let writes = 0;
    const reads: string[] = [];
    const original = "original pending save";
    server.use(
      http.post(BASE + `/profiles/${PROFILE_ID}/revisions`, () => { writes += 1; return HttpResponse.error(); }),
      http.get(`/product/v1/workspaces/${WS}/configuration-requests`, ({ request }) => { reads.push("request"); expect(new URL(request.url).searchParams.get("operationId")).toBe(original); return HttpResponse.json(result(original, "reconciled")); }),
      http.get(BASE + `/profiles/${PROFILE_ID}`, ({ request }) => { reads.push("profile"); expect(new URL(request.url).searchParams.get("revision")).toBe("2"); return HttpResponse.json(profileAt(2)); }),
    );
    mount({ recovery: { operationId: original, target_ref: savedRef } });
    await screen.findByText("已找到原配置 · 仅用于新安排");
    expect(reads).toEqual(["request", "profile"]);
    expect(writes).toBe(0);
  });

  it("persists only the original operation before a lost create reply and reloads without another POST", async () => {
    let writes = 0, reads = 0, operationId = "";
    let written: ProfileDraft = PROFILE_DRAFT;
    let metadataHeld = false, releaseMetadata: (() => void) | undefined;
    const delayedMetadata = new Promise<void>(resolve => { releaseMetadata = resolve; });
    server.use(
      http.get(BASE + "/runtimes", async () => { if (metadataHeld) await delayedMetadata; return HttpResponse.json({ items: [RUNTIME] }); }),
      http.get(BASE + "/runtime-descriptors", () => HttpResponse.json([DESCRIPTOR])),
      http.post(BASE + "/profiles", async ({ request }) => {
        writes += 1;
        const body = await request.json() as { operationId: string; draft: ProfileDraft };
        operationId = body.operationId; written = body.draft;
        const recovery = loadTabs(WS).tabs.find(tab => tab.id === "agent:new")?.configurationRecovery;
        expect(recovery).toEqual({ operationId });
        expect(window.sessionStorage.getItem(storageKey(WS))).not.toContain("private reviewed instructions");
        return HttpResponse.error();
      }),
      http.get(`/product/v1/workspaces/${WS}/configuration-requests`, ({ request }) => {
        reads += 1;
        expect(new URL(request.url).searchParams.get("operationId")).toBe(operationId);
        return HttpResponse.json({ operationId, state: "reconciled", target_ref: PROFILE_REF, result_ref: PROFILE_REF, reason_code: null });
      }),
      http.get(BASE + `/profiles/${PROFILE_ID}`, ({ request }) => { expect(new URL(request.url).searchParams.get("revision")).toBe("1"); return HttpResponse.json(profileAt(1, written)); }),
    );
    const original = mount({ profile: null, actualMetadata: true });
    await userEvent.type(await screen.findByLabelText("名称"), "Explicit new Profile");
    await userEvent.selectOptions(screen.getByLabelText("运行环境"), RUNTIME_ID);
    await userEvent.type(screen.getByLabelText("指令"), "private reviewed instructions");
    await userEvent.click(screen.getByRole("button", { name: "保存 Profile" }));
    await screen.findByText("保存结果未确认");
    const recovery = loadTabs(WS).tabs.find(tab => tab.id === "agent:new")?.configurationRecovery;
    expect(recovery).toEqual({ operationId });
    original.unmount();
    metadataHeld = true;
    mount({ profile: null, actualMetadata: true, ...(recovery ? { recovery } : {}) });
    await screen.findByText("正在读取配置元数据");
    expect(screen.queryByLabelText("名称")).not.toBeInTheDocument();
    expect(loadTabs(WS).tabs.find(tab => tab.id === "agent:new")?.configurationRecovery).toEqual({ operationId });
    expect(reads).toBe(0);
    releaseMetadata?.();
    await screen.findByText("已找到原配置 · 仅用于新安排");
    expect(writes).toBe(1); expect(reads).toBe(1);
  });

  it("retains conflict draft until explicit current-head review authorizes a new intent", async () => {
    let writes = 0, reads = 0;
    const parents: number[] = [];
    server.use(
      http.post(BASE + `/profiles/${PROFILE_ID}/revisions`, async ({ request }) => {
        writes += 1;
        const body = await request.json() as { operationId: string; expected_ref: typeof PROFILE_REF };
        parents.push(body.expected_ref.revision);
        return HttpResponse.json(result(body.operationId, "rejected"), { status: 409 });
      }),
      http.get(BASE + `/profiles/${PROFILE_ID}`, () => { reads += 1; return HttpResponse.json(profileAt(5, { ...PROFILE_DRAFT, name: "Current head" })); }),
    );
    mount();
    await userEvent.type(screen.getByLabelText("指令"), " local draft");
    await userEvent.click(screen.getByRole("button", { name: "保存 Profile" }));
    await screen.findByText("配置版本已变化");
    expect(screen.getByLabelText("指令")).toHaveValue("Reviewed instructions local draft");
    expect(screen.getByRole("button", { name: "保存 Profile" })).toBeDisabled();
    expect(reads).toBe(0);
    await userEvent.click(screen.getByRole("button", { name: "查看当前版本" }));
    await screen.findByText("Current head");
    expect(screen.getByLabelText("指令")).toHaveValue("Reviewed instructions local draft");
    expect(writes).toBe(1);
    await userEvent.click(screen.getByRole("button", { name: "以当前版本重新确认" }));
    await userEvent.click(screen.getByRole("button", { name: "保存 Profile" }));
    await waitFor(() => expect(writes).toBe(2));
    expect(parents).toEqual([1, 5]);
  });

  it("guards the production dirty form and cancels tab metadata changes on Stay", async () => {
    const { router } = mount();
    await userEvent.type(screen.getByLabelText("指令"), " local draft");
    const before = window.sessionStorage.getItem(storageKey(WS));
    await userEvent.click(screen.getByRole("button", { name: "Task tab" }));
    await screen.findByRole("dialog", { name: "未保存的配置" });
    await userEvent.click(screen.getByRole("button", { name: "留在此页" }));
    expect(router.state.location.pathname).toBe(HERE);
    expect(screen.getByLabelText("指令")).toHaveValue("Reviewed instructions local draft");
    expect(window.sessionStorage.getItem(storageKey(WS))).toBe(before);
    await userEvent.click(screen.getByRole("button", { name: "Task tab" }));
    await userEvent.click(await screen.findByRole("button", { name: "离开" }));
    await waitFor(() => expect(router.state.location.pathname).toBe(THERE));
  });

  it("guards unsettled intent on Leave without cancelling or replaying its write", async () => {
    let writes = 0;
    server.use(http.post(BASE + `/profiles/${PROFILE_ID}/revisions`, async ({ request }) => { writes += 1; return HttpResponse.json(result((await request.json() as { operationId: string }).operationId, "unknown"), { status: 202 }); }));
    const { router } = mount();
    await userEvent.type(screen.getByLabelText("指令"), " local draft");
    await userEvent.click(screen.getByRole("button", { name: "保存 Profile" }));
    await screen.findByText("保存结果未确认");
    await userEvent.click(screen.getByRole("button", { name: "Task tab" }));
    await screen.findByRole("dialog", { name: "保存结果未确认" });
    expect(router.state.location.pathname).toBe(HERE);
    await userEvent.click(screen.getByRole("button", { name: "离开" }));
    await waitFor(() => expect(router.state.location.pathname).toBe(THERE));
    expect(writes).toBe(1);
    expect(window.sessionStorage.getItem(storageKey(WS))).not.toContain("local draft");
  });

  it("warns on native unload only while dirty or unsettled", async () => {
    mount();
    const clean = new Event("beforeunload", { cancelable: true });
    act(() => { window.dispatchEvent(clean); });
    expect(clean.defaultPrevented).toBe(false);
    await userEvent.type(screen.getByLabelText("指令"), " local draft");
    const dirty = new Event("beforeunload", { cancelable: true });
    act(() => { window.dispatchEvent(dirty); });
    expect(dirty.defaultPrevented).toBe(true);
  });
});
