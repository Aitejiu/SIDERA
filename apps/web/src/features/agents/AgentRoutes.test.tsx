import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { RouterProvider } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { createTestRouter } from "@/app/router";
import { loadTabs, storageKey } from "@/app/workspaceTabsState";
import { server } from "@/test/server";
import { DESCRIPTOR, PROFILE, PROFILE_ID, PROFILE_WORK, RUNTIME, WORKSPACE_ID as WS } from "./testFixtures";

const BASE = `/agents/v1/workspaces/${WS}`;

function mount(path: string) {
  const router = createTestRouter([path]);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider>);
  return router;
}

beforeEach(() => {
  window.sessionStorage.clear();
  server.use(
    http.get(BASE + "/profiles", () => HttpResponse.json({ items: [PROFILE] })),
    http.get(BASE + `/profiles/${PROFILE_ID}`, () => HttpResponse.json(PROFILE)),
    http.get(BASE + "/runtimes", () => HttpResponse.json({ items: [RUNTIME] })),
    http.get(BASE + "/runtime-descriptors", () => HttpResponse.json([DESCRIPTOR])),
    http.get(BASE + `/profiles/${PROFILE_ID}/validation`, () => HttpResponse.json({ valid: true, issues: [], effective_preview: null })),
    http.get(BASE + `/profiles/${PROFILE_ID}/work`, () => HttpResponse.json(PROFILE_WORK)),
  );
});

describe("additive agents application routes", () => {
  it("mounts the actual directory and preserves the existing Task-first navigation", async () => {
    let writes = 0;
    server.use(http.post(BASE + "/*", () => { writes += 1; return HttpResponse.json({}); }));
    mount(`/workspaces/${WS}/agents`);
    expect(await screen.findByRole("heading", { name: "智能体" })).toBeInTheDocument();
    const rail = screen.getByRole("complementary", { name: "主导航" });
    expect(within(rail).getByRole("link", { name: "任务" })).toHaveAttribute("href", `/workspaces/${WS}/tasks`);
    expect(within(rail).getByRole("link", { name: "智能体" })).toHaveAttribute("href", `/workspaces/${WS}/agents`);
    expect(within(rail).getByRole("link", { name: "运行环境" })).toHaveAttribute("href", `/workspaces/${WS}/runtimes`);
    expect(within(rail).getByRole("link", { name: "执行节点" })).toBeInTheDocument();
    await screen.findByRole("link", { name: "Reusable reviewer" });
    expect(loadTabs(WS).activeTabId).toBe("agents");
    expect(writes).toBe(0);
  });

  it("keeps the actual settings draft and selected semantic tab when sidebar navigation is cancelled", async () => {
    const path = `/workspaces/${WS}/agents/${PROFILE_ID}?view=settings`;
    const router = mount(path);
    await userEvent.type(await screen.findByLabelText("指令"), " unsaved instructions");
    await waitFor(() => expect(loadTabs(WS).activeTabId).toBe(`agent:${PROFILE_ID}`));
    const before = window.sessionStorage.getItem(storageKey(WS));
    await userEvent.click(within(screen.getByRole("complementary", { name: "主导航" })).getByRole("link", { name: "任务" }));
    await screen.findByRole("dialog", { name: "未保存的配置" });
    await userEvent.click(screen.getByRole("button", { name: "留在此页" }));
    expect(router.state.location.pathname + router.state.location.search).toBe(path);
    expect(screen.getByLabelText("指令")).toHaveValue("Reviewed instructions unsaved instructions");
    expect(window.sessionStorage.getItem(storageKey(WS))).toBe(before);
  });
});
