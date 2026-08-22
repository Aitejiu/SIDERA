import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { server } from "@/test/server";

import { WorkerComposer } from "./WorkerComposer";
import { workerItemDraftKey } from "../trace/draftStore";
import type { WorkerWorkspace } from "./types";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const WORKER_ITEM_ID = "40000000-0000-0000-0000-000000000001";
const EXECUTION_ID = "60000000-0000-0000-0000-000000000002";
const SESSION_ID = "70000000-0000-0000-0000-000000000002";
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
) {
  return render(
    <MemoryRouter>
      <WorkerComposer
        workspaceId={WORKSPACE_ID}
        workerItemId={WORKER_ITEM_ID}
        workspace={currentWorkspace}
        execution={undefined}
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
});
