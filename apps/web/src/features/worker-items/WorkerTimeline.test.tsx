import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { WorkerTimeline } from "./WorkerTimeline";

describe("WorkerTimeline", () => {
  it("keeps handoff milestones in the activity stream and opens its trace entry", async () => {
    const user = userEvent.setup();
    const onOpenTrace = vi.fn();
    const activity = {
      activity_id: "a0000000-0000-0000-0000-000000000001",
      source_type: "handoff_transition",
      source_id: "b0000000-0000-0000-0000-000000000001",
      execution_id: "60000000-0000-0000-0000-000000000001",
      session_id: "70000000-0000-0000-0000-000000000001",
      occurred_at: "2026-08-23T08:05:00Z",
      kind: "handoff.transition",
      title: "Agent 切换：启动目标 Agent",
      summary: "源 Agent 已停止，目标 Agent 正在启动",
      correlation_id: "c0000000-0000-0000-0000-000000000001",
      body: null,
      delivery_status: null,
      artifact_ids: [],
      trace_descriptor: {
        root_task_id: "30000000-0000-0000-0000-000000000001",
        execution_id: "60000000-0000-0000-0000-000000000001",
        session_id: "70000000-0000-0000-0000-000000000001",
        route: "/api/v1/trace",
        raw_ref: null,
      },
    };

    render(
      <MemoryRouter>
        <WorkerTimeline
          activities={[activity]}
          hasMore={false}
          isLoadingMore={false}
          onLoadMore={vi.fn()}
          onOpenTrace={onOpenTrace}
        />
      </MemoryRouter>,
    );

    expect(await screen.findByText("Agent 切换：启动目标 Agent")).toBeVisible();
    expect(screen.getByText("WORKER HANDOFF")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "查看 Agent Trace" }));
    expect(onOpenTrace).toHaveBeenCalledWith(activity);
  });
});
