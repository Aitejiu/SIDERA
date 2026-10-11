import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { NativeWorkerResultActionDraft, WorkerResultOutcome } from "./nativeWorkerResultActions";
import { NativeWorkerResultActionRecovery, type NativeWorkerResultActionRecoveryProps } from "./NativeWorkerResultActionRecovery";

function recoveryProps(): NativeWorkerResultActionRecoveryProps {
  const scope = {
    workspace_id: "20000000-0000-0000-0000-000000000001", root_task_id: "30000000-0000-0000-0000-000000000001",
    worker_item_id: "40000000-0000-0000-0000-000000000001", principal_id: "10000000-0000-0000-0000-000000000001",
  };
  const state: NativeWorkerResultActionDraft = {
    schema_version: 1, scope, status: "unknown", aggregate_id: null, aggregate_version: null, outcome: null, rejection_code: null,
    review: {
      action: {
        action: "pause", command_type: "PauseCollaborationRelease", target_type: "worker_item", target_id: scope.worker_item_id,
        expected_version: 9, expected_plan_generation: 2, expected_fingerprint: null,
        expected_input_selection_id: null, expected_input_selection_hash: null,
      }, reason: "Review first",
    },
    original: {
      command_id: "c0000000-0000-0000-0000-000000000001", workspace_id: scope.workspace_id,
      command_type: "PauseCollaborationRelease", actor: { type: "user", id: scope.principal_id },
      target_type: "worker_item", target_id: scope.worker_item_id, expected_version: 9,
      idempotency_key: "original-pause", correlation_id: "c0000000-0000-0000-0000-000000000002",
      payload: { transport: "web", reason: "Review first", target_plan_generation: 2 },
    },
  };
  return {
    workspaceId: scope.workspace_id,
    actions: {
      authority: { kind: "unavailable", reason: "session_unavailable" }, authorized: false, state, busy: false, error: null, storageError: null,
      lookupOriginal: vi.fn(async () => undefined), retryStorage: vi.fn(), onSessionAuthorized: vi.fn(),
    },
  };
}

describe("native WorkerItem original recovery", () => {
  it("exposes local authorization for an expired idle Product session", () => {
    const props = recoveryProps(); const scope = props.actions.state!.scope;
    props.actions.state = { ...props.actions.state!, status: "idle", original: null, review: null };
    props.actions.authority = { kind: "product", session: {
      session_id: "d0000000-0000-0000-0000-000000000001", workspace_id: scope.workspace_id,
      principal_id: scope.principal_id, expires_at: "2000-01-01T00:00:00Z",
    } };
    const view = render(<NativeWorkerResultActionRecovery {...props} />);
    expect(screen.getByRole("button", { name: "本地会话" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "查询原操作" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重试本地记录" })).not.toBeInTheDocument();
    props.actions.authorized = true;
    view.rerender(<NativeWorkerResultActionRecovery {...props} />);
    expect(screen.queryByRole("region", { name: "结果操作记录" })).not.toBeInTheDocument();
  });

  it("keeps the original query and current-human entry available without current action panels", async () => {
    const props = recoveryProps(); render(<NativeWorkerResultActionRecovery {...props} />);
    expect(screen.getByText("原操作结果未知")).toBeVisible();
    expect(screen.getByRole("button", { name: "本地会话" })).toBeVisible();
    await userEvent.setup().click(screen.getByRole("button", { name: "查询原操作" }));
    expect(props.actions.lookupOriginal).toHaveBeenCalledOnce();
    expect(screen.queryByRole("button", { name: /再次提交|重试组装/ })).not.toBeInTheDocument();
  });

  it.each([
    ["paused", "已暂停自动放行"], ["approved", "Proposal 已批准"], ["stale", "Proposal 已失效"],
    ["rejected", "Proposal 已驳回"], ["automatic", "已恢复自动放行"], ["queued", "上下文组装重试已排队"],
  ] satisfies Array<[WorkerResultOutcome, string]>)("distinguishes handled %s from admission or completion", (outcome, label) => {
    const props = recoveryProps();
    if (props.actions.state) { props.actions.state.status = "applied"; props.actions.state.outcome = outcome; }
    render(<NativeWorkerResultActionRecovery {...props} />);
    expect(screen.getByText(label)).toBeVisible(); expect(screen.getByText("原操作已处理")).toBeVisible();
    expect(screen.queryByText(/任务已完成|已开始执行|消费者已准入/)).not.toBeInTheDocument();
  });

  it("offers explicit storage retry while preserving the original query", async () => {
    const props = recoveryProps(); props.actions.storageError = "storage_unavailable";
    render(<NativeWorkerResultActionRecovery {...props} />);
    await userEvent.setup().click(screen.getByRole("button", { name: "重试本地记录" }));
    expect(props.actions.retryStorage).toHaveBeenCalledOnce(); expect(screen.getByRole("button", { name: "查询原操作" })).toBeEnabled();
  });

  it("does not relabel an explicit journal rejection as a business decision", () => {
    const props = recoveryProps(); if (props.actions.state) props.actions.state.status = "rejected";
    props.actions.busy = true; render(<NativeWorkerResultActionRecovery {...props} />);
    expect(screen.getByText("原操作被拒绝")).toBeVisible(); expect(screen.queryByText("Proposal 已驳回")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "查询原操作" })).toBeDisabled();
  });
});
