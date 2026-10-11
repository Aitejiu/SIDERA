import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { useState } from "react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";

import { server } from "@/test/server";
import { LocalSessionDialog } from "./LocalSessionDialog";

const WS = "20000000-0000-0000-0000-000000000001";
const TOKEN = "PRIVATE_ONE_USE_TOKEN_SENTINEL";
const metadata = { session_id: "30000000-0000-0000-0000-000000000001", workspace_id: WS, principal_id: "local-human", expires_at: "2026-10-06T12:00:00Z" };
const missing = () => HttpResponse.json({ error: { code: "local_session_required" } }, { status: 401 });

function mount(workspaceId = WS) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  function Page() {
    const [open, setOpen] = useState(true);
    return open ? <LocalSessionDialog workspaceId={workspaceId} onClose={() => setOpen(false)} onAuthorized={() => undefined} /> : <p>Original page</p>;
  }
  const router = createMemoryRouter([{ path: "/workspaces/:workspaceId/agents", element: <Page /> }], { initialEntries: [`/workspaces/${workspaceId}/agents`] });
  render(<QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider>);
  return { client, router };
}

afterEach(() => { document.cookie = "sidera_csrf=; Max-Age=0; Path=/"; window.sessionStorage.clear(); window.localStorage.clear(); });

