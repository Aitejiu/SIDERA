import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { server } from "@/test/server";

import { TaskResultPanel } from "./TaskResultPanel";
import { taskResultsQueryKey } from "./api";
import {
  RESULT_CANDIDATE_ID,
  RESULT_OPERATION_ID,
  RESULT_ROOT_TASK_ID,
  RESULT_WORKSPACE_ID,
  resultAction,
  resultView,
} from "./testFixtures";

function renderPanel(initialEntry = `/workspaces/${RESULT_WORKSPACE_ID}/tasks/${RESULT_ROOT_TASK_ID}?view=results`) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const rendered = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route
            path="/workspaces/:workspaceId/tasks/:rootTaskId"
            element={<TaskResultPanel workspaceId={RESULT_WORKSPACE_ID} rootTaskId={RESULT_ROOT_TASK_ID} />}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { queryClient, ...rendered };
}

describe("TaskResultPanel", () => {
  beforeEach(() => {
    server.resetHandlers();
    server.use(
      http.get("/main-agent/v1/health", () => HttpResponse.json({ status: "ok", service: "sidera-main-agent-gateway" })),
      http.get(
        `/api/v1/workspaces/${RESULT_WORKSPACE_ID}/tasks/${RESULT_ROOT_TASK_ID}/results`,
        () => HttpResponse.json(resultView()),
      ),
      http.post(
        `/api/v1/workspaces/${RESULT_WORKSPACE_ID}/tasks/${RESULT_ROOT_TASK_ID}/results/${RESULT_CANDIDATE_ID}/acceptance-basis`,
        () => HttpResponse.json({
          candidate_id: RESULT_CANDIDATE_ID,
          candidate_hash: "c".repeat(64),
          expected_result_version: 7,
          acceptance_basis_hash: "f".repeat(64),
        }),
      ),
      http.post("/api/v1/commands", () => HttpResponse.json({
        aggregate_id: RESULT_ROOT_TASK_ID,
        aggregate_version: 12,
        command_id: "71000000-0000-0000-0000-000000000012",
        event_ids: [],
      })),
    );
  });

  it("keeps a passed check separate from root task completion", async () => {
    renderPanel();

    const panel = (await screen.findByText("等待用户验收")).closest("section")!;
    expect(within(panel).getByText("等待用户验收")).toBeVisible();
    expect(within(panel).getAllByText("Expired credentials are rejected")).toHaveLength(2);
    expect(within(panel).getByText("默认不自动应用")).toBeVisible();
    expect(within(panel).getByRole("button", { name: "接受成果" })).toBeDisabled();
    expect(within(panel).queryByText("任务已完成")).not.toBeInTheDocument();
  });

  it("refreshes while a result operation is still in progress", async () => {
    let requests = 0;
    server.use(
      http.get(
        `/api/v1/workspaces/${RESULT_WORKSPACE_ID}/tasks/${RESULT_ROOT_TASK_ID}/results`,
        () => {
          requests += 1;
          return HttpResponse.json(requests === 1
            ? resultView({
              result_stage: "verifying",
              active_operation: {
                operation_id: RESULT_OPERATION_ID,
                operation_kind: "verification",
                status: "running",
                attempt: 1,
                failure_code: null,
                receipt_ref: null,
                version: 1,
                candidate_id: RESULT_CANDIDATE_ID,
              },
              available_actions: [],
            })
            : resultView({
              result_stage: "completed",
              task_status: "succeeded",
              available_actions: [],
            }));
        },
      ),
    );

    renderPanel();

    const panel = await screen.findByRole("region", { name: "成果验收" });
    expect(await within(panel).findByText("检查中")).toBeVisible();
    expect(await within(panel).findByText("已完成", {}, { timeout: 5_000 })).toBeVisible();
    expect(requests).toBeGreaterThan(1);
  });

  it("requires a decision for every criterion before accepting", async () => {
    const user = userEvent.setup();
    renderPanel();

    const panel = (await screen.findByText("等待用户验收")).closest("section")!;
    await user.selectOptions(
      within(panel).getByLabelText("Expired credentials are rejected 的决定"),
      "accept_passed",
    );
    expect(within(panel).getByRole("button", { name: "接受成果" })).toBeEnabled();
  });

  it("requires a reason for a failed-check exception", async () => {
    const user = userEvent.setup();
    server.use(
      http.get(
        `/api/v1/workspaces/${RESULT_WORKSPACE_ID}/tasks/${RESULT_ROOT_TASK_ID}/results`,
        () => HttpResponse.json(resultView({
          criteria: [{
            ...resultView().criteria[0]!,
            observed_status: "failed",
            verification_status: "failed",
          }],
          blocking_reasons: ["unit_failed"],
        })),
      ),
    );
    renderPanel();

    const panel = (await screen.findByText("等待用户验收")).closest("section")!;
    await user.selectOptions(
      within(panel).getByLabelText("Expired credentials are rejected 的决定"),
      "exception_accepted",
    );
    expect(within(panel).getByRole("button", { name: "接受成果" })).toBeDisabled();
    await user.type(within(panel).getByLabelText("例外原因"), "仅供本地演示");
    expect(within(panel).getByRole("button", { name: "接受成果" })).toBeEnabled();
  });

  it("keeps the decision draft after a stale acceptance response", async () => {
    const user = userEvent.setup();
    server.use(
      http.post("/api/v1/commands", () => HttpResponse.json(
        { error: { code: "acceptance_stale", message: "成果已变化，请重新审阅" } },
        { status: 409 },
      )),
    );
    renderPanel();

    const panel = (await screen.findByText("等待用户验收")).closest("section")!;
    const decision = within(panel).getByLabelText("Expired credentials are rejected 的决定");
    await user.selectOptions(decision, "accept_passed");
    await user.click(within(panel).getByRole("button", { name: "接受成果" }));

    expect(await within(panel).findByText("成果已变化，请重新审阅")).toBeVisible();
    expect(decision).toHaveValue("accept_passed");
  });

  it("keeps an exception reason when the result snapshot advances", async () => {
    const user = userEvent.setup();
    let requests = 0;
    const failedCriteria = [{
      ...resultView().criteria[0]!,
      observed_status: "failed" as const,
      verification_status: "failed" as const,
    }];
    server.use(
      http.get(
        `/api/v1/workspaces/${RESULT_WORKSPACE_ID}/tasks/${RESULT_ROOT_TASK_ID}/results`,
        () => {
          requests += 1;
          return HttpResponse.json(requests === 1
            ? resultView({ criteria: failedCriteria, blocking_reasons: ["unit_failed"] })
            : resultView({
              result_version: 8,
              criteria: failedCriteria,
              blocking_reasons: ["unit_failed"],
              available_actions: [{ ...resultAction(), expected_result_version: 8 }],
            }));
        },
      ),
    );
    const { queryClient } = renderPanel();

    const panel = (await screen.findByText("等待用户验收")).closest("section")!;
    await user.selectOptions(
      within(panel).getByLabelText("Expired credentials are rejected 的决定"),
      "exception_accepted",
    );
    const reason = "候选变化后的人工依据";
    await user.type(within(panel).getByLabelText("例外原因"), reason);

    await queryClient.invalidateQueries({ queryKey: taskResultsQueryKey(RESULT_WORKSPACE_ID, RESULT_ROOT_TASK_ID) });
    expect(await within(panel).findByText("RESULT V8")).toBeVisible();
    expect(within(panel).getByLabelText("例外原因")).toHaveValue(reason);
  });

  it("opens an older candidate as a read-only history entry", async () => {
    const oldCandidateId = "71000000-0000-0000-0000-000000000014";
    const current = resultView();
    const oldCandidate = {
      ...current.candidate_history.items[0]!,
      candidate_id: oldCandidateId,
      candidate_hash: "1".repeat(64),
      is_current: false,
    };
    server.use(
      http.get(
        `/api/v1/workspaces/${RESULT_WORKSPACE_ID}/tasks/${RESULT_ROOT_TASK_ID}/results`,
        () => HttpResponse.json(resultView({
          candidate_history: {
            items: [current.candidate_history.items[0]!, oldCandidate],
            next_cursor: null,
          },
        })),
      ),
    );
    renderPanel(`/workspaces/${RESULT_WORKSPACE_ID}/tasks/${RESULT_ROOT_TASK_ID}?view=results&candidate=${oldCandidateId}`);

    const panel = (await screen.findByText("历史候选，只读")).closest("section")!;
    expect(within(panel).getByText("历史候选，只读")).toBeVisible();
    expect(within(panel).getAllByText("111111111111")).toHaveLength(2);
    expect(within(panel).queryByRole("button", { name: "接受成果" })).not.toBeInTheDocument();
    expect(RESULT_CANDIDATE_ID).not.toBe(oldCandidateId);
  });
});
