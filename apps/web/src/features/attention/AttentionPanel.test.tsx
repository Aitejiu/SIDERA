import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { useState } from "react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { server } from "@/test/server";

import { AttentionPanel } from "./AttentionPanel";

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

describe("AttentionPanel", () => {
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
