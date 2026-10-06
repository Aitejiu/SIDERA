import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { createMemoryRouter, Outlet, RouterProvider } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { WorkspaceTabs, WorkspaceTabsProvider, useWorkspaceTabs } from "@/app/WorkspaceTabs";
import { initialTabs, loadTabs, openTab, reduceTabs, saveTabs, storageKey } from "@/app/workspaceTabsState";
import { server } from "@/test/server";
import { RuntimeSettings } from "./RuntimeSettings";
import { DESCRIPTOR, RUNTIME, RUNTIME_ID, WORKSPACE_ID as WS } from "./testFixtures";

const BASE = `/agents/v1/workspaces/${WS}`;
const DETAIL = BASE + `/runtimes/${RUNTIME_ID}`;

function mount(detail = false, navigation = false) {
  const here = `/workspaces/${WS}/runtimes${detail ? `/${RUNTIME_ID}` : ""}`;
  const there = `/workspaces/${WS}/tasks/task-b`;
  function Destination() {
    const tabs = useWorkspaceTabs();
    return <><button onClick={() => tabs.activate("root-task:task-b")}>Task destination</button><Outlet /></>;
  }
  if (navigation) {
    const task = reduceTabs(initialTabs(WS), openTab({ id: "root-task:task-b", kind: "root-task", title: "Task B", href: there, closeable: true }));
    saveTabs(reduceTabs(task, openTab(detail ? { id: `runtime:${RUNTIME_ID}`, kind: "runtime", title: "Runtime", href: here, closeable: true } : { id: "runtimes", kind: "runtimes", title: "Runtimes", href: here, closeable: true })));
  }
  const router = createMemoryRouter(navigation ? [{
    element: <WorkspaceTabsProvider workspaceId={WS}><WorkspaceTabs workspaceId={WS} /><Destination /></WorkspaceTabsProvider>,
    children: [
      { path: detail ? "/workspaces/:workspaceId/runtimes/:runtimeId" : "/workspaces/:workspaceId/runtimes", element: <RuntimeSettings /> },
      { path: there, element: <p>Actual destination route</p> },
    ],
  }] : [{
    path: detail ? "/workspaces/:workspaceId/runtimes/:runtimeId" : "/workspaces/:workspaceId/runtimes",
    element: <WorkspaceTabsProvider workspaceId={WS}><WorkspaceTabs workspaceId={WS} /><RuntimeSettings /></WorkspaceTabsProvider>,
  }], { initialEntries: [here] });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider>);
  return { router, client };
}

beforeEach(() => {
  window.sessionStorage.clear();
  server.use(
    http.get(BASE + "/runtimes", () => HttpResponse.json({ items: [RUNTIME] })),
    http.get(BASE + "/runtime-descriptors", () => HttpResponse.json([DESCRIPTOR])),
    http.get(DETAIL, () => HttpResponse.json(RUNTIME)),
  );
});

