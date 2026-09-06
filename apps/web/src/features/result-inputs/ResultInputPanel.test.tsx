import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { workerResultInputsFixture } from "@/test/resultInputFixtures";

import { ResultInputPanel } from "./ResultInputPanel";
import type { WorkerResultInputsView } from "./api";

function renderPanel(view: WorkerResultInputsView = workerResultInputsFixture()) {
  const onOpenWorkerItem = vi.fn();
  const onOpenArtifact = vi.fn();
  const onAction = vi.fn();
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <ResultInputPanel
          view={view}
          onOpenWorkerItem={onOpenWorkerItem}
          onOpenArtifact={onOpenArtifact}
          onAction={onAction}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { onOpenWorkerItem, onOpenArtifact, onAction };
}

describe("ResultInputPanel", () => {
  it("keeps delivery distinct from release", () => {
    const view = workerResultInputsFixture();
    view.delivery.reason_code = "result_input_unavailable";
    renderPanel(view);

    const panel = screen.getByRole("region", { name: "结果输入" });
    expect(panel).toHaveTextContent("已采用 2 份结果");
    expect(panel).toHaveTextContent("内容暂不可用");
    expect(panel).not.toHaveTextContent("交付完成");
    expect(panel).toHaveTextContent("等待用户确认输入来源");
  });

  it("shows ordered candidates, fixed artifacts, evidence, and hard dependencies", async () => {
    const user = userEvent.setup();
    const view = workerResultInputsFixture();
    const { onOpenWorkerItem, onOpenArtifact } = renderPanel(view);
    const panel = screen.getByRole("region", { name: "结果输入" });

    expect(within(panel).getByText("Research reports")).toBeVisible();
    expect(within(panel).getByText("至少 2 / 3")).toBeVisible();
    expect(within(panel).getByText("Research C")).toBeVisible();
    expect(within(panel).getByText("缺少 Artifact：artifact_type:report")).toBeVisible();
    expect(within(panel).getByText("本次采用")).toBeVisible();
    expect(within(panel).getAllByText(/固定版本 · attempt 1/)).toHaveLength(2);
    expect(within(panel).getAllByText("acceptance:research-complete")).toHaveLength(2);
    expect(within(panel).getByText("独立硬依赖")).toBeVisible();
    expect(within(panel).getAllByText("Research A")).toHaveLength(3);

    await user.click(within(panel).getByRole("button", { name: "查看来源 WorkerItem：Research A" }));
    expect(onOpenWorkerItem).toHaveBeenCalledWith("61000000-0000-0000-0000-000000000011");
    await user.click(within(panel).getAllByRole("button", { name: "查看固定 Artifact：report" })[0]!);
    expect(onOpenArtifact).toHaveBeenCalledWith("61000000-0000-0000-0000-000000000015");
  });

  it("passes the exact assembly action descriptor and marks unknown actions unsupported", async () => {
    const user = userEvent.setup();
    const view = workerResultInputsFixture();
    view.delivery.assembly_request_id = "61000000-0000-0000-0000-000000000025";
    view.delivery.assembly_status = "retry_wait";
    view.delivery.assembly_version = 4;
    view.delivery.assembly_request_fingerprint = "d".repeat(64);
    view.available_actions = [{
      action: "future_action",
      command_type: "FutureCommand",
      target_type: "future_target",
      target_id: "61000000-0000-0000-0000-000000000026",
      expected_version: 9,
      expected_fingerprint: null,
      expected_plan_generation: 4,
      expected_input_selection_id: null,
      expected_input_selection_hash: null,
    }, ...view.available_actions];
    const { onAction } = renderPanel(view);
    const panel = screen.getByRole("region", { name: "结果输入" });

    expect(within(panel).getByText("暂不支持此操作：future_action")).toBeVisible();
    await user.click(within(panel).getByRole("button", { name: "重试上下文组装" }));
    expect(onAction).toHaveBeenCalledWith(expect.objectContaining({
      action: "retry_context_assembly",
      command_type: "RetryContextAssembly",
      target_type: "context_assembly",
      target_id: "61000000-0000-0000-0000-000000000025",
      expected_version: 4,
      expected_fingerprint: "d".repeat(64),
    }));
  });

  it("keeps an empty historical request readable without actions", () => {
    const view = workerResultInputsFixture();
    view.is_historical = true;
    view.request = null;
    view.groups = [];
    view.hard_dependencies = [];
    view.available_actions = [];
    const { onAction } = renderPanel(view);
    const panel = screen.getByRole("region", { name: "结果输入" });

    expect(within(panel).getByText("历史执行")).toBeVisible();
    expect(within(panel).getByText("没有声明结果输入组或独立硬依赖")).toBeVisible();
    expect(within(panel).queryByRole("button", { name: "重试上下文组装" })).not.toBeInTheDocument();
    expect(onAction).not.toHaveBeenCalled();
  });
});
