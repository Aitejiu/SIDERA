import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

import type { HandoffTraceDescriptor, NativeHandoffTransition } from "./types";
import { NativeHandoffProgress } from "./NativeHandoffProgress";

const workspaceId = "20000000-0000-0000-0000-000000000001";
const rootTaskId = "30000000-0000-0000-0000-000000000001";
const executionId = "60000000-0000-0000-0000-000000000001";
const sessionId = "70000000-0000-0000-0000-000000000001";
const trace: HandoffTraceDescriptor = {
  root_task_id: rootTaskId,
  execution_id: executionId,
  session_id: sessionId,
  route: "native",
  raw_ref: null,
};
const transition = {
  id: "b0000000-0000-0000-0000-000000000001",
  handoff_schema_version: 2,
  workspace_id: workspaceId,
  worker_item_id: "40000000-0000-0000-0000-000000000001",
  proposal_id: "a0000000-0000-0000-0000-000000000001",
  version: 1,
  status: "pending",
  phase: "stop_source",
  writer_state: "source_stop_requested",
  confirmed_at: "2026-10-07T00:02:00Z",
  started_at: null,
  completed_at: null,
  failure_code: null,
  blocked_operation_id: null,
  logical_attempt: 1,
  next_attempt_at: null,
  source_execution_id: executionId,
  source_session_id: sessionId,
  source_run_id: "90000000-0000-0000-0000-000000000001",
  source_binding_version: 2,
  source_profile_ref: { workspace_id: workspaceId, profile_id: "source-profile", revision: 1 },
  source_input_cutoff_sequence: 5,
  target_assignee: { kind: "human", id: "principal-current" },
  target_profile_ref: null,
  target_worker_id: null,
  target_execution_id: null,
  target_session_id: null,
  target_run_id: null,
  target_binding_version: null,
  target_context_version: 4,
  context: {
    source_package_id: "package-source",
    source_package_hash: "a".repeat(64),
    source_context_version: 3,
    target_package_id: null,
    target_package_hash: null,
    target_context_version: 4,
  },
  plan_base: { snapshot_id: "10000000-0000-0000-0000-000000000001", generation: 2, head_version: 6 },
  available_recovery_strategies: [],
} as NativeHandoffTransition;

describe("NativeHandoffProgress", () => {
  it("shows source-stop state and keeps an unadmitted human target distinct from failure", async () => {
    const onRefresh = vi.fn(async () => undefined);
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <NativeHandoffProgress
          workspaceId={workspaceId}
          transition={transition}
          traceDescriptor={trace}
          onRefresh={onRefresh}
        />
      </MemoryRouter>,
    );

    expect(screen.getByRole("region", { name: "原生交接进度" })).toBeInTheDocument();
    expect(screen.getByText("等待源 Agent 停止")).toBeInTheDocument();
    expect(screen.getByText("source_stop_requested")).toBeInTheDocument();
    expect(screen.getAllByText("当前用户").length).toBeGreaterThan(0);
    expect(screen.getByText(/目标 Runtime 尚未接入/)).toBeInTheDocument();
    expect(screen.getByText("source-profile · R1")).toBeInTheDocument();
    expect(screen.queryByText("目标 Agent 启动结果未知")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /恢复|Recover/ })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "查看源 Trace" })).toHaveAttribute(
      "href",
      `/workspaces/${workspaceId}/tasks/${rootTaskId}/executions/${executionId}/sessions/${sessionId}/trace`,
    );
    expect(screen.queryByRole("link", { name: "查看目标 Trace" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "重新读取原生进度" }));

    expect(onRefresh).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("waits for source-stop evidence from the public deferred-stop fields", () => {
    const deferredStop: NativeHandoffTransition = {
      ...transition,
      status: "running",
      phase: "stop_source",
      writer_state: "source_stop_requested",
      started_at: "2026-10-07T00:02:30Z",
      next_attempt_at: "2026-10-07T00:05:00Z",
      failure_code: "source_stop_unconfirmed",
      blocked_operation_id: null,
      target_worker_id: null,
      target_execution_id: null,
      target_session_id: null,
      target_run_id: null,
      available_recovery_strategies: [],
    };
    render(
      <MemoryRouter>
        <NativeHandoffProgress
          workspaceId={workspaceId}
          transition={deferredStop}
          onRefresh={async () => undefined}
        />
      </MemoryRouter>,
    );

    const progress = screen.getByRole("region", { name: "原生交接进度" });
    expect(progress).toHaveTextContent("等待源 Agent 停止证据");
    expect(progress).toHaveTextContent("source_stop_requested");
    expect(screen.getByText("冻结原因：source_stop_unconfirmed")).toBeInTheDocument();
    expect(progress).toHaveTextContent("目标 Runtime 尚未接入");
    expect(screen.queryByText("目标 Agent 启动结果未知")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /恢复|Recover/ })).not.toBeInTheDocument();
  });

  it("does not attach a source Trace descriptor when its execution pin differs", () => {
    render(
      <MemoryRouter>
        <NativeHandoffProgress
          workspaceId={workspaceId}
          transition={transition}
          traceDescriptor={{ ...trace, execution_id: "different-execution" }}
          onRefresh={async () => undefined}
        />
      </MemoryRouter>,
    );

    expect(screen.queryByRole("link", { name: "查看源 Trace" })).not.toBeInTheDocument();
    expect(screen.getByText("查看源 Trace · 暂无")).toBeInTheDocument();
  });
});
