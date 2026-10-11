import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { useEffect, useRef, useState } from "react";
import { createMemoryRouter, Link, Outlet, redirect, RouterProvider, useBlocker, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { server } from "@/test/server";
import { WorkspaceTabs, WorkspaceTabsProvider, useWorkspaceTabs } from "./WorkspaceTabs";
import { initialTabs, openTab, reduceTabs, saveTabs, storageKey, type ConfigurationRecovery, type WorkspaceTab } from "./workspaceTabsState";

const WS = "tab-test-workspace";
const A = `/workspaces/${WS}/agents/a`;
const B = `/workspaces/${WS}/agents/b`;
const TASK = `/workspaces/${WS}/tasks/task-b`;
const TASK_C = `/workspaces/${WS}/tasks/task-c`;
const agent = (id: string): WorkspaceTab => ({ id: `agent:${id}`, kind: "agent", title: `Profile ${id}`, href: `/workspaces/${WS}/agents/${id}`, closeable: true });
const ORIGINAL_RECOVERY: ConfigurationRecovery = { operationId: "original/create 1" };
let writes = 0;

function Shell() {
  const tabs = useWorkspaceTabs();
  const location = useLocation();
  return <>
    <WorkspaceTabs workspaceId={WS} />
    <output aria-label="current route">{location.pathname}</output>
    <output aria-label="selected tab">{tabs.state.activeTabId}</output>
    <button onClick={() => tabs.activate("root-task:task-b")}>Activate task</button>
    <button onClick={() => tabs.open(agent("b"))}>Open B</button>
    <button onClick={() => tabs.close("agent:a")}>Close A</button>
    <button onClick={() => tabs.close("root-task:task-b")}>Close inactive task</button>
    <button onClick={() => tabs.close("agent:other")}>Close inactive other</button>
    <button onClick={() => tabs.activate("agent:a")}>Activate same A</button>
    <button onClick={() => {
      const tab = tabs.state.tabs.find(item => item.id === "agent:a");
      if (tab) tabs.registerDirectRoute({ ...tab, configurationRecovery: ORIGINAL_RECOVERY });
    }}>Remember original recovery</button>
    <button onClick={() => {
      const source = tabs.state.tabs.find(item => item.id === "agent:a");
      if (!source) return;
      const tab = { ...source };
      delete tab.configurationRecovery;
      tabs.registerDirectRoute(tab);
    }}>Clear original recovery</button>
    <Link to={TASK}>Sidebar task</Link>
    <Outlet />
  </>;
}

function Editor() {
  const [draft, setDraft] = useState("Reviewed instructions");
  const tabs = useWorkspaceTabs();
  const blocker = useBlocker(({ currentLocation, nextLocation }) => draft !== "Reviewed instructions"
    && `${currentLocation.pathname}${currentLocation.search}` !== `${nextLocation.pathname}${nextLocation.search}`);
  const original = useRef<typeof blocker | null>(null);
  useEffect(() => {
    if (blocker.state === "blocked" && original.current === null) original.current = blocker;
  }, [blocker]);
  return <>
    <label>Instructions<input value={draft} onChange={event => setDraft(event.target.value)} /></label>
    {blocker.state === "blocked" ? <div role="dialog" aria-label="Unsaved changes">
      <button onClick={() => { original.current?.reset?.(); original.current = null; tabs.cancelPendingNavigation?.(); }}>Stay</button>
      <button onClick={() => { original.current?.proceed?.(); original.current = null; }}>Leave</button>
    </div> : null}
  </>;
}

function mount(options: { holdB?: () => Promise<null>; redirectB?: boolean; unrelated?: boolean; recovery?: ConfigurationRecovery } = {}) {
  let state = initialTabs(WS);
  if (options.unrelated) state = reduceTabs(state, openTab(agent("other")));
  state = reduceTabs(state, openTab({ id: "root-task:task-b", kind: "root-task", title: "Task B", href: TASK, closeable: true }));
  state = reduceTabs(state, openTab(options.recovery ? { ...agent("a"), title: "Reviewed Profile A", description: "Reviewed description", configurationRecovery: options.recovery } : agent("a")));
  saveTabs(state);
  const router = createMemoryRouter([{
    element: <WorkspaceTabsProvider workspaceId={WS}><Shell /></WorkspaceTabsProvider>,
    children: [
      { path: A, element: <Editor /> },
      { path: B, element: <p>Profile B</p>, loader: options.redirectB ? () => redirect(TASK_C) : options.holdB },
      { path: TASK, element: <p>Task B content</p> },
      { path: TASK_C, element: <p>Task C content</p> },
      { path: `/workspaces/${WS}/tasks`, element: <p>Task directory</p> },
    ],
  }], { initialEntries: [TASK, A], initialIndex: 1 });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider>);
  return router;
}