describe("explicit local browser session", () => {
  it("observes a committed exchange after an unreadable success reply", async () => {
    let exchanges = 0, reads = 0;
    server.use(
      http.get("/product/v1/session", () => { reads += 1; return exchanges ? HttpResponse.json(metadata) : missing(); }),
      http.post("/product/v1/session/bootstrap", () => { exchanges += 1; return new HttpResponse("{", { status: 200, headers: { "Content-Type": "application/json" } }); }),
    );
    const { client, router } = mount();
    await userEvent.type(await screen.findByLabelText("CLI 令牌"), TOKEN);
    await userEvent.click(screen.getByRole("button", { name: "连接" }));
    expect(await screen.findByText("local-human")).toBeInTheDocument();
    expect(screen.queryByLabelText("CLI 令牌")).not.toBeInTheDocument();
    expect(reads).toBe(2);
    expect(exchanges).toBe(1);
    expect(document.body.textContent).not.toContain(TOKEN);
    expect(JSON.stringify(client.getQueryCache().getAll())).not.toContain(TOKEN);
    expect(window.sessionStorage.length + window.localStorage.length).toBe(0);
    expect(router.state.location.pathname).not.toContain(TOKEN);
  });

  it("keeps an uncertain exchange unavailable until an explicit readonly observation settles it", async () => {
    let exchanges = 0, reads = 0, available = false;
    server.use(
      http.get("/product/v1/session", () => {
        reads += 1;
        if (!exchanges) return missing();
        return available ? HttpResponse.json(metadata) : HttpResponse.json({ error: { code: "temporary_unavailable", message: TOKEN } }, { status: 503 });
      }),
      http.post("/product/v1/session/bootstrap", () => { exchanges += 1; return HttpResponse.error(); }),
    );
    mount();
    await userEvent.type(await screen.findByLabelText("CLI 令牌"), TOKEN);
    await userEvent.click(screen.getByRole("button", { name: "连接" }));
    await screen.findByRole("alert");
    expect(reads).toBe(2);
    expect(screen.queryByLabelText("CLI 令牌")).not.toBeInTheDocument();
    expect(document.body.textContent).not.toContain(TOKEN);
    expect(screen.getByRole("button", { name: "关闭本地会话" })).toBeEnabled();
    available = true;
    await userEvent.click(screen.getByRole("button", { name: "查询当前会话" }));
    await screen.findByText("local-human");
    expect(reads).toBe(3);
    expect(exchanges).toBe(1);
  });

  it("retains last-known metadata after an uncertain logout and unavailable observation", async () => {
    let logouts = 0, exchanges = 0, reads = 0;
    let lookup: "unavailable" | "denied" | "known" = "unavailable";
    server.use(
      http.get("/product/v1/session", () => {
        reads += 1;
        if (!logouts || lookup === "known") return HttpResponse.json(metadata);
        return HttpResponse.json({ error: { code: lookup === "denied" ? "authentication_ambiguous" : "temporary_unavailable", message: TOKEN } }, { status: lookup === "denied" ? 401 : 503 });
      }),
      http.post("/product/v1/session/logout", () => { logouts += 1; return HttpResponse.error(); }),
      http.post("/product/v1/session/bootstrap", () => { exchanges += 1; return HttpResponse.error(); }),
    );
    mount();
    await screen.findByText("local-human");
    await userEvent.click(screen.getByRole("button", { name: "退出本地会话" }));
    await screen.findByRole("alert");
    expect(screen.getByText("local-human")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("会话状态未确认");
    expect(screen.queryByLabelText("CLI 令牌")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "退出本地会话" })).toBeDisabled();
    lookup = "denied";
    await userEvent.click(screen.getByRole("button", { name: "查询当前会话" }));
    await waitFor(() => expect(reads).toBe(3));
    expect(screen.getByText("local-human")).toBeInTheDocument();
    expect(screen.queryByLabelText("CLI 令牌")).not.toBeInTheDocument();
    lookup = "known";
    await userEvent.click(screen.getByRole("button", { name: "查询当前会话" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "退出本地会话" })).toBeEnabled());
    expect(reads).toBe(4);
    expect(logouts).toBe(1);
    expect(exchanges).toBe(0);
    expect(document.body.textContent).not.toContain(TOKEN);
  });

  it("permits a new explicit exchange only after uncertain logout observes confirmed absence", async () => {
    let logouts = 0, exchanges = 0, reads = 0;
    server.use(
      http.get("/product/v1/session", () => { reads += 1; return logouts ? missing() : HttpResponse.json(metadata); }),
      http.post("/product/v1/session/logout", () => { logouts += 1; return HttpResponse.error(); }),
      http.post("/product/v1/session/bootstrap", () => { exchanges += 1; return HttpResponse.error(); }),
    );
    mount();
    await screen.findByText("local-human");
    await userEvent.click(screen.getByRole("button", { name: "退出本地会话" }));
    expect(await screen.findByLabelText("CLI 令牌")).toHaveValue("");
    expect(screen.queryByText("local-human")).not.toBeInTheDocument();
    expect(logouts).toBe(1);
    expect(exchanges).toBe(0);
    expect(reads).toBe(2);
  });

  it("reads before one explicit exchange, clears the token and refetches actual metadata", async () => {
    let exchanges = 0, reads = 0;
    server.use(
      http.get("/product/v1/session", () => { reads += 1; return exchanges ? HttpResponse.json(metadata) : missing(); }),
      http.post("/product/v1/session/bootstrap", async ({ request }) => { exchanges += 1; expect(await request.json()).toEqual({ token: TOKEN }); return HttpResponse.json({ session_id: metadata.session_id, expires_at: metadata.expires_at }); }),
    );
    const { client, router } = mount();
    await screen.findByLabelText("CLI 令牌");
    expect(exchanges).toBe(0);
    await userEvent.type(screen.getByLabelText("CLI 令牌"), TOKEN);
    await userEvent.click(screen.getByRole("button", { name: "连接" }));
    expect(await screen.findByText("local-human")).toBeInTheDocument();
    expect(screen.queryByLabelText("CLI 令牌")).not.toBeInTheDocument();
    expect(exchanges).toBe(1);
    expect(reads).toBe(2);
    expect(JSON.stringify(client.getQueryCache().getAll())).not.toContain(TOKEN);
    expect(window.sessionStorage.length + window.localStorage.length).toBe(0);
    expect(router.state.location.pathname).not.toContain(TOKEN);
  });

  it("disables repeated exchange while the original response is held", async () => {
    let exchanges = 0, reads = 0, issued = false;
    let release: (() => void) | undefined;
    const held = new Promise<void>(resolve => { release = resolve; });
    server.use(
      http.get("/product/v1/session", () => { reads += 1; return issued ? HttpResponse.json(metadata) : missing(); }),
      http.post("/product/v1/session/bootstrap", async () => { exchanges += 1; await held; issued = true; return HttpResponse.json({ session_id: metadata.session_id, expires_at: metadata.expires_at }); }),
    );
    mount();
    await userEvent.type(await screen.findByLabelText("CLI 令牌"), TOKEN);
    const button = screen.getByRole("button", { name: "连接" });
    await userEvent.dblClick(button);
    await waitFor(() => expect(exchanges).toBe(1));
    try {
      expect(button).not.toBeInTheDocument();
      expect(screen.queryByLabelText("CLI 令牌")).not.toBeInTheDocument();
      const observation = screen.getByRole("button", { name: "查询当前会话" });
      expect(observation).toBeDisabled();
      await userEvent.click(observation);
      expect(exchanges).toBe(1);
      expect(reads).toBe(1);
    } finally {
      release?.();
    }
    await screen.findByText("local-human");
    expect(exchanges).toBe(1);
    expect(reads).toBe(2);
    expect(document.body.textContent).not.toContain(TOKEN);
  });

  it.each([true, false])("queries after a lost reply without resending when cookie presence is %s", async issued => {
    let exchanges = 0, reads = 0;
    server.use(
      http.get("/product/v1/session", () => { reads += 1; return exchanges && issued ? HttpResponse.json(metadata) : missing(); }),
      http.post("/product/v1/session/bootstrap", () => { exchanges += 1; return HttpResponse.error(); }),
    );
    mount();
    await userEvent.type(await screen.findByLabelText("CLI 令牌"), TOKEN);
    await userEvent.click(screen.getByRole("button", { name: "连接" }));
    if (issued) await screen.findByText("local-human");
    else { await screen.findByRole("alert"); expect(screen.getByLabelText("CLI 令牌")).toHaveValue(""); }
    expect(exchanges).toBe(1);
    expect(reads).toBe(2);
    expect(document.body.textContent).not.toContain(TOKEN);
  });

  it.each(["bootstrap_expired", "bootstrap_used", "authentication_ambiguous"])("keeps %s errors safe and never rotates or retries implicitly", async code => {
    let exchanges = 0;
    server.use(http.get("/product/v1/session", missing), http.post("/product/v1/session/bootstrap", () => { exchanges += 1; return HttpResponse.json({ error: { code, message: TOKEN } }, { status: 401 }); }));
    mount();
    await userEvent.type(await screen.findByLabelText("CLI 令牌"), TOKEN);
    await userEvent.click(screen.getByRole("button", { name: "连接" }));
    await screen.findByRole("alert");
    expect(screen.getByLabelText("CLI 令牌")).toHaveValue("");
    expect(exchanges).toBe(1);
    expect(document.body.textContent).not.toContain(TOKEN);
  });

  it("cancels without exchange, token storage or navigation", async () => {
    let exchanges = 0;
    server.use(http.get("/product/v1/session", missing), http.post("/product/v1/session/bootstrap", () => { exchanges += 1; return HttpResponse.error(); }));
    const { router } = mount();
    await userEvent.type(await screen.findByLabelText("CLI 令牌"), TOKEN);
    await userEvent.click(screen.getByRole("button", { name: "关闭本地会话" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(exchanges).toBe(0);
    expect(router.state.location.pathname).toBe(`/workspaces/${WS}/agents`);
    expect(window.sessionStorage.length + window.localStorage.length).toBe(0);
  });

  it("offers an explicit metadata-scoped link on a foreign page with zero exchange and no automatic redirect", async () => {
    let exchanges = 0;
    server.use(http.get("/product/v1/session", () => HttpResponse.json(metadata)), http.post("/product/v1/session/bootstrap", () => { exchanges += 1; return HttpResponse.error(); }));
    const foreign = "40000000-0000-0000-0000-000000000001";
    const { router } = mount(foreign);
    expect(await screen.findByRole("link", { name: "打开已授权工作区" })).toHaveAttribute("href", `/workspaces/${WS}/agents`);
    expect(router.state.location.pathname).toBe(`/workspaces/${foreign}/agents`);
    expect(exchanges).toBe(0);
  });

  it("logs out only explicitly using the existing CSRF mutation boundary", async () => {
    let loggedOut = false, writes = 0;
    document.cookie = "sidera_csrf=original-csrf; Path=/";
    server.use(
      http.get("/product/v1/session", () => loggedOut ? missing() : HttpResponse.json(metadata)),
      http.post("/product/v1/session/logout", ({ request }) => { writes += 1; expect(request.headers.get("X-Sidera-CSRF")).toBe("original-csrf"); loggedOut = true; return HttpResponse.json({ status: "revoked" }); }),
    );
    mount();
    await screen.findByText("local-human");
    expect(writes).toBe(0);
    await userEvent.click(screen.getByRole("button", { name: "退出本地会话" }));
    await screen.findByLabelText("CLI 令牌");
    expect(writes).toBe(1);
  });
});
