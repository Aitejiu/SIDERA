import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { server } from "@/test/server";

import { InboxPage } from "@/features/notifications/InboxPage";

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
});
