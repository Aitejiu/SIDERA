import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { createMemoryRouter, Link, RouterProvider } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { WorkspaceTabs, WorkspaceTabsProvider } from "@/app/WorkspaceTabs";
import { server } from "@/test/server";
import { DefaultRoleControls } from "./DefaultRoleControls";
import { PROFILE, PROFILE_ID, WORKSPACE_ID as WS } from "./testFixtures";
import type { Defaults, DefaultsBody, ProfileView } from "./types";

const PATH = `/product/v1/workspaces/${WS}/defaults`;
const HERE = `/workspaces/${WS}/agents`, THERE = `/workspaces/${WS}/tasks/owned-task`;
const EMPTY: Defaults = { workspace_id: WS, version: 0, main_profile_id: null, planner_profile_id: null, worker_profile_id: null };

function mount(profiles: ProfileView[] = [PROFILE]) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = createMemoryRouter([
    { path: HERE, element: <WorkspaceTabsProvider workspaceId={WS}><WorkspaceTabs workspaceId={WS} /><Link to={THERE}>前往 Task</Link><DefaultRoleControls workspaceId={WS} profiles={profiles} /></WorkspaceTabsProvider> },
    { path: THERE, element: <h1>Owned Task</h1> },
  ], { initialEntries: [HERE] });
  render(<QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider>);
  return router;
}

beforeEach(() => {
  window.sessionStorage.clear(); window.localStorage.clear();
  server.use(http.get(PATH, () => HttpResponse.json(EMPTY)));
});

describe("independent role defaults and exact CAS observation", () => {
  it("never chooses a first row and permits one enabled Profile for all three roles", async () => {
    let writes = 0;
    const foreign = { ...PROFILE, profile: { ...PROFILE.profile, ref: { ...PROFILE.profile.ref, workspace_id: "20000000-0000-0000-0000-000000000002" }, draft: { ...PROFILE.profile.draft, name: "Foreign Profile" } } };
    const disabled = { ...PROFILE, profile: { ...PROFILE.profile, ref: { ...PROFILE.profile.ref, profile_id: "30000000-0000-0000-0000-000000000002" }, draft: { ...PROFILE.profile.draft, name: "Disabled Profile" } }, status: { ...PROFILE.status, enabled: false } };
    server.use(http.put(PATH, async ({ request }) => {
      writes += 1;
      expect(await request.json()).toEqual({ expected_version: 0, main_profile_id: PROFILE_ID, planner_profile_id: PROFILE_ID, worker_profile_id: PROFILE_ID });
      return HttpResponse.json({ ...EMPTY, version: 1, main_profile_id: PROFILE_ID, planner_profile_id: PROFILE_ID, worker_profile_id: PROFILE_ID });
    }));
    mount([foreign, disabled, PROFILE]);
    for (const role of ["Main", "Planner", "Worker"]) {
      const select = await screen.findByLabelText(`默认 ${role}`);
      expect(select).toHaveValue("");
      await userEvent.selectOptions(select, PROFILE_ID);
    }
    expect(screen.queryByRole("option", { name: /Foreign Profile|Disabled Profile/ })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "保存默认安排" }));
    await screen.findByText("默认安排已保存 · 仅用于新安排");
    expect(writes).toBe(1); expect(screen.getByText("默认安排 · V1")).toBeInTheDocument();
  });

  it("retains a conflicted draft and uses a new CAS version only after explicit adoption", async () => {
    let writes = 0;
    server.use(
      http.get(PATH, () => HttpResponse.json(writes === 0 ? EMPTY : { ...EMPTY, version: 4 })),
      http.put(PATH, async ({ request }) => { writes += 1; const body = await request.json() as DefaultsBody; expect(body.expected_version).toBe(writes === 1 ? 0 : 4); return writes === 1 ? HttpResponse.json({ detail: { code: "version_conflict" } }, { status: 409 }) : HttpResponse.json({ ...EMPTY, ...body, version: 5 }); }),
    );
    mount();
    await userEvent.selectOptions(await screen.findByLabelText("默认 Main"), PROFILE_ID);
    await userEvent.click(screen.getByRole("button", { name: "保存默认安排" }));
    await screen.findByText("默认安排版本已变化");
    expect(screen.getByLabelText("默认 Main")).toHaveValue(PROFILE_ID);
    expect(screen.getByText("默认安排 · V0")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "读取默认安排当前状态" }));
    await screen.findByText("当前默认安排 · V4");
    expect(screen.getByLabelText("默认 Main")).toHaveValue(PROFILE_ID);
    await userEvent.click(screen.getByRole("button", { name: "采用当前默认安排" }));
    await userEvent.selectOptions(screen.getByLabelText("默认 Main"), PROFILE_ID);
    await userEvent.click(screen.getByRole("button", { name: "保存默认安排" }));
    await screen.findByText("默认安排已保存 · 仅用于新安排"); expect(writes).toBe(2);
  });

  it("keeps an unknown write guarded after current-state observation without resending", async () => {
    let writes = 0, reads = 0;
    server.use(
      http.get(PATH, () => { reads += 1; return HttpResponse.json(writes === 0 ? EMPTY : { ...EMPTY, version: 1, main_profile_id: PROFILE_ID }); }),
      http.put(PATH, () => { writes += 1; return HttpResponse.error(); }),
    );
    const router = mount();
    await userEvent.selectOptions(await screen.findByLabelText("默认 Main"), PROFILE_ID);
    await userEvent.click(screen.getByRole("button", { name: "保存默认安排" }));
    await screen.findByText("默认安排保存结果未确认");
    await userEvent.click(screen.getByRole("button", { name: "读取默认安排当前状态" }));
    await screen.findByText("当前默认安排 · V1");
    expect(screen.getByRole("button", { name: "保存默认安排" })).toBeDisabled();
    await userEvent.click(screen.getByRole("link", { name: "前往 Task" }));
    await userEvent.click(await screen.findByRole("button", { name: "留在此页" }));
    expect(router.state.location.pathname).toBe(HERE);
    expect(writes).toBe(1); expect(reads).toBe(2);
    await userEvent.click(screen.getByRole("button", { name: "采用当前默认安排" }));
    await userEvent.click(screen.getByRole("link", { name: "前往 Task" }));
    await screen.findByRole("heading", { name: "Owned Task" }); expect(writes).toBe(1);
  });

  it("protects held writer navigation and dispatches one reviewed defaults mutation", async () => {
    let release: (() => void) | undefined, writes = 0;
    const held = new Promise<void>(resolve => { release = resolve; });
    server.use(http.put(PATH, async () => { writes += 1; await held; return HttpResponse.json({ ...EMPTY, version: 1, main_profile_id: PROFILE_ID }); }));
    const router = mount();
    await userEvent.selectOptions(await screen.findByLabelText("默认 Main"), PROFILE_ID);
    await userEvent.dblClick(screen.getByRole("button", { name: "保存默认安排" }));
    await waitFor(() => expect(writes).toBe(1));
    await userEvent.click(screen.getByRole("link", { name: "前往 Task" }));
    await userEvent.click(await screen.findByRole("button", { name: "留在此页" }));
    expect(router.state.location.pathname).toBe(HERE);
    release?.();
    await screen.findByText("默认安排已保存 · 仅用于新安排"); expect(writes).toBe(1);
  });
});
