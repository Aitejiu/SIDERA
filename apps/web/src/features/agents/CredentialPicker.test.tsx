import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { server } from "@/test/server";
import { CredentialPicker } from "./CredentialPicker";
import { CredentialWriteDialog } from "./CredentialWriteDialog";
import { CREDENTIAL, WORKSPACE_ID as WS } from "./testFixtures";
import type { CredentialWrite, CredentialWriteInput, SecretBinding } from "./types";

const BASE = `/model-config/v1/workspaces/${WS}`;

function mountPicker(initial: SecretBinding[] = []) {
  function Fixture() {
    const [value, setValue] = useState(initial);
    const [guard, setGuard] = useState({ dirty: false, unsettled: false });
    return <><CredentialPicker workspaceId={WS} value={value} onChange={setValue} onGuardChange={setGuard} /><output aria-label="safe bindings">{JSON.stringify(value)}</output><output aria-label="credential guard">{JSON.stringify(guard)}</output></>;
  }
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><Fixture /></QueryClientProvider>);
}

function mountDialog() {
  const completed = vi.fn(), close = vi.fn();
  function Fixture() {
    const [open, setOpen] = useState(true);
    return <><button onClick={() => setOpen(true)}>打开凭据</button><CredentialWriteDialog workspaceId={WS} open={open} onClose={() => { close(); setOpen(false); }} onCompleted={completed} /></>;
  }
  render(<Fixture />);
  return { completed, close };
}

beforeEach(() => {
  window.sessionStorage.clear(); window.localStorage.clear();
  server.use(http.get(BASE + "/credentials", () => HttpResponse.json([CREDENTIAL])));
});

