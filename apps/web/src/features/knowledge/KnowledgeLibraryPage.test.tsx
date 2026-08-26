import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { server } from "@/test/server";

import { knowledgePagesQueryKey } from "./api";
import { KnowledgeLibraryPage } from "./KnowledgeLibraryPage";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const PAGE_ID = "30000000-0000-0000-0000-000000000001";
const OTHER_PAGE_ID = "30000000-0000-0000-0000-000000000002";

const pages = {
  items: [
    {
      page_id: PAGE_ID,
      workspace_id: WORKSPACE_ID,
      canonical_title: "Repository guide",
      slug: "repository-guide",
      status: "active",
      version: 3,
      current_revision: {
        revision_id: "40000000-0000-0000-0000-000000000001",
        page_id: PAGE_ID,
        workspace_id: WORKSPACE_ID,
        revision_number: 3,
        title: "Repository guide",
        summary: "The current project reference.",
        tags: ["architecture", "r5"],
        content_hash: "a".repeat(64),
        change_reason: "markdown_file",
        created_by: "local_owner",
        created_at: "2026-08-26T01:02:03Z",
        accepted_candidate_id: null,
        source_types: ["markdown_file"],
      },
      updated_at: "2026-08-26T01:02:03Z",
    },
    {
      page_id: OTHER_PAGE_ID,
      workspace_id: WORKSPACE_ID,
      canonical_title: "Worker handoff",
      slug: "worker-handoff",
      status: "archived",
      version: 1,
      current_revision: null,
      updated_at: "2026-08-20T01:02:03Z",
    },
  ],
  next_cursor: null,
};

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const rendered = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/knowledge`]}>
        <Routes>
          <Route path="/workspaces/:workspaceId/knowledge" element={<KnowledgeLibraryPage />} />
          <Route
            path="/workspaces/:workspaceId/knowledge/pages/:pageId"
            element={<div data-testid="knowledge-page-route">知识页已打开</div>}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...rendered, queryClient };
}

describe("KnowledgeLibraryPage", () => {
  beforeEach(() => {
    server.resetHandlers();
    server.use(
      http.get(
        `/knowledge-api/api/v1/workspaces/${WORKSPACE_ID}/knowledge/pages`,
        () => HttpResponse.json(pages),
      ),
    );
  });

  it("shows a compact knowledge table and opens a page semantic tab", async () => {
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByRole("heading", { name: "知识库" })).toBeVisible();
    expect(await screen.findByText("Repository guide")).toBeVisible();
    expect(screen.getByText("The current project reference.")).toBeVisible();
    expect(screen.getByText("architecture")).toBeVisible();
    expect(screen.getByText("markdown_file")).toBeVisible();
    expect(within(screen.getByRole("link", { name: "打开知识页 Worker handoff" })).getByText("归档")).toBeVisible();
    expect(screen.getByLabelText("知识页列表")).toBeVisible();

    await user.click(screen.getByRole("link", { name: "打开知识页 Repository guide" }));
    expect(await screen.findByTestId("knowledge-page-route")).toBeVisible();
  });

  it("debounces free-text search and supports Enter for immediate search", async () => {
    const user = userEvent.setup();
    let searchCalls = 0;
    server.use(
      http.get(
        `/knowledge-api/api/v1/workspaces/${WORKSPACE_ID}/knowledge/search`,
        ({ request }) => {
          searchCalls += 1;
          expect(new URL(request.url).searchParams.get("q")).toBe("context");
          return HttpResponse.json({
            items: [
              {
                page_id: PAGE_ID,
                revision_id: "40000000-0000-0000-0000-000000000001",
                section_key: "overview",
                revision_number: 3,
                revision_hash: "a".repeat(64),
                section_hash: "b".repeat(64),
                title: "Context package",
                summary: "A context reference.",
                tags: ["r5"],
                source_ids: [],
                source_types: ["task"],
                source_availability: ["available"],
                trust: "owner",
                section_byte_count: 20,
                rank: 0.9,
                rank_explanation: "title match",
              },
            ],
            next_cursor: null,
          });
        },
      ),
    );
    renderPage();

    const search = screen.getByRole("searchbox", { name: "搜索知识" });
    await user.type(search, "context");
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(searchCalls).toBe(0);

    await user.keyboard("{Enter}");
    expect(await screen.findByText("Context package")).toBeVisible();
    expect(searchCalls).toBe(1);
  });

  it("sends status, tag and source filters to the knowledge search endpoint", async () => {
    const user = userEvent.setup();
    server.use(
      http.get(
        `/knowledge-api/api/v1/workspaces/${WORKSPACE_ID}/knowledge/search`,
        ({ request }) => {
          const query = new URL(request.url).searchParams;
          expect(query.get("status")).toBe("archived");
          expect(query.get("tags")).toBe("architecture");
          expect(query.get("source_types")).toBe("markdown_file");
          return HttpResponse.json({ items: [], next_cursor: null });
        },
      ),
    );
    renderPage();

    await user.selectOptions(screen.getByLabelText("状态筛选"), "archived");
    await user.clear(screen.getByLabelText("标签筛选"));
    await user.type(screen.getByLabelText("标签筛选"), "architecture");
    await user.selectOptions(screen.getByLabelText("来源类型筛选"), "markdown_file");

    await waitFor(() => expect(screen.getByText("没有匹配的知识页")).toBeVisible());
  });

  it("keeps a retryable error state and an explicit empty state", async () => {
    server.use(
      http.get(
        `/knowledge-api/api/v1/workspaces/${WORKSPACE_ID}/knowledge/pages`,
        () => new HttpResponse(null, { status: 503 }),
      ),
    );
    renderPage();

    expect(await screen.findByRole("button", { name: "重试读取知识页" })).toBeVisible();
    expect(screen.getByText("无法读取知识页")).toBeVisible();
  });

  it("uses low-frequency foreground refresh for the page library", async () => {
    const { queryClient } = renderPage();

    await screen.findByText("Repository guide");

    const query = queryClient.getQueryCache().find({
      queryKey: knowledgePagesQueryKey(WORKSPACE_ID),
    });
    const options = query?.options as {
      refetchInterval?: number;
      refetchIntervalInBackground?: boolean;
      refetchOnWindowFocus?: boolean;
    } | undefined;
    expect(options?.refetchInterval).toBe(15_000);
    expect(options?.refetchIntervalInBackground).toBe(false);
    expect(options?.refetchOnWindowFocus).toBe(true);
  });
});
