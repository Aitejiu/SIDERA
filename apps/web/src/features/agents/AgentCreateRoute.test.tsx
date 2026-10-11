import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { createMemoryRouter, Outlet, RouterProvider } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { WorkspaceTabs, WorkspaceTabsProvider } from "@/app/WorkspaceTabs";
import { loadTabs, storageKey } from "@/app/workspaceTabsState";
import { server } from "@/test/server";
import { readProfile } from "./api";
import { AgentDetailPage } from "./AgentDetailPage";
import { DESCRIPTOR, PROFILE, PROFILE_DRAFT, PROFILE_ID, PROFILE_REF, RUNTIME, RUNTIME_ID, WORKSPACE_ID as WS, profileAt } from "./testFixtures";
import type { ProfileDraft, ProfileRef, ProfileView } from "./types";

const base = `/agents/v1/workspaces/${WS}`;
const createdDraft: ProfileDraft = {
  ...PROFILE_DRAFT,
  name: "Created native profile",
  description: "R1 description",
  instructions: "Reviewed create instructions",
  max_active: 2,
};
const SECOND_PROFILE_ID = "30000000-0000-0000-0000-000000000002";
const secondDraft: ProfileDraft = {
  ...PROFILE_DRAFT,
  name: "Second saved profile",
  description: "Second Profile description",
  instructions: "Second Profile instructions",
};
const secondProfile: ProfileView = {
  ...PROFILE,
  profile: {
    ...PROFILE.profile,
    ref: { ...PROFILE_REF, profile_id: SECOND_PROFILE_ID },
    draft: secondDraft,
  },
  status: { ...PROFILE.status, profile_id: SECOND_PROFILE_ID },
};

type CreateRequest = {
  operationId: string;
  draft: ProfileDraft;
  expected_ref?: ProfileRef;
};

type CreateMode = "confirmed" | "held" | "unknown";

function WorkspaceRouteShell() {
  return <><WorkspaceTabs workspaceId={WS} /><Outlet /></>;
}

function captureFacts(router: ReturnType<typeof createMemoryRouter>) {
  const tabs = loadTabs(WS);
  const agentTabs = tabs.tabs.filter(tab => tab.kind === "agent");
  const stored = window.sessionStorage.getItem(storageKey(WS)) ?? "";
  return {
    heading: screen.queryByRole("heading", { level: 1 })?.textContent ?? null,
    form: {
      name: (screen.getByLabelText("名称") as HTMLInputElement).value,
      description: (screen.getByLabelText("描述") as HTMLInputElement).value,
      instructionsMatch: (screen.getByLabelText("指令") as HTMLTextAreaElement).value === createdDraft.instructions,
      runtimeId: (screen.getByLabelText("运行环境") as HTMLSelectElement).value,
      maxActive: (screen.getByLabelText("并行安排上限") as HTMLInputElement).value,
    },
    successVisible: screen.queryByText("配置已保存 · 仅用于新安排", { exact: true }) !== null,
    route: router.state.location.pathname + router.state.location.search,
    activeTabId: tabs.activeTabId,
    tabIds: tabs.tabs.map(tab => tab.id),
    recoveryKeys: agentTabs.map(tab => ({ id: tab.id, keys: Object.keys(tab.configurationRecovery ?? {}) })),
    instructionsStored: stored.includes(createdDraft.instructions),
  };
}

