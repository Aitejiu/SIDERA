import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { server } from "@/test/server";

import { knowledgeCandidatesQueryKey } from "./api";
import { KnowledgeInboxPage } from "./KnowledgeInboxPage";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const CANDIDATE_ID = "50000000-0000-0000-0000-000000000001";

const candidates = {
  items: [
    {
      candidate_id: CANDIDATE_ID,
      workspace_id: WORKSPACE_ID,
      operation: "create_page",
      target_page_id: null,
      expected_revision_id: null,
      expected_revision_hash: null,
      proposed_title: "New runtime guide",
      proposed_slug: "new-runtime-guide",
      candidate_hash: "a".repeat(64),
      status: "pending",
      producer_type: "main_agent",
      producer_id: "aitejiu",
      task_id: "30000000-0000-0000-0000-000000000001",
      subtask_id: "31000000-0000-0000-0000-000000000001",
      worker_item_id: "32000000-0000-0000-0000-000000000001",
      execution_id: "33000000-0000-0000-0000-000000000001",
      supersedes_candidate_id: null,
      version: 1,
      created_at: "2026-08-26T01:02:03Z",
      updated_at: "2026-08-26T01:02:03Z",
    },
    {
      candidate_id: "50000000-0000-0000-0000-000000000002",
      workspace_id: WORKSPACE_ID,
      operation: "revise_page",
      target_page_id: "34000000-0000-0000-0000-000000000001",
      expected_revision_id: "35000000-0000-0000-0000-000000000001",
      expected_revision_hash: "b".repeat(64),
      proposed_title: "Updated task guide",
      proposed_slug: "updated-task-guide",
      candidate_hash: "c".repeat(64),
      status: "changes_requested",
      producer_type: "worker",
      producer_id: "worker-01",
      task_id: "30000000-0000-0000-0000-000000000002",
      subtask_id: "31000000-0000-0000-0000-000000000002",
      worker_item_id: null,
      execution_id: null,
      supersedes_candidate_id: null,
      version: 2,
      created_at: "2026-08-25T01:02:03Z",
      updated_at: "2026-08-25T02:02:03Z",
    },
    {
      candidate_id: "50000000-0000-0000-0000-000000000003",
      workspace_id: WORKSPACE_ID,
      operation: "revise_page",
      target_page_id: "34000000-0000-0000-0000-000000000002",
      expected_revision_id: "35000000-0000-0000-0000-000000000002",
      expected_revision_hash: "d".repeat(64),
      proposed_title: "Stale worker notes",
      proposed_slug: "stale-worker-notes",
      candidate_hash: "e".repeat(64),
      status: "stale",
      producer_type: "worker",
      producer_id: "worker-02",
      task_id: "30000000-0000-0000-0000-000000000003",
      subtask_id: "31000000-0000-0000-0000-000000000003",
      worker_item_id: null,
      execution_id: null,
      supersedes_candidate_id: null,
      version: 1,
      created_at: "2026-08-24T01:02:03Z",
      updated_at: "2026-08-24T02:02:03Z",
    },
    {
      candidate_id: "50000000-0000-0000-0000-000000000004",
      workspace_id: WORKSPACE_ID,
      operation: "create_page",
      target_page_id: null,
      expected_revision_id: null,
      expected_revision_hash: null,
      proposed_title: "Already accepted",
      proposed_slug: "already-accepted",
      candidate_hash: "f".repeat(64),
      status: "accepted",
      producer_type: "main_agent",
      producer_id: "aitejiu",
      task_id: "30000000-0000-0000-0000-000000000004",
      subtask_id: null,
      worker_item_id: null,
      execution_id: null,
      supersedes_candidate_id: null,
      version: 1,
      created_at: "2026-08-23T01:02:03Z",
      updated_at: "2026-08-23T02:02:03Z",
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
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/knowledge/inbox`]}>
        <Routes>
          <Route path="/workspaces/:workspaceId/knowledge/inbox" element={<KnowledgeInboxPage />} />
          <Route
            path="/workspaces/:workspaceId/knowledge/candidates/:candidateId"
            element={<div data-testid="candidate-route">候选详情已打开</div>}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...rendered, queryClient };
}

describe("KnowledgeInboxPage", () => {
  beforeEach(() => {
    server.resetHandlers();
    server.use(
      http.get(
        `/knowledge-api/api/v1/workspaces/${WORKSPACE_ID}/knowledge/candidates`,
        () => HttpResponse.json(candidates),
      ),
    );
  });

  it("prioritizes candidate and task context in the default review queue", async () => {
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByRole("heading", { name: "知识收件箱" })).toBeVisible();
    expect(await screen.findByText("New runtime guide")).toBeVisible();
    expect(screen.getByText("待审核 1")).toBeVisible();
    expect(screen.getByText("新页面")).toBeVisible();
    expect(screen.getAllByText("修改页面")).toHaveLength(2);
    expect(within(screen.getByRole("link", { name: "打开候选 New runtime guide" })).getByText("任务 30000000 / 子任务 31000000")).toBeVisible();
    expect(screen.getByText("worker-01")).toBeVisible();
    expect(screen.queryByRole("button", { name: /接受|通过/ })).not.toBeInTheDocument();

    await user.click(screen.getByRole("link", { name: "打开候选 New runtime guide" }));
    expect(await screen.findByTestId("candidate-route")).toBeVisible();
  });

  it("supports status and source-task filters without mixing in accepted candidates", async () => {
    const user = userEvent.setup();
    renderPage();

    await screen.findByText("New runtime guide");
    await user.selectOptions(screen.getByLabelText("候选状态筛选"), "all");
    expect(screen.getByText("Already accepted")).toBeVisible();

    await user.type(screen.getByLabelText("来源任务筛选"), "30000000-0000-0000-0000-000000000002");
    expect(screen.getByText("Updated task guide")).toBeVisible();
    expect(screen.queryByText("New runtime guide")).not.toBeInTheDocument();
  });

  it("shows a retryable service error", async () => {
    server.use(
      http.get(
        `/knowledge-api/api/v1/workspaces/${WORKSPACE_ID}/knowledge/candidates`,
        () => new HttpResponse(null, { status: 503 }),
      ),
    );
    renderPage();

    expect(await screen.findByRole("button", { name: "重试读取候选" })).toBeVisible();
    expect(screen.getByText("无法读取知识候选")).toBeVisible();
  });

  it("uses low-frequency foreground refresh for the review inbox", async () => {
    const { queryClient } = renderPage();

    await screen.findByText("New runtime guide");

    const query = queryClient.getQueryCache().find({
      queryKey: knowledgeCandidatesQueryKey(WORKSPACE_ID, { limit: "100" }),
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