async function dirty() {
  const input = await screen.findByRole("textbox", { name: "Instructions" });
  await userEvent.type(input, " local draft");
  return input;
}

function serialized() { return window.sessionStorage.getItem(storageKey(WS)); }

function savedTabs(): { tabs: WorkspaceTab[]; activeTabId: string } {
  return JSON.parse(serialized() ?? "null");
}

function unavailableTaskMetadata() {
  for (const id of ["task-b", "task-c"]) {
    server.use(http.get(`/api/v1/tasks/${id}/graph`, () => HttpResponse.json({ detail: "unavailable" }, { status: 503 })));
  }
}

beforeEach(() => {
  window.sessionStorage.clear();
  writes = 0;
  server.use(http.all("/api/v1/*", ({ request }) => {
    if (request.method !== "GET") writes += 1;
    return HttpResponse.json({ overview: { title: "Task B" }, nodes: [] });
  }));
});

describe("atomic blocked tab navigation", () => {
  it("preserves original recovery and display metadata through actual generic agent registration", async () => {
    unavailableTaskMetadata();
    mount({ recovery: ORIGINAL_RECOVERY });
    await screen.findByLabelText("Instructions");
    await waitFor(() => expect(savedTabs().tabs.find(tab => tab.id === "agent:a")?.configurationRecovery).toEqual(ORIGINAL_RECOVERY));
    expect(savedTabs().activeTabId).toBe("agent:a");
    expect(savedTabs().tabs.find(tab => tab.id === "agent:a")).toMatchObject({ title: "Reviewed Profile A", description: "Reviewed description", returnTabId: "root-task:task-b" });
    expect(screen.getByRole("link", { name: "Reviewed Profile A" })).toBeInTheDocument();
    expect(writes).toBe(0);
  });

  it("keeps explicit original recovery clear after later actual generic registration", async () => {
    const router = mount();
    await userEvent.click(screen.getByRole("button", { name: "Remember original recovery" }));
    await waitFor(() => expect(savedTabs().tabs.find(tab => tab.id === "agent:a")?.configurationRecovery).toEqual(ORIGINAL_RECOVERY));
    await userEvent.click(screen.getByRole("button", { name: "Clear original recovery" }));
    await act(async () => { await router.navigate(A + "?view=settings"); });
    await waitFor(() => expect(savedTabs().tabs.find(tab => tab.id === "agent:a")?.href).toBe(A + "?view=settings"));
    expect(savedTabs().tabs.find(tab => tab.id === "agent:a")?.configurationRecovery).toBeUndefined();
    expect(serialized()).not.toContain(ORIGINAL_RECOVERY.operationId);
    expect(writes).toBe(0);
  });

  it("aligns the committed destination after an inactive activation target was closed", async () => {
    unavailableTaskMetadata();
    const router = mount({ unrelated: true });
    const input = await dirty();
    await userEvent.click(screen.getByRole("button", { name: "Activate task" }));
    await screen.findByRole("dialog");
    await userEvent.click(screen.getByRole("button", { name: "Close inactive task" }));
    await userEvent.click(screen.getByRole("button", { name: "Close inactive other" }));
    expect(router.state.location.pathname).toBe(A);
    expect(screen.getByLabelText("selected tab")).toHaveTextContent("agent:a");
    expect(input).toHaveValue("Reviewed instructions local draft");
    expect(savedTabs().tabs.map(tab => tab.id)).not.toContain("root-task:task-b");
    await userEvent.click(screen.getByRole("button", { name: "Leave" }));
    await waitFor(() => expect(router.state.location.pathname).toBe(TASK));
    expect(screen.getByLabelText("selected tab")).toHaveTextContent("root-task:task-b");
    const saved = savedTabs();
    expect(saved.activeTabId).toBe("root-task:task-b");
    expect(saved.tabs.filter(tab => tab.id === "root-task:task-b")).toHaveLength(1);
    expect(saved.tabs.map(tab => tab.id)).toContain("agent:a");
    expect(saved.tabs.map(tab => tab.id)).not.toContain("agent:other");
    expect(writes).toBe(0);
  });

  it("aligns the committed destination when active-source close outlives its inactive return target", async () => {
    unavailableTaskMetadata();
    const router = mount({ unrelated: true });
    const input = await dirty();
    await userEvent.click(screen.getByRole("button", { name: "Close A" }));
    await screen.findByRole("dialog");
    await userEvent.click(screen.getByRole("button", { name: "Close inactive task" }));
    await userEvent.click(screen.getByRole("button", { name: "Close inactive other" }));
    expect(router.state.location.pathname).toBe(A);
    expect(screen.getByLabelText("selected tab")).toHaveTextContent("agent:a");
    expect(input).toHaveValue("Reviewed instructions local draft");
    expect(savedTabs().tabs.filter(tab => tab.id === "agent:a")).toHaveLength(1);
    await userEvent.click(screen.getByRole("button", { name: "Leave" }));
    await waitFor(() => expect(router.state.location.pathname).toBe(TASK));
    expect(screen.getByLabelText("selected tab")).toHaveTextContent("root-task:task-b");
    const saved = savedTabs();
    expect(saved.activeTabId).toBe("root-task:task-b");
    expect(saved.tabs.filter(tab => tab.id === "root-task:task-b")).toHaveLength(1);
    expect(saved.tabs.map(tab => tab.id)).not.toContain("agent:a");
    expect(saved.tabs.map(tab => tab.id)).not.toContain("agent:other");
    expect(writes).toBe(0);
  });

  it("aligns a different committed destination after the actual loader redirects", async () => {
    unavailableTaskMetadata();
    const router = mount({ redirectB: true, unrelated: true });
    await dirty();
    await userEvent.click(screen.getByRole("button", { name: "Open B" }));
    await screen.findByRole("dialog");
    await userEvent.click(screen.getByRole("button", { name: "Close inactive other" }));
    await userEvent.click(screen.getByRole("button", { name: "Leave" }));
    await waitFor(() => expect(router.state.location.pathname).toBe(TASK_C));
    expect(screen.getByLabelText("selected tab")).toHaveTextContent("root-task:task-c");
    await waitFor(() => expect(savedTabs().activeTabId).toBe("root-task:task-c"));
    const saved = savedTabs();
    expect(saved.activeTabId).toBe("root-task:task-c");
    expect(saved.tabs.filter(tab => tab.id === "root-task:task-c")).toHaveLength(1);
    expect(saved.tabs.map(tab => tab.id)).toContain("agent:a");
    expect(saved.tabs.map(tab => tab.id)).not.toContain("agent:b");
    expect(saved.tabs.map(tab => tab.id)).not.toContain("agent:other");
    expect(writes).toBe(0);
  });

  it("retains active Profile, URL, draft and persisted tabs after Task activation is cancelled", async () => {
    const router = mount();
    const input = await dirty();
    const before = serialized();
    await userEvent.click(screen.getByRole("button", { name: "Activate task" }));
    await screen.findByRole("dialog");
    await userEvent.click(screen.getByRole("button", { name: "Stay" }));
    expect(router.state.location.pathname).toBe(A);
    expect(screen.getByLabelText("selected tab")).toHaveTextContent("agent:a");
    expect(input).toHaveValue("Reviewed instructions local draft");
    expect(serialized()).toBe(before);
    expect(writes).toBe(0);
  });

  it("does not remove active A on Stay and commits its original return target exactly once on Leave", async () => {
    const router = mount();
    await dirty();
    const before = serialized();
    await userEvent.click(screen.getByRole("button", { name: "Close A" }));
    await screen.findByRole("dialog");
    await userEvent.click(screen.getByRole("button", { name: "Stay" }));
    expect(serialized()).toBe(before);
    await userEvent.click(screen.getByRole("button", { name: "Close A" }));
    await userEvent.click(await screen.findByRole("button", { name: "Leave" }));
    await waitFor(() => expect(router.state.location.pathname).toBe(TASK));
    expect(JSON.parse(serialized() ?? "null").tabs.map((tab: WorkspaceTab) => tab.id)).not.toContain("agent:a");
    expect(screen.getByLabelText("selected tab")).toHaveTextContent("root-task:task-b");
    expect(writes).toBe(0);
  });

  it("creates no phantom B on Stay, then commits B with A as return target only after Leave", async () => {
    const router = mount();
    await dirty();
    const before = serialized();
    await userEvent.click(screen.getByRole("button", { name: "Open B" }));
    await userEvent.click(await screen.findByRole("button", { name: "Stay" }));
    expect(serialized()).toBe(before);
    await userEvent.click(screen.getByRole("button", { name: "Open B" }));
    await userEvent.click(await screen.findByRole("button", { name: "Leave" }));
    await waitFor(() => expect(router.state.location.pathname).toBe(B));
    expect(JSON.parse(serialized() ?? "null").tabs.find((tab: WorkspaceTab) => tab.id === "agent:b").returnTabId).toBe("agent:a");
    expect(writes).toBe(0);
  });

  it("keeps the first reviewed destination when another tab is clicked while blocked", async () => {
    const router = mount();
    await dirty();
    await userEvent.click(screen.getByRole("button", { name: "Open B" }));
    await screen.findByRole("dialog");
    await userEvent.click(screen.getByRole("button", { name: "Activate task" }));
    await userEvent.click(screen.getByRole("button", { name: "Leave" }));
    await waitFor(() => expect(router.state.location.pathname).toBe(B));
    expect(screen.getByLabelText("selected tab")).toHaveTextContent("agent:b");
    expect(writes).toBe(0);
  });

  it("closes inactive Task without discarding dirty A and same-href activation leaves no pending intent", async () => {
    const router = mount();
    const input = await dirty();
    await userEvent.click(screen.getByRole("button", { name: "Close inactive task" }));
    expect(router.state.location.pathname).toBe(A);
    expect(input).toHaveValue("Reviewed instructions local draft");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Activate same A" }));
    await userEvent.click(screen.getByRole("button", { name: "Open B" }));
    await userEvent.click(await screen.findByRole("button", { name: "Leave" }));
    await waitFor(() => expect(router.state.location.pathname).toBe(B));
    expect(JSON.parse(serialized() ?? "null").tabs.map((tab: WorkspaceTab) => tab.id)).not.toContain("root-task:task-b");
    expect(writes).toBe(0);
  });

  it.each(["back", "sidebar", "direct"])("retains the same draft and metadata when %s navigation is cancelled", async (kind) => {
    const router = mount();
    const input = await dirty();
    const before = serialized();
    if (kind === "sidebar") await userEvent.click(screen.getByRole("link", { name: "Sidebar task" }));
    else await act(async () => { await router.navigate(kind === "back" ? -1 : TASK); });
    await userEvent.click(await screen.findByRole("button", { name: "Stay" }));
    expect(router.state.location.pathname).toBe(A);
    expect(input).toHaveValue("Reviewed instructions local draft");
    expect(serialized()).toBe(before);
    expect(writes).toBe(0);
  });

  it("does not publish a phantom tab while a destination loader is held then aborted", async () => {
    let release: (() => void) | undefined;
    const held = new Promise<null>(resolve => { release = () => resolve(null); });
    const router = mount({ holdB: () => held });
    await screen.findByRole("textbox");
    const before = serialized();
    await userEvent.click(screen.getByRole("button", { name: "Open B" }));
    expect(router.state.location.pathname).toBe(A);
    expect(serialized()).toBe(before);
    await act(async () => { await router.navigate(A); release?.(); });
    expect(router.state.location.pathname).toBe(A);
    expect(serialized()).toBe(before);
    expect(writes).toBe(0);
  });
});
