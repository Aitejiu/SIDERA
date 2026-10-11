import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { useState } from "react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { server } from "@/test/server";

import { AttentionPanel } from "./AttentionPanel";
import { useNativeAttentionAcknowledgement } from "./useNativeAttentionAcknowledgement";
import type { CommandRequest } from "../assignments/types";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const ROOT_TASK_ID = "30000000-0000-0000-0000-000000000001";
const ITEM = {
  key: "subtask:30000000-0000-0000-0000-000000000002:blocked",
  kind: "blocked",
  task_title: "Control room",
  subtask_key: "backend",
  subtask_title: "Blocked worker",
  reason: "Waiting for intervention",
  resource_id: "30000000-0000-0000-0000-000000000002",
  resource_version: 3,
  correlation_id: "00000000-0000-0000-0000-000000000000",
  occurred_at: "2026-08-21T08:00:00Z",
  requires_operator: false,
};

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="attention-location">{location.pathname}</output>;
}

function RefreshingAttentionPanel() {
  const [items, setItems] = useState([ITEM]);
  return (
    <AttentionPanel
      workspaceId={WORKSPACE_ID}
      rootTaskId={ROOT_TASK_ID}
      rootTaskVersion={1}
      items={items}
      onRefresh={async () => setItems([])}
    />
  );
}

function NativePanel({ item = ITEM, onAcknowledged }: { item?: typeof ITEM; onAcknowledged: () => void }) {
  const action = useNativeAttentionAcknowledgement({ workspaceId: WORKSPACE_ID, rootTaskId: ROOT_TASK_ID, onRefresh: async () => undefined });
  return <AttentionPanel workspaceId={WORKSPACE_ID} rootTaskId={ROOT_TASK_ID} rootTaskVersion={11}
    items={[item]} nativeAction={action} onAcknowledged={onAcknowledged} />;
}

function nativeHandlers(kind = "mapped") {
  const posted: CommandRequest[] = [];
  server.use(
    http.get("/main-agent/v1/health", () => HttpResponse.json({ status: "ok", service: "sidera-product" })),
    http.get("/product/v1/session", () => HttpResponse.json({ session_id: "operator-session", workspace_id: WORKSPACE_ID, principal_id: "operator", expires_at: "2099-01-01T00:00:00Z" })),
    http.get(`/api/v1/tasks/${ROOT_TASK_ID}`, () => HttpResponse.json({
      id: ROOT_TASK_ID, workspace_id: WORKSPACE_ID, version: 11, title: "Task", description: "Review", acceptance_criteria: [],
      requested_by: "operator", priority: 1, status: "running", created_at: "2026-10-07T00:00:00Z", updated_at: "2026-10-07T00:00:00Z",
    })),
    http.get(`/api/v1/tasks/${ROOT_TASK_ID}/graph`, () => HttpResponse.json({ schema_version: 1, nodes: [], edges: [], milestones: [], overview: {
      schema_version: 1, workspace_id: WORKSPACE_ID, root_task_id: ROOT_TASK_ID, title: "Task", goal_summary: "Review", plan_snapshot_id: ROOT_TASK_ID,
      generation: 1, acceptance_refs: [], current_milestone_key: null, subtask_count: 0, planned_subtask_count: 0,
      running_subtask_count: 0, completed_subtask_count: 0, failed_subtask_count: 0, handoff_blocked_count: 0,
      handoff_running_count: 0, next_subtask_keys: [], attention_items: [ITEM], last_change_cursor: 1, as_of: "2026-10-07T00:00:00Z",
    } })),
    http.post("/api/v1/commands", async ({ request }) => {
      const command = await request.json() as CommandRequest; posted.push(command);
      return kind === "lost" ? HttpResponse.error() : HttpResponse.json({ command_id: kind === "foreign" ? ITEM.correlation_id : command.command_id,
        aggregate_id: ROOT_TASK_ID, aggregate_version: kind === "wrong-version" ? 12 : 11, event_ids: [], data: {} });
    }),
  );
  return posted;
}

function mountNative(onAcknowledged: () => void) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const content = (item = ITEM) => <QueryClientProvider client={client}><MemoryRouter><NativePanel item={item} onAcknowledged={onAcknowledged} /></MemoryRouter></QueryClientProvider>;
  const view = render(content());
  return { ...view, nextFact: () => view.rerender(content({ ...ITEM, resource_version: 4 })) };
}

