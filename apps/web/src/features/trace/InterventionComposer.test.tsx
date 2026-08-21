import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { server } from "@/test/server";

import { InterventionComposer } from "./InterventionComposer";
import { interventionDraftKey } from "./draftStore";
import type { Execution, ExecutionContextAddress, ExecutionSession } from "./types";

const context: ExecutionContextAddress = {
  plan_snapshot_id: "10000000-0000-0000-0000-000000000002",
  root_task_id: "30000000-0000-0000-0000-000000000001",
  work_item_key: "backend",
  worker_slot_key: "backend",
  worker_id: "80000000-0000-0000-0000-000000000001",
  execution_id: "60000000-0000-0000-0000-000000000001",
  session_id: "70000000-0000-0000-0000-000000000001",
};

const session: ExecutionSession = {
  id: context.session_id,
  workspace_id: "20000000-0000-0000-0000-000000000001",
  execution_id: context.execution_id,
  worker_id: context.worker_id,
  adapter_session_id: "opaque",
  status: "active",
  last_sequence: 12,
  last_activity_at: "2026-08-21T08:00:00Z",
  input_mode: "queued",
  version: 4,
  adapter_module_id: null,
  adapter_config_version: null,
  capability_snapshot: null,
  created_at: "2026-08-21T08:00:00Z",
  updated_at: "2026-08-21T08:00:00Z",
  workspace_config_version: null,
  workspace_module_id: null,
  workspace_ref: null,
};

const execution: Execution = {
  id: context.execution_id,
  workspace_id: session.workspace_id,
  task_id: "40000000-0000-0000-0000-000000000001",
  lineage_id: context.execution_id,
  attempt: 1,
  max_attempts: 3,
  purpose: "backend",
  status: "running",
  capability_requirements: ["python"],
  pinned_worker_id: context.worker_id,
  assigned_worker_id: context.worker_id,
  active_lease_id: null,
  last_fencing_token: 1,
  not_before: "2026-08-21T08:00:00Z",
  failure_code: null,
  started_at: "2026-08-21T08:00:00Z",
  finished_at: null,
  version: 5,
  stop_requested: false,
  source_execution_id: null,
  source_session_id: null,
  created_at: "2026-08-21T08:00:00Z",
  updated_at: "2026-08-21T08:00:00Z",
};

function renderComposer(traceFresh = true, inputs: never[] = []) {
  return render(
    <MemoryRouter>
      <InterventionComposer
        workspaceId={session.workspace_id}
        rootTaskId={context.root_task_id}
        context={context}
        contextBranch={null}
        session={session}
        execution={execution}
        inputs={inputs}
        traceFresh={traceFresh}
        onRefresh={async () => undefined}
      />
    </MemoryRouter>,
  );
}

describe("InterventionComposer", () => {
  beforeEach(() => {
    window.localStorage.clear();
    server.resetHandlers();
  });

  it("sends the full immutable address and keeps the draft on context conflict", async () => {
    const user = userEvent.setup();
    let requestBody: unknown = null;
    server.use(
      http.post("/api/v1/commands", async ({ request }) => {
        requestBody = await request.json() as Record<string, unknown>;
        return HttpResponse.json({ error: {
          code: "execution_context_changed",
          message: "execution context changed",
          details: { changed_fields: ["plan_snapshot_id"], current: { plan_snapshot_id: "new" } },
        } }, { status: 409 });
      }),
    );
    renderComposer();
    const input = screen.getByRole("textbox", { name: "介入内容" });
    await user.type(input, "Keep this draft");
    const key = interventionDraftKey(session.workspace_id, context);
    expect(window.localStorage.getItem(key)).toContain("Keep this draft");

    await user.click(screen.getByRole("button", { name: "发送到当前 Session" }));
    expect(await screen.findByText(/执行地址已变化/)).toBeVisible();
    expect(input).toHaveValue("Keep this draft");
    const sentRequest = requestBody as Record<string, unknown>;
    expect(sentRequest.target_id).toBe(context.session_id);
    expect(sentRequest.expected_version).toBe(session.version);
    expect((sentRequest.payload as Record<string, unknown>).context).toEqual(context);
  });

  it("blocks writes while trace is stale and clears only after the accepted input appears", async () => {
    const user = userEvent.setup();
    const { rerender } = renderComposer(false);
    const input = screen.getByRole("textbox", { name: "介入内容" });
    expect(screen.getByRole("button", { name: "发送到当前 Session" })).toBeDisabled();
    expect(input).toBeDisabled();

    rerender(
      <MemoryRouter>
        <InterventionComposer
          workspaceId={session.workspace_id}
          rootTaskId={context.root_task_id}
          context={context}
          contextBranch={null}
          session={session}
          execution={execution}
          inputs={[]}
          traceFresh
          onRefresh={async () => undefined}
        />
      </MemoryRouter>,
    );
    await user.type(screen.getByRole("textbox", { name: "介入内容" }), "Accepted draft");
    server.use(
      http.post("/api/v1/commands", () => HttpResponse.json({ command_id: "1", aggregate_id: "2", aggregate_version: 1, event_ids: [] })),
    );
    await user.click(screen.getByRole("button", { name: "发送到当前 Session" }));
    const clientKey = JSON.parse(window.localStorage.getItem(interventionDraftKey(session.workspace_id, context)) ?? "{}").clientIdempotencyKey as string;
    rerender(
      <MemoryRouter>
        <InterventionComposer
          workspaceId={session.workspace_id}
          rootTaskId={context.root_task_id}
          context={context}
          contextBranch={null}
          session={session}
          execution={execution}
          inputs={[{ id: "input", workspace_id: session.workspace_id, execution_id: context.execution_id, session_id: context.session_id, client_idempotency_key: clientKey, status: "queued", operation_id: null, failure_code: null, version: 2, text_length: 14, attachment_count: 0, created_at: "2026-08-21T08:00:00Z", updated_at: "2026-08-21T08:00:00Z" }]}
          traceFresh
          onRefresh={async () => undefined}
        />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByRole("textbox", { name: "介入内容" })).toHaveValue(""));
  });
});
