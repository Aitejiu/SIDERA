import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { server } from "@/test/server";

import { NotificationSettingsPage } from "./NotificationSettingsPage";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";

describe("NotificationSettingsPage", () => {
  beforeEach(() => {
    server.resetHandlers();
    server.use(
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/notification-summary`, () =>
        HttpResponse.json({
          open_count: 2,
          unread_count: 1,
          progress_count: 3,
          channel_issue_count: 1,
          last_change_cursor: 42,
          recipient_type: "workspace_operator",
          recipient_id: "aitejiu",
        })),
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/notification-readiness`, () =>
        HttpResponse.json({
          component: "notification_gateway",
          as_of: "2026-09-04T08:00:00Z",
          status: "ready",
          checks: [{
            name: "worker_running",
            ready: true,
            code: "ok",
            summary: "Notification worker is running",
          }],
        })),
      http.get("/api/v1/modules", ({ request }) => {
        expect(new URL(request.url).searchParams.get("module_type")).toBe("notification_channel");
        return HttpResponse.json([{
          id: "60000000-0000-0000-0000-000000000001",
          instance_name: "akashic-private",
          provider: "akashic",
          module_type: "notification_channel",
          config_version: 1,
          definition_lifecycle: "active",
          instance_lifecycle: "active",
        }]);
      }),
    );
  });

  it("shows operational lifecycle without exposing adapter-private fields", async () => {
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/settings`]}>
          <Routes>
            <Route path="/workspaces/:workspaceId/settings" element={<NotificationSettingsPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByRole("heading", { name: "通知设置" })).toBeVisible();
    expect(await screen.findByText("akashic-private")).toBeVisible();
    expect(screen.getByText("已就绪")).toBeVisible();
    expect(screen.getByText("积压与耗尽投递")).toBeVisible();
    expect(screen.queryByText("60000000-0000-0000-0000-000000000001")).not.toBeInTheDocument();
    expect(screen.queryByText(/source_ref|route_id|token/i)).not.toBeInTheDocument();
  });
});