describe("safe credential selection and original managed write", () => {
  it("binds an exact same-workspace active Ref and explicit destination, then unbinds without revoking", async () => {
    let writes = 0;
    const foreign = { ...CREDENTIAL, credential_id: "a0000000-0000-0000-0000-000000000002", workspace_id: "20000000-0000-0000-0000-000000000002", alias: "Foreign credential" };
    const revoked = { ...CREDENTIAL, credential_id: "a0000000-0000-0000-0000-000000000003", status: "revoked", alias: "Revoked credential" };
    server.use(http.get(BASE + "/credentials", () => HttpResponse.json([foreign, revoked, CREDENTIAL])), http.post(BASE + "/*", () => { writes += 1; return HttpResponse.json({}); }));
    mountPicker();
    const picker = await screen.findByLabelText("凭据引用");
    await waitFor(() => expect(screen.getByRole("option", { name: /Controlled API credential/ })).toBeInTheDocument());
    expect(screen.queryByRole("option", { name: /Foreign credential/ })).not.toBeInTheDocument();
    expect(screen.getByRole("option", { name: /Revoked credential/ })).toBeDisabled();
    await userEvent.selectOptions(picker, CREDENTIAL.credential_id);
    await userEvent.type(screen.getByLabelText("注入目标"), "SERVICE_API_KEY");
    await userEvent.click(screen.getByRole("button", { name: "绑定凭据" }));
    expect(JSON.parse(screen.getByLabelText("safe bindings").textContent ?? "null")).toEqual([{ credential_ref: { workspace_id: WS, credential_id: CREDENTIAL.credential_id }, destination: "SERVICE_API_KEY" }]);
    expect(screen.queryByLabelText("密钥值")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "解除 SERVICE_API_KEY 绑定" }));
    expect(screen.getByLabelText("safe bindings")).toHaveTextContent("[]"); expect(writes).toBe(0);
  });

  it("rejects a duplicate destination locally and leaves the original credential Ref unchanged", async () => {
    const original = { credential_ref: { workspace_id: WS, credential_id: CREDENTIAL.credential_id }, destination: "SERVICE_API_KEY" };
    mountPicker([original]);
    await screen.findByRole("option", { name: /Controlled API credential/ });
    await userEvent.selectOptions(await screen.findByLabelText("凭据引用"), CREDENTIAL.credential_id);
    await userEvent.type(screen.getByLabelText("注入目标"), "service_api_key");
    await userEvent.click(screen.getByRole("button", { name: "绑定凭据" }));
    expect(await screen.findByText("注入目标已绑定")).toBeInTheDocument();
    expect(JSON.parse(screen.getByLabelText("safe bindings").textContent ?? "null")).toEqual([original]);
  });

  it("reports credential metadata failure without presenting a successful empty catalog", async () => {
    server.use(http.get(BASE + "/credentials", () => HttpResponse.json({}, { status: 503 })));
    mountPicker();
    expect(await screen.findByText("凭据元数据不可用")).toBeInTheDocument();
    expect(screen.queryByText("尚无凭据")).not.toBeInTheDocument();
    expect(screen.getByLabelText("凭据引用")).toBeDisabled();
  });

  it("retains one unknown write, clears the password and recovers only that original operation", async () => {
    let creates = 0, reads = 0, recoveries = 0, original = "";
    const secret = `unit-only-${crypto.randomUUID()}`;
    function result(status: CredentialWrite["status"]): CredentialWrite { return { workspace_id: WS, operation_id: original, status, credential: { ...CREDENTIAL, status: status === "completed" ? "active" : "pending" } }; }
    server.use(
      http.post(BASE + "/credential-writes", async ({ request }) => {
        creates += 1; const body = await request.json() as CredentialWriteInput;
        original = body.operation_id; expect(body.alias).toBe("Reviewed credential"); expect(body.secret === secret).toBe(true); expect(body.replaces_id).toBeNull();
        return HttpResponse.json(result("unknown"), { status: 503 });
      }),
      http.get(BASE + "/credential-writes/:id", ({ params }) => { reads += 1; expect(params.id).toBe(original); return HttpResponse.json(result("unknown")); }),
      http.post(BASE + "/credential-writes/:id/recover", async ({ params, request }) => { recoveries += 1; expect(params.id).toBe(original); expect(await request.text()).toBe(""); return HttpResponse.json(result("completed")); }),
    );
    const { completed } = mountDialog();
    await userEvent.type(screen.getByLabelText("凭据别名"), "Reviewed credential");
    await userEvent.type(screen.getByLabelText("密钥值"), secret);
    const submit = screen.getByRole("button", { name: "保存凭据" });
    await userEvent.dblClick(submit);
    expect(await screen.findByText("凭据写入结果未确认")).toBeInTheDocument();
    expect(screen.getByLabelText("密钥值")).toHaveValue("");
    expect(submit).toBeDisabled();
    expect(window.sessionStorage.length).toBe(0); expect(window.localStorage.length).toBe(0);
    expect(document.body.textContent?.includes(secret)).toBe(false);
    await userEvent.click(screen.getByRole("button", { name: "查询原凭据写入" }));
    await waitFor(() => expect(reads).toBe(1));
    await userEvent.click(screen.getByRole("button", { name: "恢复原凭据写入" }));
    expect(await screen.findByText("凭据已保存")).toBeInTheDocument();
    expect(completed).toHaveBeenCalledWith(CREDENTIAL);
    expect(creates).toBe(1); expect(reads).toBe(1); expect(recoveries).toBe(1);
  });

  it("cancels a transient password without writing and reopens with no secret", async () => {
    let creates = 0;
    server.use(http.post(BASE + "/credential-writes", () => { creates += 1; return HttpResponse.json({}); }));
    const { close } = mountDialog();
    await userEvent.type(screen.getByLabelText("密钥值"), "unit-only-unsubmitted-value");
    await userEvent.click(screen.getByRole("button", { name: "关闭凭据" }));
    expect(close).toHaveBeenCalledOnce();
    await userEvent.click(screen.getByRole("button", { name: "打开凭据" }));
    expect(screen.getByLabelText("密钥值")).toHaveValue(""); expect(creates).toBe(0);
  });

  it("keeps a mismatched original status uncertain rather than accepting a foreign credential", async () => {
    let writes = 0, original = "";
    server.use(
      http.post(BASE + "/credential-writes", async ({ request }) => { writes += 1; original = (await request.json() as CredentialWriteInput).operation_id; return HttpResponse.error(); }),
      http.get(BASE + "/credential-writes/:id", () => HttpResponse.json({ workspace_id: WS, operation_id: original, status: "completed", credential: { ...CREDENTIAL, workspace_id: "20000000-0000-0000-0000-000000000002" } })),
    );
    const { completed } = mountDialog();
    await userEvent.type(screen.getByLabelText("凭据别名"), "Reviewed credential");
    await userEvent.type(screen.getByLabelText("密钥值"), "unit-only-value");
    await userEvent.click(screen.getByRole("button", { name: "保存凭据" }));
    await screen.findByText("凭据写入结果未确认");
    await userEvent.click(screen.getByRole("button", { name: "查询原凭据写入" }));
    expect(await screen.findByText("凭据写入结果未确认")).toBeInTheDocument();
    expect(completed).not.toHaveBeenCalled(); expect(writes).toBe(1);
  });

  it("revokes only the explicit credential version and reports remaining cleanup truthfully", async () => {
    let writes = 0;
    server.use(http.post(BASE + `/credentials/${CREDENTIAL.credential_id}/revoke`, async ({ request }) => { writes += 1; expect(await request.json()).toEqual({ expected_version: 1 }); return HttpResponse.json({ credential: { ...CREDENTIAL, status: "revoked", version: 2 }, cleanup_pending: true }); }));
    mountPicker();
    await userEvent.click(await screen.findByRole("button", { name: "撤销 Controlled API credential" }));
    await screen.findByRole("dialog", { name: "撤销凭据" });
    await userEvent.click(screen.getByRole("button", { name: "确认撤销" }));
    expect(await screen.findByText("凭据已撤销 · 清理待确认")).toBeInTheDocument(); expect(writes).toBe(1);
  });

  it.each(["completed", "failed"] as const)("allows a fresh explicit intent only after closing the known %s credential write", async status => {
    const operations: string[] = [];
    server.use(http.post(BASE + "/credential-writes", async ({ request }) => {
      const body = await request.json() as CredentialWriteInput; operations.push(body.operation_id);
      return HttpResponse.json({ workspace_id: WS, operation_id: body.operation_id, status, credential: { ...CREDENTIAL, alias: body.alias, status: status === "completed" ? "active" : "failed" } });
    }));
    mountDialog();
    await userEvent.type(screen.getByLabelText("凭据别名"), "First reviewed credential");
    await userEvent.type(screen.getByLabelText("密钥值"), "unit-only-first-value");
    await userEvent.click(screen.getByRole("button", { name: "保存凭据" }));
    await screen.findByText(status === "completed" ? "凭据已保存" : "凭据写入失败");
    expect(screen.getByLabelText("密钥值")).toHaveValue("");
    await userEvent.click(screen.getByRole("button", { name: "关闭凭据" }));
    await userEvent.click(screen.getByRole("button", { name: "打开凭据" }));
    expect(screen.getByLabelText("凭据别名")).toBeEnabled();
    expect(screen.getByLabelText("凭据别名")).toHaveValue("");
    expect(operations).toHaveLength(1);
    await userEvent.type(screen.getByLabelText("凭据别名"), "Second reviewed credential");
    await userEvent.type(screen.getByLabelText("密钥值"), "unit-only-second-value");
    await userEvent.click(screen.getByRole("button", { name: "保存凭据" }));
    await waitFor(() => expect(operations).toHaveLength(2));
    expect(operations[0]).not.toBe(operations[1]);
    expect(window.sessionStorage.length).toBe(0); expect(window.localStorage.length).toBe(0);
  });

  it("preserves the original unknown credential write through explicit close and reopen", async () => {
    let creates = 0, original = "", reads = 0;
    server.use(
      http.post(BASE + "/credential-writes", async ({ request }) => { creates += 1; original = (await request.json() as CredentialWriteInput).operation_id; return HttpResponse.error(); }),
      http.get(BASE + "/credential-writes/:id", ({ params }) => { reads += 1; expect(params.id).toBe(original); return HttpResponse.json({ workspace_id: WS, operation_id: original, status: "unknown", credential: { ...CREDENTIAL, status: "pending" } }); }),
    );
    mountDialog();
    await userEvent.type(screen.getByLabelText("凭据别名"), "Reviewed credential");
    await userEvent.type(screen.getByLabelText("密钥值"), "unit-only-unknown-value");
    await userEvent.click(screen.getByRole("button", { name: "保存凭据" }));
    await screen.findByText("凭据写入结果未确认");
    await userEvent.click(screen.getByRole("button", { name: "关闭凭据" }));
    await userEvent.click(screen.getByRole("button", { name: "打开凭据" }));
    expect(screen.getByLabelText("密钥值")).toHaveValue("");
    expect(screen.getByRole("button", { name: "保存凭据" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "查询原凭据写入" }));
    await waitFor(() => expect(reads).toBe(1)); expect(creates).toBe(1);
  });

  it("retains the original unknown revoke observation and aggregates it with an unrelated transient write guard", async () => {
    let revokes = 0, reads = 0;
    server.use(
      http.get(BASE + "/credentials", () => { reads += 1; return HttpResponse.json([{ ...CREDENTIAL, status: revokes > 0 ? "revoked" : "active", version: revokes > 0 ? 2 : 1 }]); }),
      http.post(BASE + `/credentials/${CREDENTIAL.credential_id}/revoke`, async ({ request }) => { revokes += 1; expect(await request.json()).toEqual({ expected_version: 1 }); return HttpResponse.error(); }),
    );
    mountPicker();
    await userEvent.click(await screen.findByRole("button", { name: "撤销 Controlled API credential" }));
    await userEvent.click(screen.getByRole("button", { name: "确认撤销" }));
    await screen.findByText("撤销结果未确认");
    await userEvent.click(screen.getByRole("button", { name: "关闭" }));
    expect(screen.getByLabelText("credential guard")).toHaveTextContent('"unsettled":true');
    const originalQuery = await screen.findByRole("button", { name: "读取原撤销凭据当前状态" });
    await userEvent.click(screen.getByRole("button", { name: "新建凭据" }));
    await userEvent.type(screen.getByLabelText("密钥值"), "unit-only-unrelated-transient");
    expect(screen.getByLabelText("credential guard")).toHaveTextContent('"dirty":true');
    await userEvent.click(screen.getByRole("button", { name: "关闭凭据" }));
    expect(screen.getByLabelText("credential guard")).toHaveTextContent('"dirty":false');
    expect(screen.getByLabelText("credential guard")).toHaveTextContent('"unsettled":true');
    await userEvent.click(originalQuery);
    expect(await screen.findByText("原撤销目标 · Controlled API credential · revoked · V2")).toBeInTheDocument();
    expect(screen.queryByText("凭据已撤销")).not.toBeInTheDocument();
    expect(screen.getByLabelText("credential guard")).toHaveTextContent('"unsettled":true');
    expect(revokes).toBe(1); expect(reads).toBe(2);
  });
});
