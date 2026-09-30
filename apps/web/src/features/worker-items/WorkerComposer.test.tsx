import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { server } from "@/test/server";

import { WorkerComposer } from "./WorkerComposer";
import { workerItemDraftKey } from "../trace/draftStore";
import type { WorkerActivity, WorkerWorkspace } from "./types";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const WORKER_ITEM_ID = "40000000-0000-0000-0000-000000000001";
const EXECUTION_ID = "60000000-0000-0000-0000-000000000002";
const SESSION_ID = "70000000-0000-0000-0000-000000000002";
const OLD_SESSION_ID = "70000000-0000-0000-0000-000000000001";
const WORKER_ID = "80000000-0000-0000-0000-000000000001";

const context = {
  worker_item_id: WORKER_ITEM_ID,
  plan_generation: 2,
  context_version: 4,
  execution_id: EXECUTION_ID,
  session_id: SESSION_ID,
  session_version: 4,
  worker_id: WORKER_ID,
};

const activeHandoff = {
  transition_id: "a0000000-0000-0000-0000-000000000001",
  proposal_id: "b0000000-0000-0000-0000-000000000001",
  status: "running",
  phase: "start_target_session",
  writer_state: "target_starting",
  source_execution_id: EXECUTION_ID,
  source_session_id: SESSION_ID,
  source_worker_id: WORKER_ID,
  target_worker_id: "80000000-0000-0000-0000-000000000002",
  target_execution_id: null,
  target_session_id: null,
  attention_required: false,
  failure_code: null,
  blocked_operation_id: null,
  available_recovery_strategies: [],
  source_trace_descriptor: null,
  target_trace_descriptor: null,
  started_at: "2026-08-23T08:05:00Z",
  updated_at: "2026-08-23T08:05:00Z",
};

const workspace = {
  workspace_id: WORKSPACE_ID,
  worker_item: {
    id: WORKER_ITEM_ID,
    stable_key: "callback-implementer",
    title: "实现回调幂等",
    responsibility: "实现幂等写入和重试策略",
    root_task_id: "30000000-0000-0000-0000-000000000001",
    status: "running",
    context_version: 4,
    acknowledged_context_version: 4,
    version: 7,
    updated_at: "2026-08-23T08:05:00Z",
  },
  can_intervene: true,
  read_only_reason: null,
  intervention_context: context,
} as WorkerWorkspace;

function renderComposer(
  currentWorkspace: WorkerWorkspace = workspace,
  onRefresh = vi.fn(async () => undefined),
  activities: readonly WorkerActivity[] = [],
) {
  return render(
    <MemoryRouter>
      <WorkerComposer
        workspaceId={WORKSPACE_ID}
        workerItemId={WORKER_ITEM_ID}
        workspace={currentWorkspace}
        execution={undefined}
        activities={activities}
        onRefresh={onRefresh}
      />
    </MemoryRouter>,
  );
}

