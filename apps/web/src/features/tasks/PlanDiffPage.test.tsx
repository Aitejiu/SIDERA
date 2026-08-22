import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { server } from "@/test/server";

import { PlanDiffPage } from "./PlanDiffPage";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const ROOT_TASK_ID = "30000000-0000-0000-0000-000000000001";

const diff = {
  schema_version: 1,
  last_change_cursor: 19,
  root_task_id: ROOT_TASK_ID,
  left_generation: 1,
  right_generation: 2,
  entries: [
    {
      work_item_key: "backend",
      change: "unchanged",
      changed_fields: [],
      reuse_candidate: true,
      context_patch_candidate: false,
      portable_replay_candidate: false,
      runtime_execution_id: "60000000-0000-0000-0000-000000000001",
    },
    {
      work_item_key: "frontend",
      change: "patched",
      changed_fields: ["goal_summary"],
      reuse_candidate: false,
      context_patch_candidate: true,
      portable_replay_candidate: false,
      runtime_execution_id: null,
    },
    {
      work_item_key: "docs",
      change: "added",
      changed_fields: [],
      reuse_candidate: false,
      context_patch_candidate: false,
      portable_replay_candidate: false,
      runtime_execution_id: null,
    },
    {
      work_item_key: "legacy",
      change: "removed",
      changed_fields: [],
      reuse_candidate: false,
      context_patch_candidate: false,
      portable_replay_candidate: true,
      runtime_execution_id: "60000000-0000-0000-0000-000000000002",
    },
  ],
};

describe("PlanDiffPage", () => {
  it("renders read-only diff semantics and portable replay advisory", async () => {
    server.use(
      http.get(`/api/v1/tasks/${ROOT_TASK_ID}/plan-diff`, () =>
        HttpResponse.json(diff)),
    );
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}/compare/1/2`]}>
          <Routes>
            <Route path="/workspaces/:workspaceId/tasks/:rootTaskId/compare/:left/:right" element={<PlanDiffPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByRole("heading", { name: "计划差异" })).toBeVisible();
    expect(await screen.findByText("可复用")).toBeVisible();
    expect(screen.getByText("Context Patch 建议")).toBeVisible();
    expect(screen.getByText("新增")).toBeVisible();
    expect(screen.getByText("后续不再调度")).toBeVisible();
    expect(screen.getAllByText("Portable Replay 候选").length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByRole("button", { name: /Apply|停止|重新分配/ })).not.toBeInTheDocument();
  });
});
