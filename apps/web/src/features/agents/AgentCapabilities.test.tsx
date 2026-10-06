import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { useState } from "react";
import { createMemoryRouter, Link, RouterProvider } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { WorkspaceTabs, WorkspaceTabsProvider } from "@/app/WorkspaceTabs";
import { initialTabs, openTab, reduceTabs, saveTabs } from "@/app/workspaceTabsState";
import { server } from "@/test/server";
import { AgentCapabilities } from "./AgentCapabilities";
import { DESCRIPTOR, PROFILE, PROFILE_DRAFT, PROFILE_ID, PROFILE_REF, RESOURCE, RESOURCE_DRAFT, RESOURCE_REF, RUNTIME, WORKSPACE_ID as WS, profileAt, resourceAt } from "./testFixtures";
import type { ProfileDraft, ProfileView, ResourceDraft } from "./types";

const BASE = `/agents/v1/workspaces/${WS}`;
const HERE = `/workspaces/${WS}/agents/${PROFILE_ID}`;
const THERE = `/workspaces/${WS}/tasks/owned-task`;

function mount(capability = "skill", profile = profileAt(1, { ...PROFILE_DRAFT, resource_refs: [RESOURCE_REF] })) {
  const href = HERE + `?view=capabilities&capability=${capability}`;
  saveTabs(reduceTabs(initialTabs(WS), openTab({ id: `agent:${PROFILE_ID}`, kind: "agent", title: "Reviewer", href, closeable: true })));
  function Page() {
    const [value, setValue] = useState<ProfileView>(profile);
    return <><Link to={THERE}>前往 Task</Link><AgentCapabilities workspaceId={WS} profile={value} runtimes={[RUNTIME]} descriptors={[DESCRIPTOR]} onSaved={setValue} /></>;
  }
  const router = createMemoryRouter([{ path: HERE, element: <WorkspaceTabsProvider workspaceId={WS}><WorkspaceTabs workspaceId={WS} /><Page /></WorkspaceTabsProvider> }, { path: THERE, element: <h1>Owned Task</h1> }], { initialEntries: [href] });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider>);
  return { router };
}

beforeEach(() => {
  window.sessionStorage.clear(); window.localStorage.clear();
  server.use(
    http.get(BASE + "/resources", () => HttpResponse.json({ items: [resourceAt(2, { ...RESOURCE_DRAFT, name: "New resource head" })] })),
    http.get(BASE + `/resources/${RESOURCE_REF.resource_id}`, ({ request }) => HttpResponse.json(new URL(request.url).searchParams.get("revision") === "1" ? RESOURCE : resourceAt(2))),
    http.get(BASE + `/profiles/${PROFILE_ID}/validation`, () => HttpResponse.json({ valid: false, issues: [{ code: "unsupported_capability", field: "resource_refs", message: "Skill effect unconfirmed" }], effective_preview: null })),
    http.get(`/model-config/v1/workspaces/${WS}/credentials`, () => HttpResponse.json([])),
  );
});

