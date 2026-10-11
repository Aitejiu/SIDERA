import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { server } from "@/test/server";
import { useNativeTransitionRecovery } from "./useNativeTransitionRecovery";
import { NativeTransitionRecoveryStatus } from "./NativeTransitionRecoveryStatus";

const WS = "20000000-0000-0000-0000-000000000001", WI = "40000000-0000-0000-0000-000000000001";
const SESSION = { session_id: "d0000000-0000-0000-0000-000000000001", workspace_id: WS,
  principal_id: "current-operator", expires_at: "2099-01-01T00:00:00Z" };
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
  function Subject() {
    const actions = useNativeTransitionRecovery({ workspaceId: WS, workerItemId: WI, onRefresh: async () => {} });
    return <NativeTransitionRecoveryStatus workspaceId={WS} actions={actions} />;
  }
  return render(<QueryClientProvider client={client}><MemoryRouter><Subject /></MemoryRouter></QueryClientProvider>);
}
describe("native transition status authority entry", () => {
  beforeEach(() => { localStorage.clear(); });
  afterEach(() => vi.restoreAllMocks());
  it("exposes explicit local-session entry after idle cached Product authority expires", async () => {
    const session = { ...SESSION, expires_at: new Date(Date.now() + 400).toISOString() };
    server.use(http.get("/main-agent/v1/health", () => HttpResponse.json({ status: "ok", service: "sidera-product" })),
      http.get("/product/v1/session", () => HttpResponse.json(session)));
    mount();
    await waitFor(() => expect(screen.queryByRole("region", { name: "计划同步恢复记录" })).not.toBeInTheDocument());
    const identity = vi.spyOn(crypto, "randomUUID"), save = vi.spyOn(Storage.prototype, "setItem");
    const region = await screen.findByRole("region", { name: "计划同步恢复记录" });
    expect(within(region).getByRole("button", { name: "本地会话" })).toBeEnabled();
    expect(within(region).getByText("当前操作身份未确认")).toBeVisible();
    expect(identity).not.toHaveBeenCalled(); expect(save).not.toHaveBeenCalled();
  });
  it.each(["unavailable", "expired"])("keeps %s idle authority connected to the real local-session dialog", async state => {
    server.use(http.get("/main-agent/v1/health", () => HttpResponse.json({ status: "ok", service: "sidera-product" })),
      http.get("/product/v1/session", () => HttpResponse.json(state === "unavailable" ? null : { ...SESSION, expires_at: "2000-01-01T00:00:00Z" })));
    const user = userEvent.setup(); mount();
    const region = await screen.findByRole("region", { name: "计划同步恢复记录" });
    await user.click(within(region).getByRole("button", { name: "本地会话" }));
    expect(await screen.findByRole("dialog", { name: "本地会话" })).toBeVisible();
    expect(within(region).queryByRole("button", { name: "查询原恢复" })).not.toBeInTheDocument();
  });
  it("does not present a native record under positively confirmed legacy host", async () => {
    server.use(http.get("/main-agent/v1/health", () => HttpResponse.json({ status: "ok", service: "sidera-main-agent-gateway" })));
    mount(); await waitFor(() => expect(screen.queryByRole("region", { name: "计划同步恢复记录" })).not.toBeInTheDocument());
  });
});
