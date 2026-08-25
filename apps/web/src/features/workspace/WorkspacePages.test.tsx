import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { server } from "@/test/server";

import { InboxPage } from "./WorkspacePages";

const { seedSnapshotCursor } = vi.hoisted(() => ({
  seedSnapshotCursor: vi.fn(),
}));

vi.mock("@/app/WorkspaceStreamProvider", () => ({
  useWorkspaceStreamOptional: () => ({
    seedSnapshotCursor,
    notices: [],
  }),
}));

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const REQUEST_ID = "30000000-0000-0000-0000-000000000001";

describe("InboxPage", () => {
  beforeEach(() => {
    server.resetHandlers();
    seedSnapshotCursor.mockClear();
  });

  it("restores pending planning facts from the attention list after a fresh open", async () => {
    server.use(
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/planning-requests`, ({ request }) => {
        expect(new URL(request.url).searchParams.get("requires_attention")).toBe("true");
        return HttpResponse.json({
          items: [{
            id: REQUEST_ID,
            workspace_id: WORKSPACE_ID,
            kind: "create",
            status: "proposal_ready",
            current_brief_revision: 1,
            current_proposal_revision: 2,
            current_proposal_content_hash: "a".repeat(64),
            current_run_id: null,
            planning_profile_id: "profile-1",
            planning_profile_version: 1,
            profile_key: "default",
            requires_attention: true,
            version: 4,
            updated_at: "2026-08-23T08:00:00Z",
            created_at: "2026-08-23T07:00:00Z",
          }],
          next_cursor: null,
          last_change_cursor: 42,
        });
      }),
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

    expect(await screen.findByText("任务方案待确认")).toBeVisible();
    expect(screen.getByRole("link", { name: "查看任务" })).toHaveAttribute(
      "href",
      `/workspaces/${WORKSPACE_ID}/planning/${REQUEST_ID}`,
    );
    expect(seedSnapshotCursor).toHaveBeenCalledWith(42);
  });
});
