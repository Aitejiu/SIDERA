import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
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

describe("AttentionPanel", () => {
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
    );

    await user.click(screen.getByRole("button", { name: "确认已处理" }));

    expect(await screen.findByText("当前没有待确认事项")).toBeVisible();
    expect(requestBody?.command_type).toBe("AcknowledgeAttentionItem");
    expect(requestBody?.target_id).toBe(ROOT_TASK_ID);
    expect((requestBody?.payload as Record<string, unknown>).attention_key).toBe(ITEM.key);
  });

  it("navigates to a release proposal without acknowledging it", async () => {
    const proposalItem = {
      ...ITEM,
      key: "collaboration_release_proposal:90000000-0000-0000-0000-000000000001",
      kind: "collaboration_release_proposal",
      deep_link: `/workspaces/${WORKSPACE_ID}/worker-items/40000000-0000-0000-0000-000000000001`,
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