describe("explicit runtime management", () => {
  it("reads installed, protocol, auth and limits separately without probing or starting", async () => {
    let writes = 0;
    server.use(http.post(BASE + "/*", () => { writes += 1; return HttpResponse.json({}); }));
    mount();
    expect(await screen.findByRole("link", { name: "controlled-settings · 1" })).toBeInTheDocument();
    expect(screen.getByText("已安装")).toBeInTheDocument();
    expect(screen.getByText("协议已就绪")).toBeInTheDocument();
    expect(screen.getByText("认证已就绪")).toBeInTheDocument();
    expect(screen.getByText("活跃 1 · 会话 2")).toBeInTheDocument();
    expect(writes).toBe(0);
  });

  it("discovers only explicitly and registers one reviewed absolute candidate with exact limits", async () => {
    let discoveries = 0, registrations = 0;
    server.use(
      http.post(BASE + "/runtimes/discovery", () => { discoveries += 1; return HttpResponse.json([{ harness_id: "controlled-settings", executable: "/owned/bin/runtime" }]); }),
      http.post(BASE + "/runtimes", async ({ request }) => {
        registrations += 1;
        expect(await request.json()).toEqual({ harness_id: "controlled-settings", executable: "/owned/bin/runtime", max_active: 1, max_sessions: 2 });
        return HttpResponse.json(RUNTIME, { status: 201 });
      }),
    );
    mount();
    await screen.findByRole("link", { name: "controlled-settings · 1" });
    expect(discoveries).toBe(0); expect(registrations).toBe(0);
    await userEvent.click(screen.getByRole("button", { name: "发现运行环境" }));
    await userEvent.click(await screen.findByRole("button", { name: "选择 /owned/bin/runtime" }));
    await userEvent.dblClick(screen.getByRole("button", { name: "登记运行环境" }));
    expect(await screen.findByText("登记结果已返回")).toBeInTheDocument();
    expect(discoveries).toBe(1); expect(registrations).toBe(1);
  });

  it("rejects relative manual executables before dispatch", async () => {
    let registrations = 0;
    server.use(http.post(BASE + "/runtimes", () => { registrations += 1; return HttpResponse.json(RUNTIME); }));
    mount();
    await screen.findByRole("link", { name: "controlled-settings · 1" });
    await userEvent.selectOptions(screen.getByLabelText("Harness"), "controlled-settings");
    await userEvent.type(screen.getByLabelText("可执行文件绝对路径"), "relative/runtime");
    await userEvent.click(screen.getByRole("button", { name: "登记运行环境" }));
    expect(screen.getByLabelText("可执行文件绝对路径")).toBeInvalid();
    expect(registrations).toBe(0);
  });

  it("saves one held capacity write using its original expected version", async () => {
    let writes = 0;
    let release: (() => void) | undefined;
    const held = new Promise<void>(resolve => { release = resolve; });
    server.use(http.put(DETAIL + "/limits", async ({ request }) => {
      writes += 1;
      expect(await request.json()).toEqual({ ref: RUNTIME.ref, expected_version: 1, max_active: 2, max_sessions: 3 });
      await held;
      return HttpResponse.json({ ...RUNTIME, version: 2, limits: { max_active: 2, max_sessions: 3 } });
    }));
    mount(true);
    const active = await screen.findByLabelText("活跃 Run 上限");
    await userEvent.clear(active); await userEvent.type(active, "2");
    await userEvent.clear(screen.getByLabelText("原生会话上限")); await userEvent.type(screen.getByLabelText("原生会话上限"), "3");
    const submit = screen.getByRole("button", { name: "保存容量" });
    await userEvent.dblClick(submit);
    await waitFor(() => expect(writes).toBe(1));
    expect(submit).toBeDisabled();
    release?.();
    expect(await screen.findByText("容量已保存" )).toBeInTheDocument();
    expect(writes).toBe(1);
  });

  it("retains a conflicted draft and parent until an explicit current-version review", async () => {
    const versions: number[] = [];
    let reads = 0;
    server.use(
      http.put(DETAIL + "/limits", async ({ request }) => {
        const body = await request.json() as { expected_version: number; max_active: number };
        versions.push(body.expected_version);
        expect(body.max_active).toBe(2);
        return versions.length === 1 ? HttpResponse.json({ detail: { code: "version_conflict" } }, { status: 409 }) : HttpResponse.json({ ...RUNTIME, version: 5, limits: { max_active: 2, max_sessions: 2 } });
      }),
      http.get(DETAIL, () => { reads += 1; return HttpResponse.json(reads === 1 ? RUNTIME : { ...RUNTIME, version: 4 }); }),
    );
    mount(true);
    const active = await screen.findByLabelText("活跃 Run 上限");
    await userEvent.clear(active); await userEvent.type(active, "2");
    await userEvent.click(screen.getByRole("button", { name: "保存容量" }));
    await screen.findByText("配置版本已变化");
    expect(active).toHaveValue(2); expect(versions).toEqual([1]);
    expect(screen.getByRole("button", { name: "保存容量" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "读取当前状态" }));
    await userEvent.click(await screen.findByRole("button", { name: "以读取版本重新确认" }));
    expect(active).toHaveValue(2);
    await userEvent.click(screen.getByRole("button", { name: "保存容量" }));
    await screen.findByText("容量已保存");
    expect(versions).toEqual([1, 4]);
  });

  it("observes the exact runtime after a lost status reply without proving or resending the writer", async () => {
    let writes = 0, reads = 0;
    server.use(
      http.put(DETAIL + "/status", async ({ request }) => {
        writes += 1;
        expect(await request.json()).toEqual({ ref: RUNTIME.ref, enabled: false, expected_version: 1 });
        return HttpResponse.error();
      }),
      http.get(DETAIL, () => { reads += 1; return HttpResponse.json(reads === 1 ? RUNTIME : { ...RUNTIME, enabled: false, version: 2 }); }),
    );
    mount(true);
    await userEvent.click(await screen.findByRole("button", { name: "停用运行环境" }));
    await screen.findByText("操作结果未确认");
    await userEvent.click(screen.getByRole("button", { name: "读取当前状态" }));
    expect(await screen.findByText("当前已读取 · V2")).toBeInTheDocument();
    expect(screen.getByText("操作结果未确认")).toBeInTheDocument();
    expect(screen.queryByText("状态已保存")).not.toBeInTheDocument();
    expect(writes).toBe(1); expect(reads).toBe(2);
  });

  it("keeps explicit refresh separate from native model execution", async () => {
    let refreshes = 0, starts = 0;
    server.use(
      http.post(DETAIL + "/refresh", async ({ request }) => { refreshes += 1; expect(await request.json()).toEqual({ ref: RUNTIME.ref }); return HttpResponse.json({ ...RUNTIME, protocol_state: "unconfirmed", auth_state: "unconfirmed" }); }),
      http.post("/main-agent/v1/threads", () => { starts += 1; return HttpResponse.json({}); }),
    );
    mount(true);
    await userEvent.click(await screen.findByRole("button", { name: "检查运行环境" }));
    expect(await screen.findByText("运行环境已刷新")).toBeInTheDocument();
    expect(screen.getByText("协议未确认")).toBeInTheDocument();
    expect(screen.getByText("认证未确认")).toBeInTheDocument();
    expect(refreshes).toBe(1); expect(starts).toBe(0);
  });

  it("does not show missing runtime metadata as an empty installation", async () => {
    server.use(http.get(BASE + "/runtimes", () => HttpResponse.json({ detail: { code: "temporary_unavailable" } }, { status: 503 })));
    mount();
    expect(await screen.findByText("运行环境目录不可用")).toBeInTheDocument();
    expect(screen.queryByText("尚无运行环境")).not.toBeInTheDocument();
  });
});