describe("independent resources and immutable Profile capability bindings", () => {
  it("keeps all ability views visible and separates missing support, structural issues and configured pins", async () => {
    let writes = 0;
    server.use(http.post(BASE + "/*", () => { writes += 1; return HttpResponse.json({}); }));
    mount();
    const nav = await screen.findByRole("navigation", { name: "能力类型" });
    for (const label of ["指令", "Skills", "MCP", "Integrations"]) expect(nav).toHaveTextContent(label);
    expect(await screen.findByText("能力支持未确认")).toBeInTheDocument();
    expect(await screen.findByText("resource_refs · unsupported_capability")).toBeInTheDocument();
    expect(await screen.findByText("Reviewed skill · 当前绑定 R1")).toBeInTheDocument();
    expect(await screen.findByText("New resource head · R2")).toBeInTheDocument(); expect(writes).toBe(0);
  });

  it("saves instructions as a full original Profile revision, not a resource write", async () => {
    let writes = 0;
    const saved = { ...PROFILE_DRAFT, instructions: "Reviewed instructions updated" };
    server.use(
      http.post(BASE + `/profiles/${PROFILE_ID}/revisions`, async ({ request }) => { writes += 1; const body = await request.json() as { operationId: string; draft: ProfileDraft; expected_ref: typeof PROFILE_REF }; expect(body.expected_ref).toEqual(PROFILE_REF); expect(body.draft).toEqual(saved); return HttpResponse.json({ operationId: body.operationId, state: "succeeded", target_ref: { ...PROFILE_REF, revision: 2 }, result_ref: { ...PROFILE_REF, revision: 2 }, reason_code: null }); }),
      http.get(BASE + `/profiles/${PROFILE_ID}`, () => HttpResponse.json(profileAt(2, saved))),
    );
    mount("instructions", PROFILE);
    await userEvent.type(await screen.findByLabelText("指令"), " updated");
    await userEvent.click(screen.getByRole("button", { name: "保存 Profile" }));
    await screen.findByText("配置已保存 · 仅用于新安排"); expect(writes).toBe(1);
  });

  it("only replaces a pinned resource revision after an explicit bind and Profile save", async () => {
    let writes = 0;
    const next = { ...RESOURCE_REF, revision: 2 };
    const saved = { ...PROFILE_DRAFT, resource_refs: [next] };
    server.use(
      http.post(BASE + `/profiles/${PROFILE_ID}/revisions`, async ({ request }) => { writes += 1; const body = await request.json() as { operationId: string; draft: ProfileDraft; expected_ref: typeof PROFILE_REF }; expect(body.expected_ref).toEqual(PROFILE_REF); expect(body.draft).toEqual(saved); return HttpResponse.json({ operationId: body.operationId, state: "succeeded", target_ref: { ...PROFILE_REF, revision: 2 }, result_ref: { ...PROFILE_REF, revision: 2 }, reason_code: null }); }),
      http.get(BASE + `/profiles/${PROFILE_ID}`, () => HttpResponse.json(profileAt(2, saved))),
    );
    mount();
    await screen.findByText("Reviewed skill · 当前绑定 R1"); expect(writes).toBe(0);
    await userEvent.click(screen.getByRole("button", { name: "绑定 New resource head R2" }));
    expect(await screen.findByText("New resource head · 当前绑定 R2")).toBeInTheDocument(); expect(writes).toBe(0);
    await userEvent.click(screen.getByRole("button", { name: "保存 Profile" }));
    await screen.findByText("配置已保存 · 仅用于新安排"); expect(writes).toBe(1);
  });

  it("explicit unbind saves only the Profile while leaving the resource intact", async () => {
    let profiles = 0, resources = 0;
    server.use(
      http.post(BASE + `/profiles/${PROFILE_ID}/revisions`, async ({ request }) => { profiles += 1; const body = await request.json() as { operationId: string; draft: ProfileDraft }; expect(body.draft).toEqual(PROFILE_DRAFT); return HttpResponse.json({ operationId: body.operationId, state: "succeeded", target_ref: { ...PROFILE_REF, revision: 2 }, result_ref: { ...PROFILE_REF, revision: 2 }, reason_code: null }); }),
      http.get(BASE + `/profiles/${PROFILE_ID}`, () => HttpResponse.json(profileAt(2))),
      http.all(BASE + "/resources/*", ({ request }) => { if (request.method !== "GET") resources += 1; return HttpResponse.json(RESOURCE); }),
    );
    mount();
    await userEvent.click(await screen.findByRole("button", { name: "解除 Reviewed skill R1 绑定" }));
    await userEvent.click(screen.getByRole("button", { name: "保存 Profile" }));
    await screen.findByText("配置已保存 · 仅用于新安排"); expect(profiles).toBe(1); expect(resources).toBe(0);
  });

  it("editing an independent resource retains the old Profile pin and never appends a Profile revision", async () => {
    let resources = 0, profiles = 0;
    const draft = { ...RESOURCE_DRAFT, settings: { body: "Original inline skill independent edit" } };
    server.use(
      http.get(BASE + "/resources", () => HttpResponse.json({ items: [RESOURCE] })),
      http.post(BASE + `/resources/${RESOURCE_REF.resource_id}/revisions`, async ({ request }) => { resources += 1; const body = await request.json() as { operationId: string; draft: ResourceDraft; expected_ref: typeof RESOURCE_REF }; expect(body.expected_ref).toEqual(RESOURCE_REF); expect(body.draft).toEqual(draft); return HttpResponse.json({ operationId: body.operationId, state: "succeeded", target_ref: { ...RESOURCE_REF, revision: 2 }, result_ref: { ...RESOURCE_REF, revision: 2 }, reason_code: null }); }),
      http.get(BASE + `/resources/${RESOURCE_REF.resource_id}`, ({ request }) => HttpResponse.json(new URL(request.url).searchParams.get("revision") === "2" ? resourceAt(2, draft) : RESOURCE)),
      http.post(BASE + `/profiles/${PROFILE_ID}/revisions`, () => { profiles += 1; return HttpResponse.error(); }),
    );
    mount();
    await userEvent.click(await screen.findByRole("link", { name: "编辑 Reviewed skill R1" }));
    await userEvent.type(await screen.findByLabelText("Skill 正文"), " independent edit");
    await userEvent.click(screen.getByRole("button", { name: "保存资源" }));
    await screen.findByText("资源已保存 · 现有绑定未改变");
    await userEvent.click(screen.getByRole("link", { name: "Skills" }));
    expect(await screen.findByText("Reviewed skill · 当前绑定 R1")).toBeInTheDocument(); expect(resources).toBe(1); expect(profiles).toBe(0);
  });

  it("keeps foreign resources hidden and disabled resources unbindable without guessing replacement IDs", async () => {
    const disabled = { ...RESOURCE, status: { ...RESOURCE.status, enabled: false } };
    const foreign = { ...RESOURCE, resource: { ...RESOURCE.resource, ref: { ...RESOURCE_REF, workspace_id: "20000000-0000-0000-0000-000000000002" }, draft: { ...RESOURCE_DRAFT, name: "Foreign resource" } } };
    server.use(http.get(BASE + "/resources", () => HttpResponse.json({ items: [foreign, disabled] })));
    mount("skill", PROFILE);
    expect(await screen.findByRole("button", { name: "绑定 Reviewed skill R1" })).toBeDisabled();
    expect(screen.queryByText(/Foreign resource/)).not.toBeInTheDocument();
  });

  it("changes resource enabled state with its exact status version and leaves Profile bindings unchanged", async () => {
    let writes = 0;
    server.use(http.put(BASE + `/resources/${RESOURCE_REF.resource_id}/status`, async ({ request }) => { writes += 1; expect(await request.json()).toEqual({ enabled: false, expected_version: 1 }); return HttpResponse.json({ resource_id: RESOURCE_REF.resource_id, enabled: false, version: 2 }); }));
    mount();
    await userEvent.click(await screen.findByRole("button", { name: "停用 New resource head" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "启用 New resource head" })).toBeInTheDocument());
    expect(screen.getByText("Reviewed skill · 当前绑定 R1")).toBeInTheDocument(); expect(writes).toBe(1);
  });

  it("aggregates two resource writers until the original uncertain resource is explicitly adopted", async () => {
    const secondId = "30000000-0000-0000-0000-000000000003";
    const second = { ...RESOURCE, resource: { ref: { ...RESOURCE_REF, resource_id: secondId }, draft: { ...RESOURCE_DRAFT, name: "Second resource" } }, status: { ...RESOURCE.status, resource_id: secondId } };
    let firstWrites = 0, secondWrites = 0, profileWrites = 0, firstReads = 0;
    server.use(
      http.get(BASE + "/resources", () => HttpResponse.json({ items: [RESOURCE, second] })),
      http.get(BASE + `/resources/${RESOURCE_REF.resource_id}`, () => { firstReads += 1; return HttpResponse.json(firstWrites === 0 ? RESOURCE : { ...RESOURCE, status: { ...RESOURCE.status, enabled: false, version: 2 } }); }),
      http.put(BASE + `/resources/${RESOURCE_REF.resource_id}/status`, async ({ request }) => { firstWrites += 1; expect(await request.json()).toEqual({ enabled: false, expected_version: 1 }); return HttpResponse.error(); }),
      http.put(BASE + `/resources/${secondId}/status`, async ({ request }) => { secondWrites += 1; expect(await request.json()).toEqual({ enabled: false, expected_version: 1 }); return HttpResponse.json({ resource_id: secondId, enabled: false, version: 2 }); }),
      http.post(BASE + `/profiles/${PROFILE_ID}/revisions`, () => { profileWrites += 1; return HttpResponse.error(); }),
    );
    const { router } = mount();
    await userEvent.click(await screen.findByRole("button", { name: "停用 Reviewed skill" }));
    await screen.findByText("资源状态修改未确认");
    await userEvent.click(screen.getByRole("button", { name: "停用 Second resource" }));
    await screen.findByRole("button", { name: "启用 Second resource" });
    await userEvent.click(screen.getByRole("link", { name: "前往 Task" }));
    expect(router.state.location.pathname).toBe(HERE);
    await userEvent.click(await screen.findByRole("button", { name: "留在此页" }));
    expect(router.state.location.pathname).toBe(HERE);
    expect(screen.getByText("Reviewed skill · 当前绑定 R1")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "读取资源当前状态" }));
    await screen.findByText("当前状态 · 已停用 · V2");
    expect(firstReads).toBeGreaterThan(0);
    await userEvent.click(screen.getByRole("link", { name: "前往 Task" }));
    await userEvent.click(await screen.findByRole("button", { name: "留在此页" }));
    expect(router.state.location.pathname).toBe(HERE);
    await userEvent.click(screen.getByRole("button", { name: "采用当前状态继续" }));
    await userEvent.click(screen.getByRole("link", { name: "前往 Task" }));
    await screen.findByRole("heading", { name: "Owned Task" });
    expect(firstWrites).toBe(1); expect(secondWrites).toBe(1); expect(profileWrites).toBe(0);
  });
});
