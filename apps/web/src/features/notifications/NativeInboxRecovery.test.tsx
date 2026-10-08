import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { server } from "@/test/server";
import { buildNativeInboxOriginal, emptyNativeInboxDraft, reviewNativeInboxResult, type NativeInboxIntent } from "./nativeInboxCommand";
import { NativeInboxRecovery, type NativeInboxRecoveryProps } from "./NativeInboxRecovery";

const WS = "20000000-0000-0000-0000-000000000001";
const THREAD = "60000000-0000-0000-0000-000000000001";
const FACT = "70000000-0000-0000-0000-000000000001";
const OLD = "90000000-0000-0000-0000-000000000001";
const NEXT = "90000000-0000-0000-0000-000000000002";
const SESSION = { session_id: "s", workspace_id: WS, principal_id: "operator", expires_at: "2099-01-01T00:00:00Z" };
function actions(): NativeInboxRecoveryProps["actions"] {
  return { authority: { kind: "product", session: SESSION }, authorized: true, state: emptyNativeInboxDraft({ workspaceId: WS, principalId: "operator" }),
    busy: false, error: null, storageError: null, lookupOriginal: vi.fn(async () => {}), retryStorage: vi.fn(), onSessionAuthorized: vi.fn(async () => {}) };
}
function mount(value = actions()) {
  return render(<QueryClientProvider client={new QueryClient()}><MemoryRouter><NativeInboxRecovery workspaceId={WS} actions={value} /></MemoryRouter></QueryClientProvider>);
}
function pending(kind: NativeInboxIntent["kind"] = "mark_read") {
  const scope = { workspaceId: WS, principalId: "operator" };
  const base = { threadId: THREAD, threadVersion: 4, latestFactId: FACT };
  const intent: NativeInboxIntent = kind === "requeue" ? { ...base, kind, deliveryId: OLD, routeId: "a0000000-0000-0000-0000-000000000001", routeVersion: 3, reason: "Channel restored" } : { ...base, kind };
  return { ...emptyNativeInboxDraft(scope), intent, original: buildNativeInboxOriginal(scope, intent, SESSION), status: "pending" as const };
}
describe("NativeInboxRecovery", () => {
  it("exposes real local-session entry for expired idle Product authority", async () => {
    const value = actions(); value.authorized = false; value.authority = { kind: "product", session: { ...SESSION, expires_at: "2020-01-01T00:00:00Z" } };
    server.use(http.get("/product/v1/session", () => new HttpResponse(null, { status: 401 })));
    mount(value); expect(screen.getByText("当前操作身份未确认")).toBeVisible();
    await userEvent.setup().click(screen.getByRole("button", { name: "本地会话" }));
    expect(await screen.findByRole("dialog", { name: "本地会话" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "查询原操作" })).not.toBeInTheDocument();
  });
  it("keeps positive legacy and authorized idle surfaces quiet", () => {
    const value = actions(); const rendered = mount(value); expect(screen.queryByRole("region", { name: "信箱操作记录" })).not.toBeInTheDocument();
    value.authority = { kind: "legacy" }; value.state = pending();
    rendered.rerender(<NativeInboxRecovery workspaceId={WS} actions={value} />);
    expect(screen.queryByRole("region", { name: "信箱操作记录" })).not.toBeInTheDocument();
  });
  it.each(["mark_read", "archive", "requeue"] as const)("shows original thread and explicit GET entry for %s independently of any row", async kind => {
    const value = actions(); value.state = { ...pending(kind), status: "unknown" };
    mount(value); const region = screen.getByRole("region", { name: "信箱操作记录" });
    expect(region).toHaveTextContent(THREAD); expect(region).toHaveTextContent("原操作结果未知");
    await userEvent.setup().click(screen.getByRole("button", { name: "查询原操作" })); expect(value.lookupOriginal).toHaveBeenCalledTimes(1);
  });
  it("shows applied-unmapped with original GET rather than a business success", () => {
    const value = actions(); value.state = { ...pending(), status: "applied" };
    mount(value); expect(screen.getByText("结果尚未映射")).toBeVisible(); expect(screen.getByRole("button", { name: "查询原操作" })).toBeEnabled();
    expect(screen.queryByText("已标记已读")).not.toBeInTheDocument();
  });
  it("says a requeue next round is pending without claiming delivery", () => {
    const value = actions(), draft = pending("requeue");
    value.state = reviewNativeInboxResult(draft, { command_id: draft.original!.command_id, aggregate_id: NEXT, aggregate_version: 1, event_ids: [],
      data: { notification_delivery_id: NEXT, previous_delivery_id: OLD, route_version: 3, delivery_round: 2, fencing_token: 1, delivery_status: "pending" } });
    mount(value); expect(screen.getByText("下一轮待投递")).toBeVisible(); expect(screen.queryByText("已送达")).not.toBeInTheDocument();
  });
  it("keeps original recovery available with storage errors and disables entries while busy", async () => {
    const value = actions(); value.state = pending(); value.storageError = "unavailable"; value.busy = true; mount(value);
    expect(screen.getByRole("button", { name: "查询原操作" })).toBeDisabled(); expect(screen.getByRole("button", { name: "重试本地记录" })).toBeDisabled();
  });
});
