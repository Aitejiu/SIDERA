import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { server } from "@/test/server";
import type { CommandRequest } from "../assignments/types";
import type { NotificationThreadDetail } from "./api";
import { InboxPage } from "./InboxPage";
import { nativeInboxDraftKey } from "./nativeInboxCommand";

const WS = "20000000-0000-0000-0000-000000000001";
const OTHER = "20000000-0000-0000-0000-000000000002";
const THREAD = "60000000-0000-0000-0000-000000000001";
const FACT = "70000000-0000-0000-0000-000000000001";
const OLD = "90000000-0000-0000-0000-000000000001";
const NEXT = "90000000-0000-0000-0000-000000000002";
const ROUTE = "a0000000-0000-0000-0000-000000000001";
const SESSION = { session_id: "human", workspace_id: WS, principal_id: "current-human", expires_at: "2099-01-01T00:00:00Z" };
const scope = { workspaceId: WS, principalId: SESSION.principal_id };
function detail(): NotificationThreadDetail {
  return { thread: { id: THREAD, workspace_id: WS, recipient_type: "workspace_operator", recipient_id: "local", category: "action",
    business_status: "resolved", organization_status: "active", priority: "high", root_task_id: "30000000-0000-0000-0000-000000000001", root_task_title: "Root", subtask_id: null, subtask_title: null,
    worker_item_id: null, worker_item_title: null, title: "Native thread", summary: "Channel needs review", action: "Review", topic: "notification.delivery",
    notification_ref: "native-ref", latest_fact_id: FACT, latest_fact_occurred_at: "2026-10-07T08:30:00Z", last_read_fact_id: null, unread_count: 1, version: 4,
    deep_link: "", opened_at: "2026-10-07T08:00:00Z", resolved_at: "2026-10-07T08:30:00Z", archived_at: null, updated_at: "2026-10-07T08:30:00Z" },
    facts: { items: [], next_cursor: null }, deliveries: [{ delivery_id: OLD, route_id: ROUTE, route_version: 3, channel_type: "local", contract_version: 1,
      status: "exhausted", attempts: 10, next_attempt_at: "2026-10-07T09:00:00Z", last_error_code: "provider_unavailable", delivered_at: null,
      exhausted_at: "2026-10-07T09:00:00Z", created_at: "2026-10-07T08:30:00Z", updated_at: "2026-10-07T09:00:00Z" }],
    notification_ref: "native-ref", available_commands: ["mark_read", "archive"], deep_link: "", last_change_cursor: 42 };
}
function deferred() { let release!: () => void; const promise = new Promise<void>(resolve => { release = resolve; }); return { promise, release }; }
function handlers() {
  const world = { health: { status: "ok", service: "sidera-product" }, session: { ...SESSION }, detail: detail(), rows: true,
    listFails: false, readFails: false, sessionFails: false, lost: false, appliedNull: false, version: 4, fencing: 1,
    readGate: null as ReturnType<typeof deferred> | null, postGate: null as ReturnType<typeof deferred> | null };
  const posted: CommandRequest[] = [], lookups: string[] = [], csrf: (string | null)[] = [], saved: unknown[] = [], unexpected: string[] = [];
  const requests: string[] = [];
  function trace(request: Request) { requests.push(`${request.method} ${new URL(request.url).pathname}`); }
  function legal(command: CommandRequest) {
    const requeue = command.command_type === "RequeueNotificationDelivery";
    return { command_id: command.command_id, aggregate_id: requeue ? NEXT : THREAD, aggregate_version: requeue ? world.fencing : world.version, event_ids: [],
      data: requeue ? { notification_delivery_id: NEXT, previous_delivery_id: OLD, route_version: 3, delivery_status: "pending", delivery_round: 2, fencing_token: world.fencing }
        : { notification_thread_id: THREAD, thread_version: world.version, latest_fact_id: FACT, organization_status: "archived" } };
  }
  server.use(
    http.get("/main-agent/v1/health", ({ request }) => { trace(request); return HttpResponse.json(world.health); }),
    http.get("/product/v1/session", ({ request }) => { trace(request); return world.sessionFails ? new HttpResponse(null, { status: 401 }) : HttpResponse.json(world.session); }),
    http.get("/api/v1/workspaces/:workspace/notification-summary", ({ request }) => { trace(request); return HttpResponse.json({ open_count: 1, unread_count: 1, progress_count: 0, channel_issue_count: 1, last_change_cursor: 42, recipient_type: "workspace_operator", recipient_id: "local" }); }),
    http.get("/api/v1/workspaces/:workspace/notification-threads", ({ request }) => { trace(request); return world.listFails ? new HttpResponse(null, { status: 503 }) : HttpResponse.json({ items: world.rows ? [world.detail.thread] : [], next_cursor: null, last_change_cursor: 42 }); }),
    http.get("/api/v1/workspaces/:workspace/notification-threads/:thread", async ({ request }) => {
      trace(request); const captured = structuredClone(world.detail); if (world.readGate) await world.readGate.promise;
      return world.readFails ? new HttpResponse(null, { status: 503 }) : HttpResponse.json(captured);
    }),
    http.post("/api/v1/commands", async ({ request }) => {
      trace(request); const command = await request.json() as CommandRequest; posted.push(command); csrf.push(request.headers.get("X-Sidera-CSRF"));
      saved.push(JSON.parse(localStorage.getItem(nativeInboxDraftKey(scope)) ?? "null"));
      if (world.postGate) await world.postGate.promise;
      return world.lost ? HttpResponse.error() : HttpResponse.json(world.appliedNull ? { ...legal(command), data: {} } : legal(command));
    }),
    http.get("/api/v1/workspaces/:workspace/commands/:command", ({ params, request }) => {
      trace(request); lookups.push(String(params.command)); const original = posted.find(command => command.command_id === params.command)!;
      return HttpResponse.json({ command_id: original.command_id, status: "applied", result: world.appliedNull ? null : legal(original) });
    }),
    http.all("*", ({ request }) => { trace(request); unexpected.push(`${request.method} ${new URL(request.url).pathname}`); return new HttpResponse(null, { status: 404 }); }),
  );
  return { world, posted, lookups, csrf, saved, requests, unexpected };
}
function SwitchWorkspace() { const navigate = useNavigate(); return <button onClick={() => void navigate(`/workspaces/${OTHER}/inbox`)}>Switch workspace</button>; }
function mount() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
  const rendered = render(<QueryClientProvider client={queryClient}><MemoryRouter initialEntries={[`/workspaces/${WS}/inbox`]}><Routes>
    <Route path="/workspaces/:workspaceId/inbox" element={<><SwitchWorkspace /><InboxPage /></>} />
  </Routes></MemoryRouter></QueryClientProvider>);
  return { ...rendered, queryClient };
}
async function expand() { await screen.findByText("Native thread"); await userEvent.setup().click(screen.getByRole("button", { name: "展开Native thread" })); return screen.findByRole("region", { name: "Native thread的线程历史" }); }
async function clickAction(kind: "mark_read" | "archive" | "requeue") {
  const user = userEvent.setup();
  if (kind !== "requeue") { const button = await screen.findByRole("button", { name: kind === "mark_read" ? "标记已读" : "归档线程" }); await waitFor(() => expect(button).toBeEnabled()); await user.click(button); }
  else { const button = screen.getByRole("button", { name: "重新投递" }); await waitFor(() => expect(button).toBeEnabled()); await user.click(button);
    await user.type(screen.getByLabelText("重新投递原因"), "  Channel restored  "); await user.click(screen.getByRole("button", { name: "确认重新投递" })); }
}
beforeEach(() => { localStorage.clear(); document.cookie = "sidera_csrf=real-inbox-csrf; path=/"; });
afterEach(() => { vi.restoreAllMocks(); document.cookie = "sidera_csrf=; Max-Age=0; path=/"; });

