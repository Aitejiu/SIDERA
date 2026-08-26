import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { server } from "@/test/server";

import { KnowledgeCandidatePage } from "./KnowledgeCandidatePage";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const CANDIDATE_ID = "50000000-0000-0000-0000-000000000001";
const TARGET_PAGE_ID = "30000000-0000-0000-0000-000000000001";
const TARGET_REVISION_ID = "40000000-0000-0000-0000-000000000003";
const COMMAND_ID = "60000000-0000-0000-0000-000000000001";

const candidate = {
  summary: {
    candidate_id: CANDIDATE_ID,
    workspace_id: WORKSPACE_ID,
    operation: "revise_page",
    target_page_id: TARGET_PAGE_ID,
    expected_revision_id: TARGET_REVISION_ID,
    expected_revision_hash: "a".repeat(64),
    proposed_title: "Updated runtime guide",
    proposed_slug: "updated-runtime-guide",
    candidate_hash: "b".repeat(64),
    status: "pending",
    producer_type: "worker",
    producer_id: "worker-01",
    task_id: "31000000-0000-0000-0000-000000000001",
    subtask_id: "32000000-0000-0000-0000-000000000001",
    worker_item_id: "33000000-0000-0000-0000-000000000001",
    execution_id: "34000000-0000-0000-0000-000000000001",
    supersedes_candidate_id: null,
    version: 2,
    created_at: "2026-08-26T01:02:03Z",
    updated_at: "2026-08-26T02:02:03Z",
  },
  proposed_snapshot: {
    title: "Updated runtime guide",
    slug: "updated-runtime-guide",
    summary: "A proposed runtime reference.",
    tags: ["runtime", "candidate"],
    sections: [
      {
        section_key: "overview",
        heading: "Overview",
        summary: "",
        body_markdown: "## Proposed runtime\n\nNew worker routing.",
        ordinal: 0,
      },
    ],
    relations: [],
    source_refs: [],
  },
  provenance: {
    operation: "revise_page",
    target_page_id: TARGET_PAGE_ID,
    expected_revision_id: TARGET_REVISION_ID,
    expected_revision_hash: "a".repeat(64),
    producer_type: "worker",
    producer_id: "worker-01",
    task_id: "31000000-0000-0000-0000-000000000001",
    subtask_id: "32000000-0000-0000-0000-000000000001",
    worker_item_id: "33000000-0000-0000-0000-000000000001",
    execution_id: "34000000-0000-0000-0000-000000000001",
  },
  sources: [],
  observed_target_revision: {
    revision_id: TARGET_REVISION_ID,
    page_id: TARGET_PAGE_ID,
    workspace_id: WORKSPACE_ID,
    revision_number: 3,
    title: "Runtime guide",
    summary: "The current runtime reference.",
    tags: ["runtime"],
    content_hash: "a".repeat(64),
    change_reason: "Initial import",
    created_by: "local_owner:aitejiu",
    created_at: "2026-08-25T01:02:03Z",
    accepted_candidate_id: null,
  },
  current_target_revision: {
    revision_id: TARGET_REVISION_ID,
    page_id: TARGET_PAGE_ID,
    workspace_id: WORKSPACE_ID,
    revision_number: 3,
    title: "Runtime guide",
    summary: "The current runtime reference.",
    tags: ["runtime"],
    content_hash: "a".repeat(64),
    change_reason: "Initial import",
    created_by: "local_owner:aitejiu",
    created_at: "2026-08-25T01:02:03Z",
    accepted_candidate_id: null,
  },
  target_is_stale: false,
  diff: {
    from_revision_id: TARGET_REVISION_ID,
    to_revision_id: CANDIDATE_ID,
    title_changed: true,
    summary_changed: true,
    tags_added: ["candidate"],
    tags_removed: [],
    sections_added: [],
    sections_removed: [],
    sections_changed: [{ section_key: "overview", before_hash: "a".repeat(64), after_hash: "c".repeat(64) }],
    relations_added: [],
    relations_removed: [],
    relations_changed: [],
    sources_added: [],
    sources_removed: [],
    body_unified_diff: "@@ -1 +1 @@\n-The current runtime reference.\n+New worker routing.",
    truncated: false,
  },
};

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/knowledge/candidates/${CANDIDATE_ID}`]}>
        <Routes>
          <Route path="/workspaces/:workspaceId/knowledge/candidates/:candidateId" element={<KnowledgeCandidatePage />} />
          <Route path="/workspaces/:workspaceId/knowledge/pages/:pageId" element={<div data-testid="accepted-page-route" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("KnowledgeCandidatePage", () => {
  beforeEach(() => {
    server.resetHandlers();
    server.use(
      http.get(
        `/knowledge-api/api/v1/workspaces/${WORKSPACE_ID}/knowledge/candidates/${CANDIDATE_ID}`,
        () => HttpResponse.json(candidate),
      ),
    );
  });

  it("puts task context and stale guard before the proposed snapshot", async () => {
    renderPage();

    expect(await screen.findByRole("heading", { name: "Updated runtime guide" })).toBeVisible();
    expect(screen.getByText("任务 31000000 / 子任务 32000000")).toBeVisible();
    expect(screen.getByText("待审核")).toBeVisible();
    expect(screen.getByText("修改页面")).toBeVisible();
    expect(screen.getByText("worker-01")).toBeVisible();
    expect(screen.getByRole("heading", { name: "提议内容" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "结构化 diff" })).toBeVisible();
    expect(screen.getByText(/当前目标版本 REV 3/)).toBeVisible();
    expect(screen.getByRole("button", { name: "接受候选" })).toBeVisible();
    expect(screen.getByRole("button", { name: "请求修改" })).toBeVisible();
    expect(screen.getByRole("button", { name: "拒绝候选" })).toBeVisible();
    expect(screen.getByRole("button", { name: "撤回候选" })).toBeVisible();
  });

  it("submits request changes with the observed candidate and target fence", async () => {
    const user = userEvent.setup();
    let requestBody: Record<string, unknown> | undefined;
    server.use(
      http.post("/knowledge-api/api/v1/commands", async ({ request }) => {
        requestBody = await request.json() as Record<string, unknown>;
        return HttpResponse.json({ command_id: COMMAND_ID, status: "applied", result: { status: "changes_requested" } });
      }),
    );
    renderPage();

    await screen.findByRole("heading", { name: "Updated runtime guide" });
    await user.click(screen.getByRole("button", { name: "请求修改" }));
    expect(screen.getByRole("alert")).toHaveTextContent("请先填写修改原因");
    await user.type(screen.getByLabelText("审核原因"), "补充 worker 路由说明");
    await user.click(screen.getByRole("button", { name: "提交请求修改" }));

    await waitFor(() => expect(requestBody?.command_type).toBe("ReviewKnowledgeCandidate"));
    const payload = requestBody?.payload as Record<string, unknown>;
    expect(payload.decision).toBe("request_changes");
    expect(payload.reason).toBe("补充 worker 路由说明");
    expect(payload.expected_candidate_version).toBe(2);
    expect(payload.observed_candidate_hash).toBe("b".repeat(64));
    expect(payload.observed_target_revision_id).toBe(TARGET_REVISION_ID);
    expect(payload.observed_target_revision_hash).toBe("a".repeat(64));
    expect(await screen.findByText("审核命令已提交")).toBeVisible();
  });

  it("confirms reject and opens the exact accepted revision after accept", async () => {
    const user = userEvent.setup();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    let decision: string | undefined;
    server.use(
      http.post("/knowledge-api/api/v1/commands", async ({ request }) => {
        const body = await request.json() as { payload?: { decision?: string } };
        decision = body.payload?.decision;
        return HttpResponse.json({
          command_id: COMMAND_ID,
          status: "applied",
          result: { status: "accepted", page_id: TARGET_PAGE_ID, revision_id: "40000000-0000-0000-0000-000000000004" },
        });
      }),
    );
    renderPage();
    await screen.findByRole("heading", { name: "Updated runtime guide" });
    await user.click(screen.getByRole("button", { name: "拒绝候选" }));
    expect(confirm).toHaveBeenCalled();
    expect(screen.getByLabelText("审核原因")).toBeVisible();
    await user.type(screen.getByLabelText("审核原因"), "内容不符合当前架构");
    await user.click(screen.getByRole("button", { name: "提交拒绝" }));
    await waitFor(() => expect(decision).toBe("reject"));
    expect(screen.getByText("审核命令已提交")).toBeVisible();
    confirm.mockRestore();

    // The accept path is separately fenced and should link to the exact created revision.
    server.use(
      http.post("/knowledge-api/api/v1/commands", () => HttpResponse.json({
        command_id: COMMAND_ID,
        status: "applied",
        result: { status: "accepted", page_id: TARGET_PAGE_ID, revision_id: "40000000-0000-0000-0000-000000000004" },
      })),
    );
    const acceptConfirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    await user.click(screen.getByRole("button", { name: "接受候选" }));
    await user.type(screen.getByLabelText("审核原因"), "确认内容");
    await user.click(screen.getByRole("button", { name: "提交接受" }));
    expect(await screen.findByTestId("accepted-page-route")).toBeVisible();
    acceptConfirm.mockRestore();
  });

  it("refreshes after a 409 without retrying the review command", async () => {
    const user = userEvent.setup();
    let getCalls = 0;
    let postCalls = 0;
    server.use(
      http.get(
        `/knowledge-api/api/v1/workspaces/${WORKSPACE_ID}/knowledge/candidates/${CANDIDATE_ID}`,
        () => {
          getCalls += 1;
          return HttpResponse.json(candidate);
        },
      ),
      http.post("/knowledge-api/api/v1/commands", () => {
        postCalls += 1;
        return HttpResponse.json(
          { error: { code: "expected_version_conflict", message: "stale", details: {} } },
          { status: 409 },
        );
      }),
    );
    renderPage();
    await screen.findByRole("heading", { name: "Updated runtime guide" });
    await user.click(screen.getByRole("button", { name: "接受候选" }));
    await user.type(screen.getByLabelText("审核原因"), "确认内容");
    await user.click(screen.getByRole("button", { name: "提交接受" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("候选或目标版本已变化");
    await waitFor(() => expect(getCalls).toBeGreaterThanOrEqual(2));
    expect(postCalls).toBe(1);
    expect(screen.getByText("未自动重试")).toBeVisible();
  });
});