function mountCreateRoute(createMode: CreateMode = "confirmed") {
  let createCount = 0;
  let savedDraft: ProfileDraft | undefined;
  let originalOperationId: string | null = null;
  let operationReads = 0;
  let secondProfileReads = 0;
  let releaseHeldCreate: (() => void) | undefined;
  const reads: string[] = [];
  const nonGetRequests: string[] = [];

  server.use(
    http.get(base + "/runtimes", () => HttpResponse.json({ items: [RUNTIME] })),
    http.get(base + "/runtime-descriptors", () => HttpResponse.json([DESCRIPTOR])),
    http.post(base + "/profiles", async ({ request }) => {
      createCount += 1;
      nonGetRequests.push(`POST ${new URL(request.url).pathname}`);
      const body = await request.json() as CreateRequest;
      expect(body.operationId).not.toBe("");
      expect(body.expected_ref).toBeUndefined();
      expect(body.draft).toEqual(createdDraft);
      originalOperationId = body.operationId;
      savedDraft = body.draft;
      if (createMode === "held") {
        await new Promise<void>(resolve => { releaseHeldCreate = resolve; });
      }
      if (createMode === "unknown") return HttpResponse.error();
      return HttpResponse.json({
        operationId: body.operationId,
        state: "succeeded",
        target_ref: PROFILE_REF,
        result_ref: PROFILE_REF,
        reason_code: null,
      });
    }),
    http.get(`/product/v1/workspaces/${WS}/configuration-requests`, ({ request }) => {
      operationReads += 1;
      const requestedOperationId = new URL(request.url).searchParams.get("operationId");
      expect(requestedOperationId).toBe(originalOperationId);
      if (originalOperationId === null) throw new Error("original create operation is missing");
      return HttpResponse.json({
        operationId: originalOperationId,
        state: "reconciled",
        target_ref: PROFILE_REF,
        result_ref: PROFILE_REF,
        reason_code: null,
      });
    }),
    http.get(base + `/profiles/${PROFILE_ID}`, ({ request }) => {
      reads.push(new URL(request.url).searchParams.get("revision") ?? "head");
      return HttpResponse.json(profileAt(1, savedDraft ?? createdDraft));
    }),
    http.get(base + `/profiles/${SECOND_PROFILE_ID}`, () => {
      secondProfileReads += 1;
      return HttpResponse.json(secondProfile);
    }),
    http.get(base + `/profiles/${PROFILE_ID}/validation`, () => HttpResponse.json({ valid: true, issues: [], effective_preview: null })),
    http.get(base + `/profiles/${PROFILE_ID}/work`, () => HttpResponse.json({ conversations: [], task_work: { items: [], next_cursor: null } })),
    http.all("*", ({ request }) => {
      if (request.method !== "GET") {
        nonGetRequests.push(`${request.method} ${new URL(request.url).pathname}`);
      }
      return HttpResponse.json({ detail: "unexpected request" }, { status: 500 });
    }),
  );

  const router = createMemoryRouter([{
    path: "/workspaces/:workspaceId",
    element: <WorkspaceTabsProvider workspaceId={WS}><WorkspaceRouteShell /></WorkspaceTabsProvider>,
    children: [
      { path: "agents/:profileId", element: <AgentDetailPage /> },
      { path: "agents", element: <h1>智能体</h1> },
      { path: "tasks", element: <h1>任务</h1> },
    ],
  }], { initialEntries: [`/workspaces/${WS}/agents/new`] });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const user = userEvent.setup();
  const view = render(<QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider>);

  return {
    client,
    createCount: () => createCount,
    nonGetRequests,
    operationId: () => originalOperationId,
    operationReads: () => operationReads,
    secondProfileReads: () => secondProfileReads,
    releaseCreate: () => {
      if (releaseHeldCreate === undefined) throw new Error("held create response is not active");
      releaseHeldCreate();
    },
    reads,
    router,
    user,
    unmount: () => {
      view.unmount();
      router.dispose();
      client.clear();
      window.sessionStorage.clear();
      window.localStorage.clear();
    },
  };
}

async function submitReviewedCreate(user: ReturnType<typeof userEvent.setup>) {
  await screen.findByRole("option", { name: /controlled-settings/ });
  await user.type(screen.getByLabelText("名称"), createdDraft.name);
  await user.type(screen.getByLabelText("描述"), createdDraft.description);
  await user.selectOptions(screen.getByLabelText("运行环境"), RUNTIME_ID);
  await screen.findByRole("spinbutton", { name: "Temperature" });
  await user.type(screen.getByLabelText("指令"), createdDraft.instructions);
  const maxActive = screen.getByLabelText("并行安排上限");
  await user.clear(maxActive);
  await user.type(maxActive, String(createdDraft.max_active));
  await user.click(screen.getByRole("button", { name: "保存 Profile" }));
}

beforeEach(() => {
  window.sessionStorage.clear();
  window.localStorage.clear();
});

