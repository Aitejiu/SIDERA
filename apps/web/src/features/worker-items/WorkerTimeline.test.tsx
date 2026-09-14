import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { WorkerTimeline } from "./WorkerTimeline";
import type { WorkerActivity } from "./types";

function activity(kind: string, overrides: Partial<WorkerActivity> = {}): WorkerActivity {
  return {
    activity_id: kind,
    source_type: "execution_event",
    source_id: "source-1",
    execution_id: "execution-1",
    session_id: "session-1",
    occurred_at: "2026-09-10T08:00:00Z",
    kind,
    title: kind,
    summary: `${kind} details`,
    correlation_id: "correlation-1",
    body: null,
    delivery_status: null,
    artifact_ids: [],
    trace_descriptor: null,
    ...overrides,
  };
}

describe("WorkerTimeline", () => {
  it("leads an intervention with its complete input instead of repeating a generic title and shortened summary", () => {
    const body = "只修改签名校验。保留现有重试逻辑，不修改其他子任务。";
    render(<WorkerTimeline activities={[activity("user_input", {
      title: "用户介入", summary: "只修改签名校验。保留现有重试逻辑", body, delivery_status: "delivered",
    })]} hasMore={false} isLoadingMore={false} onLoadMore={() => {}} onOpenTrace={() => {}} />);
    const message = within(screen.getByRole("article", { name: body }));
    expect(message.getAllByText(body)).toHaveLength(1);
    expect(message.queryByText("只修改签名校验。保留现有重试逻辑", { exact: true })).not.toBeInTheDocument();
    expect(message.queryByText("用户介入")).not.toBeInTheDocument();
    expect(message.getByText("你的介入")).toBeVisible();
    expect(message.getByText("已投递")).toBeVisible();
  });

  it("associates each intervention with its own content, receipt and exact-session trace", async () => {
    const user = userEvent.setup();
    const onOpenTrace = vi.fn();
    const intervention = activity("user_input", {
      title: "调整回调校验范围",
      summary: "只修改签名校验",
      body: "保留现有重试逻辑，不修改其他子任务。",
      delivery_status: "delivered",
      trace_descriptor: {
        root_task_id: "task-1", execution_id: "execution-1", session_id: "session-1",
        route: "/trace/session-1", raw_ref: null,
      },
    });
    render(<WorkerTimeline activities={[intervention, activity("checkpoint", {
      title: "签名校验已完成", delivery_status: null,
    })]} hasMore={false} isLoadingMore={false} onLoadMore={() => {}} onOpenTrace={onOpenTrace} />);

    const message = within(screen.getByRole("article", { name: "调整回调校验范围" }));
    expect(message.getByText("你的介入")).toBeVisible();
    expect(message.getByText("只修改签名校验")).toBeVisible();
    expect(message.getByText("保留现有重试逻辑，不修改其他子任务。")).toBeVisible();
    expect(message.getByText("已投递")).toBeVisible();
    expect(message.queryByText("已读")).not.toBeInTheDocument();
    const result = within(screen.getByRole("article", { name: "签名校验已完成" }));
    expect(result.queryByText("已投递")).not.toBeInTheDocument();
    await user.click(message.getByRole("button", { name: "查看 Agent Trace" }));
    expect(onOpenTrace).toHaveBeenCalledWith(intervention);
  });

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
    expect(screen.getByText("Agent 切换")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "查看 Agent Trace" }));
    expect(onOpenTrace).toHaveBeenCalledWith(activity);
  });

  it("folds routine lifecycle detail while preserving its exact trace entry", async () => {
    const user = userEvent.setup();
    const onOpenTrace = vi.fn();
    const started = activity("session.started", {
      trace_descriptor: {
        root_task_id: "task-1", execution_id: "execution-1", session_id: "session-1",
        route: "/trace/session-1", raw_ref: null,
      },
    });
    render(<WorkerTimeline activities={[started, activity("execution.running"), activity("checkpoint")]}
      hasMore={false} isLoadingMore={false} onLoadMore={() => {}} onOpenTrace={onOpenTrace} />);

    expect(screen.queryByText("session.started")).not.toBeInTheDocument();
    expect(screen.queryByText("execution.running")).not.toBeInTheDocument();
    expect(screen.getByText("checkpoint details")).toBeVisible();
    await user.click(screen.getByRole("button", { name: /系统记录/ }));
    expect(screen.getByText("session.started details")).not.toBeVisible();
    await user.click(screen.getByRole("button", { name: "查看 Agent Trace" }));
    expect(onOpenTrace).toHaveBeenCalledWith(started);
    await user.click(screen.getByText("session.started", { exact: true }));
    expect(screen.getByText("session.started details")).toBeVisible();
  });

  it("never folds errors, unknown lifecycle events, plan changes or intervention history", () => {
    const events = ["session.failed", "execution.failed", "session.unknown", "plan.generation", "plan.recovery", "user_input"];
    render(<WorkerTimeline activities={events.map(kind => activity(kind))}
      hasMore={false} isLoadingMore={false} onLoadMore={() => {}} onOpenTrace={() => {}} />);
    for (const kind of events) expect(screen.getByText(`${kind} details`)).toBeVisible();
  });

  it("uses the reported step as the headline instead of its transport event name", () => {
    render(<WorkerTimeline activities={[activity("assistant_summary", { title: "assistant.summary", summary: "完成回调测试，等待审查" }),
      activity("tool", { title: "tool.call", summary: "正在执行回调测试" })]}
      hasMore={false} isLoadingMore={false} onLoadMore={() => {}} onOpenTrace={() => {}} />);
    expect(screen.getByText("完成回调测试，等待审查").tagName).toBe("STRONG");
    expect(screen.queryByText("assistant.summary")).not.toBeInTheDocument();
    expect(screen.getByText("正在执行回调测试").tagName).toBe("STRONG");
  });

  it("does not treat revealing system records as newly received work", async () => {
    const user = userEvent.setup();
    render(<WorkerTimeline activities={[activity("checkpoint"), activity("session.started")]}
      initialScrollTop={0} hasMore={false} isLoadingMore={false} onLoadMore={() => {}} onOpenTrace={() => {}} />);
    await user.click(screen.getByRole("button", { name: "系统记录" }));
    expect(screen.queryByRole("button", { name: "有新活动" })).not.toBeInTheDocument();
  });
});