describe("AttentionPanel", () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => vi.restoreAllMocks());

  it.each(["lost", "foreign", "wrong-version"])("does not hide a native item or notify its host on %s response", async (kind) => {
    const posted = nativeHandlers(kind), acknowledged = vi.fn(); mountNative(acknowledged);
    await waitFor(() => expect(screen.getByRole("button", { name: "确认已处理" })).toBeEnabled());
    await userEvent.setup().click(screen.getByRole("button", { name: "确认已处理" }));
    await waitFor(() => expect(posted).toHaveLength(1));
    await waitFor(() => expect(screen.getByRole("button", { name: "确认已处理" })).toBeDisabled());
    expect(screen.getByText(ITEM.reason)).toBeVisible(); expect(acknowledged).not.toHaveBeenCalled();
  });

  it("notifies its host only after real native mapped acknowledgement", async () => {
    const posted = nativeHandlers(), acknowledged = vi.fn(); mountNative(acknowledged);
    await waitFor(() => expect(screen.getByRole("button", { name: "确认已处理" })).toBeEnabled());
    await userEvent.setup().click(screen.getByRole("button", { name: "确认已处理" }));
    expect(await screen.findByText("当前没有待确认事项")).toBeVisible(); expect(acknowledged).toHaveBeenCalledOnce();
    expect(posted[0]?.actor).toEqual({ type: "user", id: "operator" });
  });

  it("hides only the exact acknowledged fact while a new source version remains visible", async () => {
    nativeHandlers(); const view = mountNative(vi.fn());
    await waitFor(() => expect(screen.getByRole("button", { name: "确认已处理" })).toBeEnabled());
    await userEvent.setup().click(screen.getByRole("button", { name: "确认已处理" }));
    expect(await screen.findByText("当前没有待确认事项")).toBeVisible(); view.nextFact();
    expect(screen.getByText(ITEM.reason)).toBeVisible(); expect(screen.getByRole("button", { name: "确认已处理" })).toBeVisible();
  });
  it.each(["title", "reason"])("opens the exact WorkerItem from the %s without acknowledging the item", async (target) => {
    let commands = 0;
    server.use(http.post("/api/v1/commands", () => {
      commands += 1;
      return HttpResponse.json({});
    }));
    const user = userEvent.setup();
    render(<MemoryRouter>
      <AttentionPanel workspaceId={WORKSPACE_ID} rootTaskId={ROOT_TASK_ID} rootTaskVersion={1}
        rootTaskTitle="Control room" items={[{ ...ITEM, worker_item_id: "worker-123" }]} compact />
      <LocationProbe />
    </MemoryRouter>);

    const link = screen.getByRole("link", { name: /Control room \/ Blocked worker/ });
    expect(link).toHaveAttribute("href", `/workspaces/${WORKSPACE_ID}/worker-items/worker-123`);
    await user.click(screen.getByText(target === "title" ? "Control room / Blocked worker" : ITEM.reason));
    expect(screen.getByTestId("attention-location")).toHaveTextContent(`/workspaces/${WORKSPACE_ID}/worker-items/worker-123`);
    expect(commands).toBe(0);
    expect(screen.getByRole("button", { name: "确认已处理" })).toBeVisible();
  });

  it("opens the subtask when no exact WorkerItem is attached", () => {
    render(<AttentionPanel workspaceId={WORKSPACE_ID} rootTaskId={ROOT_TASK_ID}
      rootTaskVersion={undefined} items={[ITEM]} />, { wrapper: MemoryRouter });
    expect(screen.getByRole("link", { name: /Blocked worker/ })).toHaveAttribute(
      "href", `/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}/subtasks/backend`,
    );
    expect(screen.getByRole("button", { name: "确认已处理" })).toBeDisabled();
  });

  it("uses the WorkerItem page rather than a handoff projection API deep link", () => {
    render(<AttentionPanel workspaceId={WORKSPACE_ID} rootTaskId={ROOT_TASK_ID} rootTaskVersion={1}
      items={[{ ...ITEM, kind: "handoff", worker_item_id: "worker-123",
        deep_link: `/api/v1/workspaces/${WORKSPACE_ID}/worker-items/worker-123/workspace` }]} />,
    { wrapper: MemoryRouter });
    expect(screen.getByRole("link", { name: /Blocked worker/ })).toHaveAttribute(
      "href", `/workspaces/${WORKSPACE_ID}/worker-items/worker-123`,
    );
  });

  it("does not use an API deep link as a page when no WorkerItem is attached", () => {
    render(<AttentionPanel workspaceId={WORKSPACE_ID} rootTaskId={ROOT_TASK_ID} rootTaskVersion={1}
      items={[{ ...ITEM, deep_link: "/api/v1/tasks/source-projection" }]} />, { wrapper: MemoryRouter });
    expect(screen.getByRole("link", { name: /Blocked worker/ })).toHaveAttribute(
      "href", `/workspaces/${WORKSPACE_ID}/tasks/${ROOT_TASK_ID}/subtasks/backend`,
    );
  });

  it("keeps the blocker and action visible in compact mode while disclosing event metadata", async () => {
    const user = userEvent.setup();
    render(
      <AttentionPanel
        workspaceId={WORKSPACE_ID}
        rootTaskId={ROOT_TASK_ID}
        rootTaskVersion={1}
        rootTaskTitle="Control room"
        items={[ITEM]}
        compact
      />,
      { wrapper: MemoryRouter },
    );

    expect(screen.getByText("Control room / Blocked worker")).toBeVisible();
    expect(screen.getByText(ITEM.reason)).toBeVisible();
    expect(screen.getByRole("button", { name: "确认已处理" })).toBeEnabled();
    expect(screen.getByText(/SOURCE 30000000/)).not.toBeVisible();
    await user.click(screen.getByText("事件详情"));
    expect(screen.getByText(/SOURCE 30000000/)).toBeVisible();
    expect(screen.getByText(/SOURCE 30000000/)).toHaveTextContent("blocked");
    await user.click(screen.getByText("事件详情"));
    expect(screen.getByText(/SOURCE 30000000/)).not.toBeVisible();
  });

  it("acknowledges one exact source item", async () => {
    let requestBody: Record<string, unknown> | undefined;
    server.use(
      http.post("/api/v1/commands", async ({ request }) => {
        requestBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({
          command_id: requestBody.command_id,
          aggregate_id: ROOT_TASK_ID,
          aggregate_version: 1,
          event_ids: ["70000000-0000-0000-0000-000000000001"],
        });
      }),
    );

    const user = userEvent.setup();
    render(
      <AttentionPanel
        workspaceId={WORKSPACE_ID}
        rootTaskId={ROOT_TASK_ID}
        rootTaskVersion={1}
        items={[ITEM]}
      />,
      { wrapper: MemoryRouter },
    );

    await user.click(screen.getByRole("button", { name: "确认已处理" }));

    expect(await screen.findByText("当前没有待确认事项")).toBeVisible();
    expect(requestBody?.command_type).toBe("AcknowledgeAttentionItem");
    expect(requestBody?.target_id).toBe(ROOT_TASK_ID);
    expect((requestBody?.payload as Record<string, unknown>).attention_key).toBe(ITEM.key);
    expect((requestBody?.payload as Record<string, unknown>).source_correlation_id).toBe(ITEM.correlation_id);
    expect((requestBody?.payload as Record<string, unknown>).source_resource_version).toBe(ITEM.resource_version);
  });

  it("refreshes away a cached item when the source is no longer current", async () => {
    server.use(
      http.post("/api/v1/commands", () => HttpResponse.json(
        { error: { code: "validation_error", message: "attention item is no longer current" } },
        { status: 409 },
      )),
    );

    const user = userEvent.setup();
    render(<RefreshingAttentionPanel />, { wrapper: MemoryRouter });

    await user.click(screen.getByRole("button", { name: "确认已处理" }));

    expect(await screen.findByText("当前没有待确认事项")).toBeVisible();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("navigates to a release proposal without acknowledging it", async () => {
    const proposalItem = {
      ...ITEM,
      key: "collaboration_release_proposal:90000000-0000-0000-0000-000000000001",
      kind: "collaboration_release_proposal",
      worker_item_id: "40000000-0000-0000-0000-000000000001",
      deep_link: `/workspaces/${WORKSPACE_ID}/worker-items/40000000-0000-0000-0000-000000000001?view=governance`,
      reason: "审查者的放行建议等待决定",
    };

    render(
      <MemoryRouter>
        <AttentionPanel
          workspaceId={WORKSPACE_ID}
          rootTaskId={ROOT_TASK_ID}
          rootTaskVersion={1}
          items={[proposalItem]}
        />
      </MemoryRouter>,
    );

    const link = screen.getByRole("link", { name: "查看建议：Blocked worker" });
    expect(link).toHaveAttribute("href", proposalItem.deep_link);
    expect(screen.queryByRole("button", { name: "确认已处理" })).not.toBeInTheDocument();
    expect(screen.getByText("审查者的放行建议等待决定")).toBeVisible();
  });
});