describe("WorkerComposer", () => {
  beforeEach(() => {
    window.localStorage.clear();
    server.resetHandlers();
  });

  it("sends only the workspace supplied exact context", async () => {
    const user = userEvent.setup();
    let requestBody: Record<string, unknown> = {};
    server.use(
      http.post("/api/v1/commands", async ({ request }) => {
        requestBody = await request.json() as Record<string, unknown>;
        return HttpResponse.json({ command_id: "1", aggregate_id: SESSION_ID, aggregate_version: 5, event_ids: [] });
      }),
    );
    renderComposer();

    await user.type(screen.getByRole("textbox", { name: "介入这个 Worker" }), "先补并发测试");
    await user.click(screen.getByRole("button", { name: "发送到当前 Session" }));

    expect(requestBody.target_id).toBe(SESSION_ID);
    expect(requestBody.expected_version).toBe(context.session_version);
    const payload = requestBody.payload as Record<string, unknown>;
    expect(payload.context).toEqual(context);
    expect(payload).not.toHaveProperty("plan_snapshot_id");
    expect(payload).not.toHaveProperty("work_item_key");
    expect(payload).not.toHaveProperty("worker_slot_key");
  });

  it("keeps the draft when context changes and does not resend automatically", async () => {
    const user = userEvent.setup();
    const onRefresh = vi.fn(async () => undefined);
    let requestCount = 0;
    server.use(
      http.post("/api/v1/commands", () => {
        requestCount += 1;
        return HttpResponse.json({
          error: {
            code: "execution_context_changed",
            message: "execution context changed",
            details: { changed_fields: ["session_version"] },
          },
        }, { status: 409 });
      }),
    );
    renderComposer(workspace, onRefresh);

    const input = screen.getByRole("textbox", { name: "介入这个 Worker" });
    await user.type(input, "先补并发测试");
    await user.click(screen.getByRole("button", { name: "发送到当前 Session" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("草稿已保留");
    expect(input).toHaveValue("先补并发测试");
    expect(requestCount).toBe(1);
    await waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(1));
    expect(window.localStorage.getItem(workerItemDraftKey(WORKSPACE_ID, WORKER_ITEM_ID))).toContain("先补并发测试");
  });

  it("refreshes after an optimistic session conflict and keeps the draft", async () => {
    const user = userEvent.setup();
    const onRefresh = vi.fn(async () => undefined);
    server.use(
      http.post("/api/v1/commands", () => HttpResponse.json({
        error: {
          code: "version_conflict",
          message: "expected version 4, actual 5",
          details: { expected: 4, actual: 5 },
        },
      }, { status: 409 })),
    );
    renderComposer(workspace, onRefresh);

    const input = screen.getByRole("textbox", { name: "介入这个 Worker" });
    await user.type(input, "先补并发测试");
    await user.click(screen.getByRole("button", { name: "发送到当前 Session" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("草稿已保留");
    expect(input).toHaveValue("先补并发测试");
    await waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(1));
  });

  it("requires explicit context confirmation before sending a retained draft", async () => {
    const user = userEvent.setup();
    window.localStorage.setItem(workerItemDraftKey(WORKSPACE_ID, WORKER_ITEM_ID), JSON.stringify({
      text: "切换后继续检查",
      clientIdempotencyKey: "retained-draft",
      lastContextFingerprint: "source-session|old",
    }));

    renderComposer();

    const input = screen.getByRole("textbox", { name: "介入这个 Worker" });
    expect(input).toBeDisabled();
    expect(screen.getByRole("button", { name: "确认当前 context" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "确认当前 context" }));

    expect(input).toBeEnabled();
    expect(input).toHaveValue("切换后继续检查");
  });

  it("keeps the composer visible but disabled with a human read-only reason", async () => {
    renderComposer({
      ...workspace,
      can_intervene: false,
      read_only_reason: "worker_item_retired",
      intervention_context: null,
      worker_item: { ...workspace.worker_item, status: "retired" },
    });

    expect(screen.getByRole("textbox", { name: "介入这个 Worker" })).toBeDisabled();
    expect(screen.getByText("这个 WorkerItem 已退役，只能查看历史上下文")).toBeVisible();
    expect(screen.getByRole("button", { name: "发送到当前 Session" })).toBeDisabled();
  });

  it("keeps the draft but makes the composer read-only during an active handoff", async () => {
    window.localStorage.setItem(workerItemDraftKey(WORKSPACE_ID, WORKER_ITEM_ID), JSON.stringify({
      text: "切换完成后再发送",
      clientIdempotencyKey: "draft-key",
      lastContextFingerprint: "",
    }));

    renderComposer({ ...workspace, active_handoff: activeHandoff });

    expect(screen.getByRole("textbox", { name: "介入这个 Worker" })).toHaveValue("切换完成后再发送");
    expect(screen.getByRole("textbox", { name: "介入这个 Worker" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "发送到当前 Session" })).toBeDisabled();
    expect(screen.getByText("Agent 正在切换，当前 Session 暂停写入；草稿已保留。")).toBeVisible();
  });

  it("resends a superseded input as a new input on the current Session", async () => {
    const user = userEvent.setup();
    let requestBody: Record<string, unknown> = {};
    server.use(
      http.post("/api/v1/commands", async ({ request }) => {
        requestBody = await request.json() as Record<string, unknown>;
        return HttpResponse.json({ command_id: "2", aggregate_id: "new-input", aggregate_version: 1, event_ids: [] });
      }),
    );
    renderComposer(workspace, undefined, [{
      activity_id: "a0000000-0000-0000-0000-000000000010",
      source_type: "execution_input",
      source_id: "d0000000-0000-0000-0000-000000000010",
      execution_id: EXECUTION_ID,
      session_id: OLD_SESSION_ID,
      occurred_at: "2026-08-23T07:45:00Z",
      kind: "user_input",
      title: "用户介入",
      summary: "旧 Session 上的补充要求",
      correlation_id: "b0000000-0000-0000-0000-000000000010",
      body: "旧 Session 上的补充要求",
      delivery_status: "superseded",
      artifact_ids: [],
      trace_descriptor: null,
    }]);

    await user.click(screen.getByRole("button", { name: "重新发送" }));

    await waitFor(() => expect(requestBody.target_id).toBe(SESSION_ID));
    const payload = requestBody.payload as Record<string, unknown>;
    expect(payload.text).toBe("旧 Session 上的补充要求");
    expect(payload.client_idempotency_key).not.toBe("d0000000-0000-0000-0000-000000000010");
    expect(payload.context).toEqual(context);
    expect(requestBody.target_id).not.toBe(OLD_SESSION_ID);
  });
});
