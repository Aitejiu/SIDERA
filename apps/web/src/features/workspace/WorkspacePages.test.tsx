import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { server } from "@/test/server";

import { InboxPage } from "@/features/notifications/InboxPage";
import { CapabilitiesPage } from "./WorkspacePages";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";

describe("InboxPage", () => {
  beforeEach(() => {
    server.resetHandlers();
  });

  it("keeps the workspace page module focused on history while Inbox owns notifications", async () => {
    server.use(
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/notification-summary`, () =>
        HttpResponse.json({
          open_count: 0,
          unread_count: 0,
          progress_count: 0,
          channel_issue_count: 0,
          last_change_cursor: 42,
          recipient_type: "workspace_operator",
          recipient_id: "aitejiu",
        })),
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/notification-threads`, () =>
        HttpResponse.json({ items: [], next_cursor: null, last_change_cursor: 42 })),
    );
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/inbox`]}>
          <Routes>
            <Route path="/workspaces/:workspaceId/inbox" element={<InboxPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByText("当前没有需要处理的事项")).toBeVisible();
    expect(screen.queryByText("任务方案待确认")).not.toBeInTheDocument();
  });

  it("renders actual capability state without the old placeholder status", async () => {
    server.use(
      http.get("/healthz", () => HttpResponse.json({ status: "ok" })),
      http.get("/main-agent/v1/health", () => HttpResponse.json({
        status: "ok",
        service: "sidera-main-agent-gateway",
        provider: "available",
      })),
      http.get("/api/v1/demo/status", () => new HttpResponse(null, { status: 404 })),
      http.get("/api/v1/modules", ({ request }) => {
        const type = new URL(request.url).searchParams.get("module_type");
        return HttpResponse.json(type === "worker_adapter"
          ? [{
            id: "worker-module",
            instance_name: "worker",
            module_type: "worker_adapter",
            provider: "interview-demo-worker",
            definition_lifecycle: "active",
            instance_lifecycle: "active",
            config_version: 1,
          }]
          : [{
            id: "workspace-module",
            instance_name: "workspace",
            module_type: "workspace_provider",
            provider: "git-worktree",
            definition_lifecycle: "active",
            instance_lifecycle: "active",
            config_version: 1,
          }]);
      }),
    );
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/capabilities`]}>
          <Routes>
            <Route path="/workspaces/:workspaceId/capabilities" element={<CapabilitiesPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByText("interview-demo-worker")).toBeVisible();
    expect(screen.getByText("已配置")).toBeVisible();
    expect(screen.queryByText("入口预留")).not.toBeInTheDocument();
    expect(screen.queryByText("R5")).not.toBeInTheDocument();
  });
});