describe("composed Profile create route", () => {
  it("retains confirmed create feedback and reviewed fields in the composed page", async () => {
    const app = mountCreateRoute();
    try {
      await submitReviewedCreate(app.user);
      await screen.findByText("配置已保存 · 仅用于新安排");
      await waitFor(() => expect(screen.getByLabelText("名称")).toHaveValue(createdDraft.name));
      await waitFor(() => expect(screen.getByLabelText("指令")).toHaveValue(createdDraft.instructions));
      const facts = captureFacts(app.router);
      console.info("N20_PROFILE_CREATE_ROUTE_FACTS", JSON.stringify(facts));

      expect(screen.getByLabelText("名称")).toHaveValue(createdDraft.name);
      expect(screen.getByLabelText("描述")).toHaveValue(createdDraft.description);
      expect(screen.getByLabelText("指令")).toHaveValue(createdDraft.instructions);
      expect(screen.getByLabelText("运行环境")).toHaveValue(RUNTIME_ID);
      expect(screen.getByLabelText("并行安排上限")).toHaveValue(createdDraft.max_active);
      expect(screen.getByText("配置已保存 · 仅用于新安排")).toBeInTheDocument();
      expect(app.createCount()).toBe(1);
      expect(app.nonGetRequests).toEqual([`POST ${base}/profiles`]);
      expect(app.reads).toContain("1");
      expect(facts.instructionsStored).toBe(false);
      expect(loadTabs(WS).tabs.filter(tab => tab.kind === "agent").every(tab => tab.configurationRecovery === undefined)).toBe(true);
    } finally {
      app.unmount();
    }
  });

  it("uses the saved Profile identity rather than the new placeholder after create", async () => {
    const app = mountCreateRoute();
    try {
      await submitReviewedCreate(app.user);
      await screen.findByText("配置已保存 · 仅用于新安排");
      await waitFor(() => expect(screen.getByLabelText("名称")).toHaveValue(createdDraft.name));
      await waitFor(() => expect(screen.getByLabelText("指令")).toHaveValue(createdDraft.instructions));
      await waitFor(() => expect(app.reads).toContain("1"));
      const facts = captureFacts(app.router);
      console.info("N20_PROFILE_CREATE_ROUTE_FACTS", JSON.stringify(facts));

      await waitFor(() => expect(app.router.state.location.pathname)
        .toBe(`/workspaces/${WS}/agents/${PROFILE_ID}`));
      expect(app.router.state.location.search).toBe("?view=settings");
      expect(await screen.findByRole("heading", { name: createdDraft.name })).toBeInTheDocument();
      expect(screen.getByLabelText("名称")).toHaveValue(createdDraft.name);
      await waitFor(() => {
        const state = loadTabs(WS);
        expect(state.activeTabId).toBe(`agent:${PROFILE_ID}`);
        expect(state.tabs.some(tab => tab.id === "agent:new")).toBe(false);
      });
      const state = loadTabs(WS);
      expect(state.activeTabId).toBe(`agent:${PROFILE_ID}`);
      expect(state.tabs.some(tab => tab.id === "agent:new")).toBe(false);
      expect(state.tabs.find(tab => tab.id === `agent:${PROFILE_ID}`)?.configurationRecovery).toBeUndefined();
      expect(app.createCount()).toBe(1);
      expect(app.reads).toContain("1");
      expect(screen.queryByRole("dialog", { name: /未保存|保存结果未确认/ })).not.toBeInTheDocument();
    } finally {
      app.unmount();
    }
  });

  it("keeps a held create guarded and transitions only after confirmation", async () => {
    const app = mountCreateRoute("held");
    try {
      await submitReviewedCreate(app.user);
      await waitFor(() => expect(app.createCount()).toBe(1));
      expect(app.router.state.location.pathname).toBe(`/workspaces/${WS}/agents/new`);
      expect(screen.getByLabelText("名称")).toBeDisabled();
      expect(screen.queryByRole("heading", { name: createdDraft.name })).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "测试已保存 Profile" })).not.toBeInTheDocument();

      await app.user.click(screen.getByRole("link", { name: "任务总览" }));
      await screen.findByRole("dialog", { name: "保存结果未确认" });
      expect(app.router.state.location.pathname).toBe(`/workspaces/${WS}/agents/new`);
      await app.user.click(screen.getByRole("button", { name: "留在此页" }));
      expect(app.router.state.location.pathname).toBe(`/workspaces/${WS}/agents/new`);

      app.releaseCreate();
      await screen.findByText("配置已保存 · 仅用于新安排");
      await waitFor(() => expect(app.router.state.location.pathname)
        .toBe(`/workspaces/${WS}/agents/${PROFILE_ID}`));
      expect(app.router.state.location.search).toBe("?view=settings");
      expect(screen.getByLabelText("名称")).toHaveValue(createdDraft.name);
      expect(screen.getByLabelText("指令")).toHaveValue(createdDraft.instructions);
      expect(screen.queryByRole("dialog", { name: /未保存的配置|保存结果未确认/ })).not.toBeInTheDocument();
      expect(app.createCount()).toBe(1);
      expect(app.operationReads()).toBe(0);
      expect([["1"], ["1", "head"]]).toContainEqual(app.reads);
      expect(app.nonGetRequests).toEqual([`POST ${base}/profiles`]);
      expect(window.sessionStorage.getItem(storageKey(WS))).not.toContain(createdDraft.instructions);
    } finally {
      app.unmount();
    }
  });

  it("recovers an uncertain create by original GET before transitioning", async () => {
    const app = mountCreateRoute("unknown");
    try {
      await submitReviewedCreate(app.user);
      await screen.findByText("保存结果未确认");
      const operationId = app.operationId();
      expect(operationId).not.toBeNull();
      expect(app.router.state.location.pathname).toBe(`/workspaces/${WS}/agents/new`);
      expect(screen.getByLabelText("名称")).toBeDisabled();
      expect(screen.queryByRole("heading", { name: createdDraft.name })).not.toBeInTheDocument();
      expect(loadTabs(WS).tabs.find(tab => tab.id === "agent:new")?.configurationRecovery)
        .toEqual({ operationId });
      expect(window.sessionStorage.getItem(storageKey(WS))).not.toContain(createdDraft.instructions);
      expect(app.createCount()).toBe(1);
      expect(app.operationReads()).toBe(0);

      await app.user.click(screen.getByRole("button", { name: "查询原保存结果" }));
      await screen.findByText("已找到原配置 · 仅用于新安排");
      await waitFor(() => expect(app.router.state.location.pathname)
        .toBe(`/workspaces/${WS}/agents/${PROFILE_ID}`));
      expect(app.router.state.location.search).toBe("?view=settings");
      expect(screen.getByLabelText("名称")).toHaveValue(createdDraft.name);
      expect(screen.getByLabelText("指令")).toHaveValue(createdDraft.instructions);
      expect(loadTabs(WS).tabs.find(tab => tab.id === `agent:${PROFILE_ID}`)?.configurationRecovery).toBeUndefined();
      expect(app.createCount()).toBe(1);
      expect(app.operationReads()).toBe(1);
      expect([["1"], ["1", "head"]]).toContainEqual(app.reads);
      expect(app.nonGetRequests).toEqual([`POST ${base}/profiles`]);
      expect(screen.queryByRole("dialog", { name: /未保存的配置|保存结果未确认/ })).not.toBeInTheDocument();
      expect(window.sessionStorage.getItem(storageKey(WS))).not.toContain(createdDraft.instructions);
    } finally {
      app.unmount();
    }
  });

  it("does not reopen the previous created Profile when starting another new Profile", async () => {
    const app = mountCreateRoute();
    try {
      await submitReviewedCreate(app.user);
      await screen.findByText("配置已保存 · 仅用于新安排");
      await waitFor(() => expect(app.router.state.location.pathname)
        .toBe(`/workspaces/${WS}/agents/${PROFILE_ID}`));
      await waitFor(() => expect(loadTabs(WS).activeTabId).toBe(`agent:${PROFILE_ID}`));
      await waitFor(() => expect(loadTabs(WS).tabs.some(tab => tab.id === "agent:new")).toBe(false));

      await act(async () => {
        await app.router.navigate(`/workspaces/${WS}/agents/new`);
      });
      await screen.findByRole("heading", { name: "新建 Profile" });
      await waitFor(() => expect(loadTabs(WS).activeTabId).toBe("agent:new"));
      expect(app.router.state.location.pathname).toBe(`/workspaces/${WS}/agents/new`);
      expect(screen.getByLabelText("名称")).toHaveValue("");
      expect(screen.getByLabelText("指令")).toHaveValue("");
      expect(screen.queryByRole("heading", { name: createdDraft.name })).not.toBeInTheDocument();
      expect(app.createCount()).toBe(1);
    } finally {
      app.unmount();
    }
  });

  it("initializes settings for a different Profile rather than carrying the created Profile draft", async () => {
    const app = mountCreateRoute();
    try {
      await submitReviewedCreate(app.user);
      await screen.findByText("配置已保存 · 仅用于新安排");
      await waitFor(() => expect(app.router.state.location.pathname)
        .toBe(`/workspaces/${WS}/agents/${PROFILE_ID}`));
      await app.client.prefetchQuery({
        queryKey: ["agent-profile", WS, SECOND_PROFILE_ID],
        queryFn: () => readProfile(WS, SECOND_PROFILE_ID),
      });

      await act(async () => {
        await app.router.navigate(`/workspaces/${WS}/agents/${SECOND_PROFILE_ID}?view=settings`);
      });
      expect(app.router.state.location.pathname + app.router.state.location.search)
        .toBe(`/workspaces/${WS}/agents/${SECOND_PROFILE_ID}?view=settings`);
      await screen.findByRole("heading", { name: secondDraft.name });
      expect(app.secondProfileReads()).toBeGreaterThan(0);

      expect(screen.getByLabelText("名称")).toHaveValue(secondDraft.name);
      expect(screen.getByLabelText("描述")).toHaveValue(secondDraft.description);
      expect(screen.getByLabelText("指令")).toHaveValue(secondDraft.instructions);
      expect(screen.queryByText("配置已保存 · 仅用于新安排")).not.toBeInTheDocument();
      expect(app.createCount()).toBe(1);
    } finally {
      app.unmount();
    }
  });
});
