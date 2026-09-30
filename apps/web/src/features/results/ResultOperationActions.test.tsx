import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { server } from "@/test/server";

import { ResultOperationActions } from "./ResultOperationActions";
import {
  RESULT_CANDIDATE_ID,
  RESULT_ROOT_TASK_ID,
  RESULT_WORKSPACE_ID,
  resultAction,
  resultView,
  resultViewScenario,
} from "./testFixtures";

const WORKER_ITEM_ID = "71000000-0000-0000-0000-000000000021";

function renderActions(
  view: Parameters<typeof ResultOperationActions>[0]["view"],
  overrides: Partial<Parameters<typeof ResultOperationActions>[0]> = {},
) {
  return render(
    <ResultOperationActions
      workspaceId={RESULT_WORKSPACE_ID}
      rootTaskId={RESULT_ROOT_TASK_ID}
      workerItemId={WORKER_ITEM_ID}
      view={view}
      isLoading={false}
      isError={false}
      canIntervene
      onRetry={() => undefined}
      onRefresh={async () => undefined}
      onOpenResults={() => undefined}
      {...overrides}
    />,
  );
}

describe("ResultOperationActions", () => {
  beforeEach(() => {
    server.resetHandlers();
  });

  it("does not offer blind retry for an unknown application", async () => {
    const onOpenResults = vi.fn();
    renderActions(resultViewScenario("unknown-apply"), { onOpenResults });

    const button = screen.getByRole("button", { name: "核对应用状态" });
    expect(button).toBeEnabled();
    expect(screen.queryByRole("button", { name: "重新应用" })).not.toBeInTheDocument();
    await userEvent.setup().click(button);
    expect(onOpenResults).toHaveBeenCalledWith(RESULT_CANDIDATE_ID);
  });

  it("submits rework for the current WorkerItem only", async () => {
    const user = userEvent.setup();
    const requests: Record<string, unknown>[] = [];
    server.use(
      http.post("/api/v1/commands", async ({ request }) => {
        requests.push(await request.json() as Record<string, unknown>);
        return HttpResponse.json({
          aggregate_id: RESULT_ROOT_TASK_ID,
          aggregate_version: 8,
          command_id: "71000000-0000-0000-0000-000000000023",
          event_ids: [],
        });
      }),
    );
    const action = {
      ...resultAction("RequestTaskRework"),
      allowed_worker_item_ids: [WORKER_ITEM_ID],
    };
    renderActions(resultView({ available_actions: [action] }));

    await user.click(screen.getByRole("button", { name: "局部返工" }));
    const form = screen.getByRole("form", { name: "局部返工" });
    expect(within(form).getByText(/当前 WorkerItem/)).toBeVisible();
    expect(within(form).queryByRole("combobox", { name: "返工 WorkerItem" })).not.toBeInTheDocument();
    await user.click(within(form).getByLabelText("Expired credentials are rejected"));
    await user.type(within(form).getByLabelText("返工说明"), "修复当前 WorkerItem 的失败实现");
    await user.click(within(form).getByRole("button", { name: "提交局部返工" }));

    await waitFor(() => expect(requests).toHaveLength(1));
    expect(requests[0]).toMatchObject({
      command_type: "RequestTaskRework",
      target_type: "task",
      target_id: RESULT_ROOT_TASK_ID,
      payload: {
        candidate_id: RESULT_CANDIDATE_ID,
        mode: "user",
        target_worker_item_ids: [WORKER_ITEM_ID],
        impact_worker_item_ids: [WORKER_ITEM_ID],
        reason: "修复当前 WorkerItem 的失败实现",
      },
    });
  });

  it("routes an unavailable WorkerItem scope to Revision", async () => {
    renderActions(resultView({
      available_actions: [{
        ...resultAction("RequestTaskRework"),
        allowed_worker_item_ids: ["71000000-0000-0000-0000-000000000099"],
      }],
    }));

    expect(screen.getByText("当前 WorkerItem 不在可返工范围，需要主 Agent / Revision 调整"))
      .toBeVisible();
    expect(screen.queryByRole("button", { name: "局部返工" })).not.toBeInTheDocument();
  });

  it("keeps result operations available when the current verification adapter cannot accept input", () => {
    renderActions(resultView({
      available_actions: [{
        ...resultAction("RequestTaskRework"),
        allowed_worker_item_ids: [WORKER_ITEM_ID],
      }],
    }), {
      canIntervene: false,
      canOperateResult: true,
    });

    expect(screen.getByRole("button", { name: "局部返工" })).toBeVisible();
    expect(screen.queryByText("当前 WorkerItem 只读，局部修改需主 Agent / Revision")).not.toBeInTheDocument();
  });

  it("submits additional budget as an increment and displays cumulative usage", async () => {
    const user = userEvent.setup();
    const requests: Record<string, unknown>[] = [];
    server.use(
      http.post("/api/v1/commands", async ({ request }) => {
        requests.push(await request.json() as Record<string, unknown>);
        return HttpResponse.json({
          aggregate_id: RESULT_ROOT_TASK_ID,
          aggregate_version: 9,
          command_id: "71000000-0000-0000-0000-000000000024",
          event_ids: [],
        });
      }),
    );
    const action = {
      ...resultAction(),
      action: "authorize_additional_rework",
      command_type: "AuthorizeAdditionalRework",
      requires_reason: true,
    };
    renderActions(resultView({
      available_actions: [action],
      rework_budget: {
        auto_rounds_used: 2,
        auto_rounds_limit: 3,
        auto_seconds_limit: 900,
        auto_started_at: "2026-09-08T01:00:00Z",
        auto_deadline: "2026-09-08T01:15:00Z",
      },
    }));

    expect(screen.getByText("自动返工额度")).toBeVisible();
    expect(screen.getByText("2 / 3 轮")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "增加返工额度" }));
    await user.clear(screen.getByLabelText("追加轮数"));
    await user.type(screen.getByLabelText("追加轮数"), "1");
    await user.clear(screen.getByLabelText("追加秒数"));
    await user.type(screen.getByLabelText("追加秒数"), "300");
    await user.type(screen.getByLabelText("额度调整原因"), "用户确认继续返工");
    await user.click(screen.getByRole("button", { name: "提交额度调整" }));

    await waitFor(() => expect(requests).toHaveLength(1));
    expect(requests[0]).toMatchObject({
      command_type: "AuthorizeAdditionalRework",
      payload: {
        additional_rounds: 1,
        additional_seconds: 300,
        reason: "用户确认继续返工",
      },
    });
  });
});