describe("held original runtime navigation", () => {
  it.each(["status", "refresh"] as const)("guards the unchanged runtime while its original %s write is held", async kind => {
    let writes = 0;
    let release: (() => void) | undefined;
    const held = new Promise<void>(resolve => { release = resolve; });
    server.use(http[kind === "status" ? "put" : "post"](DETAIL + `/${kind}`, async ({ request }) => {
      writes += 1;
      expect(await request.json()).toEqual(kind === "status" ? { ref: RUNTIME.ref, expected_version: 1, enabled: false } : { ref: RUNTIME.ref });
      await held;
      return HttpResponse.json(kind === "status" ? { ...RUNTIME, enabled: false, version: 2 } : RUNTIME);
    }));
    const { router } = mount(true, true);
    try {
      await screen.findByLabelText("活跃 Run 上限");
      await waitFor(() => expect(loadTabs(WS).activeTabId).toBe(`runtime:${RUNTIME_ID}`));
      const before = window.sessionStorage.getItem(storageKey(WS));
      await userEvent.click(screen.getByRole("button", { name: kind === "status" ? "停用运行环境" : "检查运行环境" }));
      await waitFor(() => expect(writes).toBe(1));
      await userEvent.click(screen.getByRole("button", { name: "Task destination" }));
      await screen.findByRole("dialog", { name: "保存结果未确认" });
      await userEvent.click(screen.getByRole("button", { name: "留在此页" }));
      expect(router.state.location.pathname).toBe(`/workspaces/${WS}/runtimes/${RUNTIME_ID}`);
      expect(screen.getByLabelText("活跃 Run 上限")).toHaveValue(1);
      expect(loadTabs(WS).activeTabId).toBe(`runtime:${RUNTIME_ID}`);
      expect(window.sessionStorage.getItem(storageKey(WS))).toBe(before);
      expect(writes).toBe(1);
      release?.();
      await screen.findByText(kind === "status" ? "状态已保存" : "运行环境已刷新");
      await userEvent.click(screen.getByRole("button", { name: "Task destination" }));
      await screen.findByText("Actual destination route");
      expect(router.state.location.pathname).toBe(`/workspaces/${WS}/tasks/task-b`);
      expect(writes).toBe(1);
    } finally { release?.(); }
  });

  it("guards the original registration as uncertain, retaining its exact draft and one writer", async () => {
    let writes = 0;
    let release: (() => void) | undefined;
    const held = new Promise<void>(resolve => { release = resolve; });
    server.use(http.post(BASE + "/runtimes", async ({ request }) => {
      writes += 1;
      expect(await request.json()).toEqual({ harness_id: "controlled-settings", executable: "/owned/bin/runtime", max_active: 1, max_sessions: 2 });
      await held;
      return HttpResponse.json(RUNTIME, { status: 201 });
    }));
    const { router } = mount(false, true);
    try {
      await screen.findByRole("link", { name: "controlled-settings · 1" });
      await userEvent.selectOptions(screen.getByLabelText("Harness"), "controlled-settings");
      await userEvent.type(screen.getByLabelText("可执行文件绝对路径"), "/owned/bin/runtime");
      await waitFor(() => expect(loadTabs(WS).activeTabId).toBe("runtimes"));
      const before = window.sessionStorage.getItem(storageKey(WS));
      await userEvent.click(screen.getByRole("button", { name: "登记运行环境" }));
      await waitFor(() => expect(writes).toBe(1));
      await userEvent.click(screen.getByRole("button", { name: "Task destination" }));
      await screen.findByRole("dialog", { name: "保存结果未确认" });
      expect(screen.queryByRole("dialog", { name: "未保存的配置" })).not.toBeInTheDocument();
      await userEvent.click(screen.getByRole("button", { name: "留在此页" }));
      expect(screen.getByLabelText("可执行文件绝对路径")).toHaveValue("/owned/bin/runtime");
      expect(router.state.location.pathname).toBe(`/workspaces/${WS}/runtimes`);
      expect(window.sessionStorage.getItem(storageKey(WS))).toBe(before);
      expect(writes).toBe(1);
      release?.();
      await screen.findByText("登记结果已返回");
      await userEvent.click(screen.getByRole("button", { name: "Task destination" }));
      await screen.findByText("Actual destination route");
      expect(writes).toBe(1);
    } finally { release?.(); }
  });
});
