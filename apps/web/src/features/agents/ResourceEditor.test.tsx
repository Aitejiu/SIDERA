import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { createMemoryRouter, Link, RouterProvider } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { WorkspaceTabs, WorkspaceTabsProvider } from "@/app/WorkspaceTabs";
import { initialTabs, openTab, reduceTabs, saveTabs, storageKey } from "@/app/workspaceTabsState";
import { server } from "@/test/server";
import { ResourceEditor } from "./ResourceEditor";
import { CONNECTION, CREDENTIAL, PROFILE_ID, RESOURCE, RESOURCE_REF, WORKSPACE_ID as WS, resourceAt } from "./testFixtures";
import type { ConfigurationResult, ResourceDraft, ResourceView } from "./types";

const BASE = `/agents/v1/workspaces/${WS}`;
const HERE = `/workspaces/${WS}/agents/${PROFILE_ID}`;
const THERE = `/workspaces/${WS}/tasks/owned-task`;
const TOOL_CONNECTION = {
  workspace_id: WS, connection_id: "c0000000-0000-0000-0000-000000000001",
  name: "SIDERA Task Tools", availability: "configured",
};

function mount(kind: ResourceDraft["kind"] = "skill", resource: ResourceView | null = null) {
  saveTabs(reduceTabs(initialTabs(WS), openTab({ id: `agent:${PROFILE_ID}`, kind: "agent", title: "Reviewer", href: HERE, closeable: true })));
  const onSaved = vi.fn();
  const router = createMemoryRouter([{
    path: HERE, element: <WorkspaceTabsProvider workspaceId={WS}><WorkspaceTabs workspaceId={WS} /><Link to={THERE}>打开实际目标页</Link><ResourceEditor workspaceId={WS} resource={resource} kind={kind} onSaved={onSaved} /></WorkspaceTabsProvider>,
  }, { path: THERE, element: <p>Actual destination</p> }], { initialEntries: [HERE] });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider>);
  return { onSaved, router, client };
}

beforeEach(() => {
  window.sessionStorage.clear();
  server.use(
    http.get(`/model-config/v1/workspaces/${WS}/connections`, () => HttpResponse.json([CONNECTION])),
    http.get(`/product/v1/workspaces/${WS}/tool-connections`, () => HttpResponse.json([TOOL_CONNECTION])),
    http.get(`/model-config/v1/workspaces/${WS}/credentials`, () => HttpResponse.json([])),
  );
});

