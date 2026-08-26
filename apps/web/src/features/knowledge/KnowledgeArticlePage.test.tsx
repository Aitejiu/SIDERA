import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { server } from "@/test/server";

import { KnowledgeArticlePage } from "./KnowledgeArticlePage";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const PAGE_ID = "30000000-0000-0000-0000-000000000001";
const CURRENT_REVISION_ID = "40000000-0000-0000-0000-000000000003";
const PREVIOUS_REVISION_ID = "40000000-0000-0000-0000-000000000002";

const currentRevision = {
  revision_id: CURRENT_REVISION_ID,
  page_id: PAGE_ID,
  workspace_id: WORKSPACE_ID,
  revision_number: 3,
  title: "Repository guide",
  summary: "Current runtime reference.",
  tags: ["architecture", "runtime"],
  content_hash: "a".repeat(64),
  change_reason: "Accepted import",
  created_by: "local_owner:aitejiu",
  created_at: "2026-08-26T01:02:03Z",
  accepted_candidate_id: null,
  sections: [
    {
      section_key: "overview",
      heading: "Overview",
      summary: "A concise overview.",
      body_markdown: "## Overview\n\nThe current runtime path.",
      ordinal: 0,
      section_hash: "b".repeat(64),
      content_byte_count: 31,
    },
  ],
  relations: [
    {
      relation_key: "runtime",
      relation_kind: "implements",
      target_page_id: "30000000-0000-0000-0000-000000000002",
      target_revision_id: null,
    },
  ],
  source_refs: ["70000000-0000-0000-0000-000000000001"],
  sources: [
    {
      source_id: "70000000-0000-0000-0000-000000000001",
      workspace_id: WORKSPACE_ID,
      source_type: "markdown_file",
      label: "docs/runtime.md",
      uri: null,
      workspace_relative_path: "docs/runtime.md",
      git_revision: "abc1234",
      digest: "c".repeat(64),
      metadata: {},
      availability: "available",
      safe_reason_code: null,
      last_checked_at: "2026-08-26T01:02:03Z",
      version: 1,
    },
  ],
};

const previousSummary = {
  revision_id: PREVIOUS_REVISION_ID,
  page_id: PAGE_ID,
  workspace_id: WORKSPACE_ID,
  revision_number: 2,
  title: "Repository guide",
  summary: "Previous runtime reference.",
  tags: ["architecture"],
  content_hash: "d".repeat(64),
  change_reason: "Initial import",
  created_by: "local_owner:aitejiu",
  created_at: "2026-08-25T01:02:03Z",
  accepted_candidate_id: null,
};

const page = {
  summary: {
    page_id: PAGE_ID,
    workspace_id: WORKSPACE_ID,
    canonical_title: "Repository guide",
    slug: "repository-guide",
    status: "active",
    version: 3,
    current_revision: currentRevision,
    updated_at: "2026-08-26T01:02:03Z",
  },
  current_revision: currentRevision,
  revision_history: [currentRevision, previousSummary],
};

function renderPage(initialEntry = `/workspaces/${WORKSPACE_ID}/knowledge/pages/${PAGE_ID}`) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route path="/workspaces/:workspaceId/knowledge/pages/:pageId" element={<KnowledgeArticlePage />} />
          <Route path="/workspaces/:workspaceId/knowledge/import" element={<div data-testid="import-route" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("KnowledgeArticlePage", () => {
  beforeEach(() => {
    server.resetHandlers();
    server.use(
      http.get(
        `/knowledge-api/api/v1/workspaces/${WORKSPACE_ID}/knowledge/pages/${PAGE_ID}`,
        () => HttpResponse.json(page),
      ),
    );
  });

  it("shows the article body and a narrow revision/source inspector", async () => {
    renderPage();

    expect(await screen.findByRole("heading", { name: "Repository guide" })).toBeVisible();
    expect(screen.getAllByRole("heading", { name: "Overview" })).toHaveLength(2);
    expect(screen.getByText("The current runtime path.")).toBeVisible();
    expect(screen.getByText("当前版本")).toBeVisible();
    expect(screen.getByText("REV 3", { selector: "dd" })).toBeVisible();
    expect(screen.getByText("docs/runtime.md")).toBeVisible();
    expect(screen.getByText("可用")).toBeVisible();
    expect(screen.getByText("implements")).toBeVisible();
    expect(screen.getByRole("button", { name: "基于当前版本提出修改" })).toBeVisible();
  });

  it("reads an exact revision and requests a bounded diff from history", async () => {
    const user = userEvent.setup();
    let diffCalls = 0;
    server.use(
      http.get(
        `/knowledge-api/api/v1/workspaces/${WORKSPACE_ID}/knowledge/pages/${PAGE_ID}/revisions/${PREVIOUS_REVISION_ID}`,
        () => HttpResponse.json({ ...currentRevision, revision_id: PREVIOUS_REVISION_ID, revision_number: 2, sections: [{ ...currentRevision.sections[0], body_markdown: "Old body" }] }),
      ),
      http.get(
        `/knowledge-api/api/v1/workspaces/${WORKSPACE_ID}/knowledge/pages/${PAGE_ID}/diff`,
        ({ request }) => {
          diffCalls += 1;
          const query = new URL(request.url).searchParams;
          expect(query.get("from_revision_id")).toBe(PREVIOUS_REVISION_ID);
          expect(query.get("to_revision_id")).toBe(CURRENT_REVISION_ID);
          return HttpResponse.json({
            from_revision_id: PREVIOUS_REVISION_ID,
            to_revision_id: CURRENT_REVISION_ID,
            title_changed: false,
            summary_changed: true,
            tags_added: ["runtime"],
            tags_removed: [],
            sections_added: [],
            sections_removed: [],
            sections_changed: [{ section_key: "overview", before_hash: "d".repeat(64), after_hash: "b".repeat(64) }],
            relations_added: [],
            relations_removed: [],
            relations_changed: [],
            sources_added: [],
            sources_removed: [],
            body_unified_diff: "@@ -1 +1 @@\n-Old body\n+The current runtime path.",
            truncated: false,
          });
        },
      ),
    );
    renderPage(`/workspaces/${WORKSPACE_ID}/knowledge/pages/${PAGE_ID}?revision_id=${PREVIOUS_REVISION_ID}`);

    expect(await screen.findByText("Old body")).toBeVisible();
    await user.selectOptions(screen.getByLabelText("对比左侧 revision"), PREVIOUS_REVISION_ID);
    await user.selectOptions(screen.getByLabelText("对比右侧 revision"), CURRENT_REVISION_ID);
    await user.click(screen.getByRole("button", { name: "查看 diff" }));

    expect(await within(screen.getByRole("region", { name: "Revision diff" })).findByText(/Old body/)).toBeVisible();
    expect(diffCalls).toBe(1);
    expect(within(screen.getByRole("region", { name: "Revision diff" })).getByText(/新增 runtime/)).toBeVisible();
  });
});