describe("actual native InboxPage", () => {
  it.each([
    ["mark_read", 4], ["mark_read", 5], ["archive", 4], ["archive", 5], ["requeue", 1], ["requeue", 2],
  ] as const)("real native button %s maps legal version %s with current user/CSRF", async (kind, version) => {
    const h = handlers(); h.world.version = version; h.world.fencing = version; mount(); await expand(); await clickAction(kind);
    await waitFor(() => expect(h.posted).toHaveLength(1));
    expect(h.posted[0]?.actor).toEqual({ type: "user", id: "current-human" }); expect(h.csrf).toEqual(["real-inbox-csrf"]);
    expect(h.posted[0]?.target_id).toBe(kind === "requeue" ? OLD : THREAD); expect(h.posted[0]?.expected_version).toBe(kind === "requeue" ? null : 4);
    expect(h.posted[0]?.payload).toEqual(kind === "mark_read" ? { expected_latest_fact_id: FACT } : kind === "archive" ? {} : { expected_route_version: 3, expected_status: "exhausted", reason: "Channel restored" });
    expect(h.saved[0]).toMatchObject({ status: "pending", original: h.posted[0] });
    expect(await screen.findByText(kind === "mark_read" ? "已标记已读" : kind === "archive" ? "线程已归档" : "下一轮待投递")).toBeVisible();
    expect(h.unexpected).toEqual([]);
  });
  it.each(["collapse", "removed", "archived", "category", "list error", "reload"])("lost reply original GET survives %s", async transition => {
    const h = handlers(); h.world.lost = true; const rendered = mount(); await expand(); await clickAction("requeue");
    expect(await screen.findByText("原操作结果未知")).toBeVisible(); const original = h.posted[0]!;
    if (transition === "collapse") await userEvent.setup().click(screen.getByRole("button", { name: "收起Native thread" }));
    if (transition === "removed" || transition === "archived") { h.world.rows = false; await act(async () => { await rendered.queryClient.invalidateQueries({ queryKey: ["notifications", WS] }); }); }
    if (transition === "archived") { h.world.detail.thread.organization_status = "archived"; h.world.detail.available_commands = []; }
    if (transition === "category") await userEvent.setup().click(screen.getByRole("tab", { name: "进度通报 0" }));
    if (transition === "list error") { rendered.unmount(); h.world.listFails = true; mount(); expect(await screen.findByText("无法读取信箱")).toBeVisible(); }
    if (transition === "reload") { rendered.unmount(); h.world.rows = false; mount(); }
    h.world.detail.deliveries = []; h.world.detail.available_commands = []; h.world.readFails = true;
    const recovery = await screen.findByRole("region", { name: "信箱操作记录" }); await waitFor(() => expect(recovery).toHaveTextContent(THREAD));
    const identity = vi.spyOn(crypto, "randomUUID"); const readsBefore = h.requests.filter(request => request.endsWith(`/notification-threads/${THREAD}`)).length;
    await userEvent.setup().click(within(recovery).getByRole("button", { name: "查询原操作" }));
    expect(await screen.findByText("下一轮待投递")).toBeVisible(); expect(h.lookups).toEqual([original.command_id]); expect(h.posted).toHaveLength(1);
    expect(identity).not.toHaveBeenCalled(); expect(h.requests.filter(request => request.endsWith(`/notification-threads/${THREAD}`))).toHaveLength(readsBefore);
    expect(h.unexpected).toEqual([]);
  });
  it("APPLIED-null keeps original GET visible outside the collapsed row", async () => {
    const h = handlers(); h.world.appliedNull = true; mount(); await expand(); await clickAction("mark_read");
    expect(await screen.findByText("结果尚未映射")).toBeVisible(); await userEvent.setup().click(screen.getByRole("button", { name: "收起Native thread" }));
    await userEvent.setup().click(screen.getByRole("button", { name: "查询原操作" })); await waitFor(() => expect(h.lookups).toHaveLength(1));
    await expand(); expect(screen.getByRole("button", { name: "标记已读" })).toBeDisabled(); expect(h.posted).toHaveLength(1);
  });
  it.each(["expired", "foreign workspace", "denied", "unknown host"])("mount %s keeps real actions disabled and creates no command", async failure => {
    const h = handlers(); if (failure === "expired") h.world.session.expires_at = "2020-01-01T00:00:00Z";
    if (failure === "foreign workspace") h.world.session.workspace_id = OTHER; if (failure === "denied") h.world.sessionFails = true; if (failure === "unknown host") h.world.health.service = "unknown";
    const identity = vi.spyOn(crypto, "randomUUID"), writes = vi.spyOn(Storage.prototype, "setItem"); mount(); await expand();
    expect(screen.getByRole("button", { name: "标记已读" })).toBeDisabled(); expect(screen.getByRole("button", { name: "归档线程" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "重新投递" })).toBeDisabled(); expect(screen.getByRole("button", { name: "本地会话" })).toBeVisible();
    expect(h.posted).toHaveLength(0); expect(identity).not.toHaveBeenCalled(); expect(writes).not.toHaveBeenCalled(); expect(h.unexpected).toEqual([]);
  });
  it.each(["denied", "foreign principal", "read failure", "version drift", "latest fact drift", "permission drift", "save failure"])("actual click %s yields POST0", async failure => {
    const h = handlers(); mount(); await expand(); await waitFor(() => expect(screen.getByRole("button", { name: "标记已读" })).toBeEnabled());
    if (failure === "denied") h.world.sessionFails = true; if (failure === "foreign principal") h.world.session.principal_id = "foreign";
    if (failure === "read failure") h.world.readFails = true; if (failure === "version drift") h.world.detail.thread.version = 5;
    if (failure === "latest fact drift") h.world.detail.thread.latest_fact_id = NEXT; if (failure === "permission drift") h.world.detail.available_commands = [];
    if (failure === "save failure") vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("private secret"); });
    await clickAction("mark_read"); expect(await screen.findByRole("region", { name: "信箱操作记录" })).toBeVisible();
    expect(h.posted).toHaveLength(0); expect(screen.getByRole("region", { name: "信箱操作记录" })).not.toHaveTextContent("private secret"); expect(h.unexpected).toEqual([]);
  });
  it("a missing route version disables requeue but allows mark-read and original lookup", async () => {
    const h = handlers(); h.world.detail.deliveries[0]!.route_version = null; mount(); await expand();
    expect(screen.getByRole("button", { name: "重新投递" })).toBeDisabled(); await clickAction("mark_read"); expect(await screen.findByText("已标记已读")).toBeVisible();
    await userEvent.setup().click(screen.getByRole("button", { name: "查询原操作" })); await waitFor(() => expect(h.lookups).toHaveLength(1)); expect(h.posted).toHaveLength(1);
  });
  it("same-tick actual button clicks create only one POST", async () => {
    const h = handlers(); h.world.lost = true; mount(); await expand(); const button = screen.getByRole("button", { name: "标记已读" }); await waitFor(() => expect(button).toBeEnabled());
    act(() => { fireEvent.click(button); fireEvent.click(button); }); expect(await screen.findByText("原操作结果未知")).toBeVisible(); expect(h.posted).toHaveLength(1);
  });
  it("held real preflight A to B creates identity0/save0/POST0", async () => {
    const h = handlers(); mount(); await expand(); await waitFor(() => expect(screen.getByRole("button", { name: "标记已读" })).toBeEnabled()); h.world.readGate = deferred();
    const identity = vi.spyOn(crypto, "randomUUID"), writes = vi.spyOn(Storage.prototype, "setItem"); await clickAction("mark_read");
    await waitFor(() => expect(h.requests.filter(request => request.endsWith(`/notification-threads/${THREAD}`))).toHaveLength(2));
    h.world.session.workspace_id = OTHER; await userEvent.setup().click(screen.getByRole("button", { name: "Switch workspace" }));
    await act(async () => { h.world.readGate!.release(); }); await waitFor(() => expect(h.requests.some(request => request.includes(OTHER))).toBe(true));
    expect(h.posted).toHaveLength(0); expect(identity).not.toHaveBeenCalled(); expect(writes).not.toHaveBeenCalled();
  });
  it("held real POST A to B preserves A pending and does not invalidate B", async () => {
    const h = handlers(); const rendered = mount(); await expand(); h.world.postGate = deferred(); await clickAction("mark_read"); await waitFor(() => expect(h.posted).toHaveLength(1));
    const storedA = localStorage.getItem(nativeInboxDraftKey(scope)); h.world.session.workspace_id = OTHER;
    await userEvent.setup().click(screen.getByRole("button", { name: "Switch workspace" })); await waitFor(() => expect(h.requests.some(request => request.includes(OTHER))).toBe(true));
    const invalidations = vi.spyOn(rendered.queryClient, "invalidateQueries"), writes = vi.spyOn(Storage.prototype, "setItem");
    await act(async () => { h.world.postGate!.release(); });
    expect(localStorage.getItem(nativeInboxDraftKey(scope))).toBe(storedA); expect(writes).not.toHaveBeenCalled(); expect(invalidations).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "查询原操作" })).not.toBeInTheDocument();
  });
  it("handled result invalidates the entire workspace prefix after collapse and keeps handling on refresh failure", async () => {
    const h = handlers(); const rendered = mount(); await expand(); h.world.postGate = deferred(); await clickAction("archive"); await waitFor(() => expect(h.posted).toHaveLength(1));
    await userEvent.setup().click(screen.getByRole("button", { name: "收起Native thread" }));
    const invalidations = vi.spyOn(rendered.queryClient, "invalidateQueries").mockRejectedValue(new Error("private refresh"));
    await act(async () => { h.world.postGate!.release(); }); expect(await screen.findByText("线程已归档")).toBeVisible();
    expect(invalidations).toHaveBeenCalledWith({ queryKey: ["notifications", WS] });
    expect(JSON.parse(localStorage.getItem(nativeInboxDraftKey(scope))!)).toMatchObject({ status: "applied", outcome: { kind: "archive" } });
  });
  it("real local-session recheck clears equal cached authority denial with no identity/write/POST", async () => {
    const h = handlers(); h.world.lost = true; const rendered = mount(); await expand(); await clickAction("mark_read"); expect(await screen.findByText("原操作结果未知")).toBeVisible();
    const cached = rendered.queryClient.getQueryData(["native-inbox-authority", WS]), stored = localStorage.getItem(nativeInboxDraftKey(scope)); h.world.sessionFails = true;
    await userEvent.setup().click(screen.getByRole("button", { name: "查询原操作" })); expect(await screen.findByText("当前操作身份未确认，请重新授权本地会话。")).toBeVisible();
    await userEvent.setup().click(screen.getByRole("button", { name: "本地会话" }));
    expect(await screen.findByRole("button", { name: "查询当前会话" })).toBeVisible(); const identity = vi.spyOn(crypto, "randomUUID"), writes = vi.spyOn(Storage.prototype, "setItem");
    h.world.sessionFails = false; await userEvent.setup().click(screen.getByRole("button", { name: "查询当前会话" }));
    await waitFor(() => expect(screen.queryByText("当前操作身份未确认，请重新授权本地会话。")).not.toBeInTheDocument());
    expect(rendered.queryClient.getQueryData(["native-inbox-authority", WS])).toBe(cached); expect(localStorage.getItem(nativeInboxDraftKey(scope))).toBe(stored);
    expect(identity).not.toHaveBeenCalled(); expect(writes).not.toHaveBeenCalled(); expect(h.posted).toHaveLength(1); expect(h.lookups).toHaveLength(0);
  });
  it("mount, categories and list-read retry make no command or other business writes", async () => {
    const h = handlers(); h.world.listFails = true; mount(); const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "重试读取信箱" })); h.world.listFails = false;
    await user.click(screen.getByRole("button", { name: "重试读取信箱" })); await screen.findByText("Native thread");
    await user.click(screen.getByRole("tab", { name: "进度通报 0" })); await user.click(screen.getByRole("tab", { name: "已归档" }));
    expect(h.posted).toHaveLength(0); expect(h.lookups).toHaveLength(0); expect(h.requests.every(request => request.startsWith("GET "))).toBe(true); expect(h.unexpected).toEqual([]);
  });
});