describe("typed independent capability resource edits", () => {
  it.each(["product", "model"])("keeps the other loaded connection usable when %s metadata fails", async failed => {
    let writes = 0;
    const selected = failed === "product" ? CONNECTION : TOOL_CONNECTION;
    const draft: ResourceDraft = { name: "Available connection", kind: "mcp", settings: { connection_id: selected.connection_id }, secret_refs: [] };
    server.use(
      http.get(failed === "product" ? `/product/v1/workspaces/${WS}/tool-connections` : `/model-config/v1/workspaces/${WS}/connections`, () => HttpResponse.json({ detail: { code: "store_unavailable" } }, { status: 503 })),
      http.post(BASE + "/resources", async ({ request }) => {
        writes += 1;
        const body = await request.json() as { operationId: string; draft: ResourceDraft };
        expect(body.draft).toEqual(draft);
        return HttpResponse.json({ operationId: body.operationId, state: "succeeded", target_ref: RESOURCE_REF, result_ref: RESOURCE_REF, reason_code: null });
      }),
      http.get(BASE + `/resources/${RESOURCE_REF.resource_id}`, () => HttpResponse.json(resourceAt(1, draft))),
    );
    mount("mcp");
    await screen.findByText(failed === "product" ? "任务工具连接元数据不可用" : "连接元数据不可用");
    const option = await screen.findByRole("option", { name: new RegExp(selected.name) });
    expect(option).not.toBeDisabled();
    expect(screen.getByLabelText("受信连接")).not.toBeDisabled();
    await userEvent.type(screen.getByLabelText("资源名称"), draft.name);
    await userEvent.selectOptions(screen.getByLabelText("受信连接"), selected.connection_id);
    await userEvent.click(screen.getByRole("button", { name: "保存资源" }));
    await screen.findByText("资源已保存 · 现有绑定未改变");
    expect(writes).toBe(1);
  });

  it.each(["product", "model"])("does not bind stale %s data after a failed metadata refresh", async failed => {
    let writes = 0;
    const selected = failed === "product" ? TOOL_CONNECTION : CONNECTION;
    const { client } = mount("mcp");
    await screen.findByRole("option", { name: new RegExp(selected.name) });
    await userEvent.type(screen.getByLabelText("资源名称"), "Stale metadata must not bind");
    await userEvent.selectOptions(screen.getByLabelText("受信连接"), selected.connection_id);
    server.use(
      http.get(failed === "product" ? `/product/v1/workspaces/${WS}/tool-connections` : `/model-config/v1/workspaces/${WS}/connections`, () => HttpResponse.json({ detail: { code: "store_unavailable" } }, { status: 503 })),
      http.post(BASE + "/resources", () => { writes += 1; return HttpResponse.error(); }),
    );
    await act(async () => { await client.refetchQueries({ queryKey: [failed === "product" ? "product-tool-connections" : "agent-connections", WS] }); });
    await screen.findByText(failed === "product" ? "任务工具连接元数据不可用" : "连接元数据不可用");
    expect(screen.queryByRole("option", { name: new RegExp(selected.name) })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "保存资源" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "保存资源" }));
    expect(writes).toBe(0);
  });

  it("selects the advertised Product task tools and saves their exact public UUID without credentials or starts", async () => {
    let writes = 0, otherWrites = 0;
    const draft: ResourceDraft = { name: "Explicit task tools", kind: "mcp", settings: { connection_id: TOOL_CONNECTION.connection_id }, secret_refs: [] };
    server.use(
      http.post(BASE + "/resources", async ({ request }) => {
        writes += 1;
        const body = await request.json() as { operationId: string; draft: ResourceDraft };
        expect(body.draft).toEqual(draft);
        return HttpResponse.json({ operationId: body.operationId, state: "succeeded", target_ref: RESOURCE_REF, result_ref: RESOURCE_REF, reason_code: null }, { status: 201 });
      }),
      http.get(BASE + `/resources/${RESOURCE_REF.resource_id}`, () => HttpResponse.json(resourceAt(1, draft))),
      http.post("*", () => { otherWrites += 1; return HttpResponse.error(); }),
    );
    const { onSaved } = mount("mcp");
    const option = await screen.findByRole("option", { name: "SIDERA Task Tools · configured" });
    expect(option).not.toBeDisabled();
    expect(await screen.findByRole("option", { name: /Trusted controlled connection.*R1/ })).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("资源名称"), draft.name);
    await userEvent.selectOptions(screen.getByLabelText("受信连接"), TOOL_CONNECTION.connection_id);
    await userEvent.click(screen.getByRole("button", { name: "保存资源" }));
    await screen.findByText("资源已保存 · 现有绑定未改变");
    expect(onSaved).toHaveBeenCalledWith(resourceAt(1, draft));
    expect(writes).toBe(1); expect(otherWrites).toBe(0);
  });

  it.each(["failed", "foreign", "missing", "unavailable", "duplicate", "catalog_duplicate"])("refuses a %s Product connection binding", async variant => {
    let writes = 0;
    const metadata = variant === "foreign" ? [{ ...TOOL_CONNECTION, workspace_id: "20000000-0000-0000-0000-000000000002" }]
      : variant === "missing" ? [] : variant === "unavailable" ? [{ ...TOOL_CONNECTION, availability: "unavailable" }]
        : variant === "duplicate" ? [TOOL_CONNECTION, TOOL_CONNECTION] : [TOOL_CONNECTION];
    server.use(
      http.get(`/product/v1/workspaces/${WS}/tool-connections`, () => variant === "failed"
        ? HttpResponse.json({ detail: { code: "store_unavailable" } }, { status: 503 }) : HttpResponse.json(metadata)),
      http.get(`/model-config/v1/workspaces/${WS}/connections`, () => HttpResponse.json(variant === "catalog_duplicate" ? [{ ...CONNECTION, connection_id: TOOL_CONNECTION.connection_id }] : [CONNECTION])),
      http.post(BASE + "/resources/*", () => { writes += 1; return HttpResponse.error(); }),
    );
    mount("mcp", resourceAt(1, { name: "Original task tools", kind: "mcp", settings: { connection_id: TOOL_CONNECTION.connection_id }, secret_refs: [] }));
    await userEvent.type(screen.getByLabelText("资源名称"), " edited");
    if (variant === "failed") expect(await screen.findByText("任务工具连接元数据不可用")).toBeInTheDocument();
    else await waitFor(() => expect(screen.getByLabelText("受信连接")).not.toBeDisabled());
    expect(screen.getByRole("button", { name: "保存资源" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "保存资源" }));
    expect(writes).toBe(0);
  });

  it.each([
    ["skill", { body: "Reviewed body" }],
    ["mcp", { transport: "stdio", command: ["/owned/bin/tool"], environment: {} }],
    ["mcp", { transport: "http", url: "http://127.0.0.1:19002/mcp", headers: {} }],
    ["integration", { connection_id: CONNECTION.connection_id }],
  ] satisfies [ResourceDraft["kind"], ResourceDraft["settings"]][])("does not fetch Product metadata for %s with non-MCP-connection settings", async (kind, settings) => {
    let reads = 0;
    server.use(http.get(`/product/v1/workspaces/${WS}/tool-connections`, () => { reads += 1; return HttpResponse.json([TOOL_CONNECTION]); }));
    mount(kind, resourceAt(1, { name: "Existing resource", kind, settings, secret_refs: [] }));
    await screen.findByRole("button", { name: "新建凭据" });
    if (kind === "integration") await screen.findByRole("option", { name: /Trusted controlled connection/ });
    expect(reads).toBe(0);
  });

  it.each([
    ["skill", "inline", { body: "Reviewed inline body" }],
    ["skill", "source", { source_ref: { provider: "knowledge", key: "owned/revision", version: "7" }, content_hash: "c".repeat(64) }],
    ["mcp", "connection", { connection_id: CONNECTION.connection_id }],
    ["mcp", "stdio", { transport: "stdio", command: ["/owned/bin/tool", "--read"], environment: { SAFE_MODE: "read" } }],
    ["mcp", "http", { transport: "http", url: "http://127.0.0.1:19002/mcp", headers: { "X-Mode": "read" } }],
    ["integration", "connection", { connection_id: CONNECTION.connection_id }],
  ] satisfies [ResourceDraft["kind"], string, ResourceDraft["settings"]][])("saves %s %s using exact finite settings without fetching the external source", async (kind, mode, settings) => {
    let writes = 0, externalReads = 0;
    let draft: ResourceDraft | undefined;
    server.use(
      http.post(BASE + "/resources", async ({ request }) => {
        writes += 1;
        const body = await request.json() as { operationId: string; draft: ResourceDraft };
        draft = body.draft;
        expect(body.draft).toEqual({ name: "Reviewed resource", kind, settings, secret_refs: [] });
        return HttpResponse.json({ operationId: body.operationId, state: "succeeded", target_ref: RESOURCE_REF, result_ref: RESOURCE_REF, reason_code: null }, { status: 201 });
      }),
      http.get(BASE + `/resources/${RESOURCE_REF.resource_id}`, ({ request }) => {
        expect(new URL(request.url).searchParams.get("revision")).toBe("1");
        if (draft === undefined) throw new Error("Controlled resource not written");
        return HttpResponse.json(resourceAt(1, draft));
      }),
      http.get("http://127.0.0.1:19002/*", () => { externalReads += 1; return HttpResponse.json({}); }),
    );
    mount(kind);
    await userEvent.type(screen.getByLabelText("资源名称"), "Reviewed resource");
    if (kind !== "integration") await userEvent.selectOptions(screen.getByLabelText("资源模式"), mode);
    if (mode === "inline") await userEvent.type(screen.getByLabelText("Skill 正文"), "Reviewed inline body");
    if (mode === "source") {
      await userEvent.type(screen.getByLabelText("来源提供方"), "knowledge");
      await userEvent.type(screen.getByLabelText("来源键"), "owned/revision");
      await userEvent.type(screen.getByLabelText("来源版本"), "7");
      await userEvent.type(screen.getByLabelText("SHA256"), "c".repeat(64));
    }
    if (mode === "connection") await userEvent.selectOptions(await screen.findByLabelText("受信连接"), CONNECTION.connection_id);
    if (mode === "stdio") {
      await userEvent.type(screen.getByLabelText("可执行文件绝对路径"), "/owned/bin/tool");
      await userEvent.click(screen.getByRole("button", { name: "添加参数" }));
      await userEvent.type(screen.getByLabelText("参数 1"), "--read");
      await userEvent.click(screen.getByRole("button", { name: "添加变量" }));
      await userEvent.type(screen.getByLabelText("变量名称 1"), "SAFE_MODE");
      await userEvent.type(screen.getByLabelText("变量值 1"), "read");
    }
    if (mode === "http") {
      await userEvent.type(screen.getByLabelText("MCP 地址"), "http://127.0.0.1:19002/mcp");
      await userEvent.click(screen.getByRole("button", { name: "添加 Header" }));
      await userEvent.type(screen.getByLabelText("Header 名称 1"), "X-Mode");
      await userEvent.type(screen.getByLabelText("Header 值 1"), "read");
    }
    await userEvent.click(screen.getByRole("button", { name: "保存资源" }));
    expect(await screen.findByText("资源已保存 · 现有绑定未改变")).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: /JSON/ })).not.toBeInTheDocument();
    expect(writes).toBe(1); expect(externalReads).toBe(0);
  });

  it("retains one original resource revision request and reads its exact result after a lost reply", async () => {
    let writes = 0, queries = 0, operationId = "";
    const ref = { ...RESOURCE_REF, revision: 2 };
    const draft = { ...RESOURCE.resource.draft, settings: { body: "Original inline skill local edit" } };
    server.use(
      http.post(BASE + `/resources/${RESOURCE_REF.resource_id}/revisions`, async ({ request }) => {
        writes += 1;
        const body = await request.json() as { operationId: string; expected_ref: typeof RESOURCE_REF; draft: ResourceDraft };
        operationId = body.operationId;
        expect(body.expected_ref).toEqual(RESOURCE_REF); expect(body.draft).toEqual(draft);
        return HttpResponse.error();
      }),
      http.get(`/product/v1/workspaces/${WS}/configuration-requests`, ({ request }) => {
        queries += 1; expect(new URL(request.url).searchParams.get("operationId")).toBe(operationId);
        const result: ConfigurationResult = { operationId, state: "reconciled", target_ref: ref, result_ref: ref, reason_code: null };
        return HttpResponse.json(result);
      }),
      http.get(BASE + `/resources/${RESOURCE_REF.resource_id}`, ({ request }) => { expect(new URL(request.url).searchParams.get("revision")).toBe("2"); return HttpResponse.json(resourceAt(2, draft)); }),
    );
    const { onSaved } = mount("skill", RESOURCE);
    await userEvent.type(screen.getByLabelText("Skill 正文"), " local edit");
    await userEvent.click(screen.getByRole("button", { name: "保存资源" }));
    await screen.findByText("资源保存结果未确认");
    expect(window.sessionStorage.getItem(storageKey(WS))).toContain(operationId);
    expect(window.sessionStorage.getItem(storageKey(WS))).not.toContain("local edit");
    await userEvent.click(screen.getByRole("button", { name: "查询原资源保存结果" }));
    expect(await screen.findByText("已找到原资源 · 现有绑定未改变")).toBeInTheDocument();
    expect(onSaved).toHaveBeenCalledWith(resourceAt(2, draft));
    expect(writes).toBe(1); expect(queries).toBe(1);
  });

  it("keeps a foreign or unresolved trusted connection unselectable rather than guessing an ID", async () => {
    const foreign = { ...CONNECTION, connection_id: "b0000000-0000-0000-0000-000000000002", workspace_id: "20000000-0000-0000-0000-000000000002", name: "Foreign connection" };
    server.use(http.get(`/model-config/v1/workspaces/${WS}/connections`, () => HttpResponse.json([foreign, { ...CONNECTION, availability: "credential_revoked" }])));
    mount("integration");
    await waitFor(() => expect(screen.queryByRole("option", { name: "Foreign connection" })).not.toBeInTheDocument());
    const select = await screen.findByLabelText("受信连接");
    expect(select).toHaveValue("");
    expect(await screen.findByRole("option", { name: /Trusted controlled connection/ })).toBeDisabled();
    expect(screen.queryByRole("textbox", { name: /connection_id|JSON/ })).not.toBeInTheDocument();
  });

  it("saves only exact credential references in the independent resource, never the key or a Profile", async () => {
    let writes = 0;
    let written: ResourceDraft | undefined;
    server.use(
      http.get(`/model-config/v1/workspaces/${WS}/credentials`, () => HttpResponse.json([CREDENTIAL])),
      http.post(BASE + "/resources", async ({ request }) => { writes += 1; const body = await request.json() as { operationId: string; draft: ResourceDraft }; written = body.draft; expect(body.draft).toEqual({ name: "Skill with safe credential", kind: "skill", settings: { body: "Reviewed inline skill" }, secret_refs: [{ credential_ref: { workspace_id: WS, credential_id: CREDENTIAL.credential_id }, destination: "SERVICE_API_KEY" }] }); return HttpResponse.json({ operationId: body.operationId, state: "succeeded", target_ref: RESOURCE_REF, result_ref: RESOURCE_REF, reason_code: null }); }),
      http.get(BASE + `/resources/${RESOURCE_REF.resource_id}`, () => { if (written === undefined) throw new Error("Controlled resource not yet written"); return HttpResponse.json(resourceAt(1, written)); }),
    );
    mount();
    await userEvent.type(screen.getByLabelText("资源名称"), "Skill with safe credential");
    await userEvent.type(screen.getByLabelText("Skill 正文"), "Reviewed inline skill");
    await screen.findByRole("option", { name: /Controlled API credential/ });
    await userEvent.selectOptions(screen.getByLabelText("凭据引用"), CREDENTIAL.credential_id);
    await userEvent.type(screen.getByLabelText("注入目标"), "SERVICE_API_KEY");
    await userEvent.click(screen.getByRole("button", { name: "绑定凭据" }));
    expect(screen.queryByLabelText("密钥值")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "保存资源" }));
    await screen.findByText("资源已保存 · 现有绑定未改变"); expect(writes).toBe(1);
  });

  it("keeps an original unknown credential write in the actual resource route guard after its dialog closes", async () => {
    let writes = 0;
    server.use(http.post(`/model-config/v1/workspaces/${WS}/credential-writes`, () => { writes += 1; return HttpResponse.error(); }));
    const { router } = mount();
    await userEvent.click(await screen.findByRole("button", { name: "新建凭据" }));
    await userEvent.type(screen.getByLabelText("凭据别名"), "Original resource credential");
    await userEvent.type(screen.getByLabelText("密钥值"), "unit-only-transient-value");
    await userEvent.click(screen.getByRole("button", { name: "保存凭据" }));
    await screen.findByText("凭据写入结果未确认");
    await userEvent.click(screen.getByRole("button", { name: "关闭凭据" }));
    await userEvent.click(screen.getByRole("link", { name: "打开实际目标页" }));
    await screen.findByRole("dialog", { name: "保存结果未确认" });
    await userEvent.click(screen.getByRole("button", { name: "留在此页" }));
    expect(router.state.location.pathname).toBe(HERE); expect(writes).toBe(1);
    await userEvent.click(screen.getByRole("button", { name: "新建凭据" }));
    expect(screen.getByLabelText("密钥值")).toHaveValue("");
    expect(screen.getByRole("button", { name: "保存凭据" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "查询原凭据写入" })).toBeInTheDocument();
  });
});
