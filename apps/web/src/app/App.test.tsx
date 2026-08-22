import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import { act, render, screen, waitFor } from "@testing-library/react";
import { RouterProvider } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { createTestRouter } from "./router";

describe("app shell", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
  });

  it("opens the task control room with workspace navigation", async () => {
    const router = createTestRouter([
      "/workspaces/20000000-0000-0000-0000-000000000001/tasks",
    ]);
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    );

    expect(await screen.findByText("SIDERA")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "任务" })).toBeInTheDocument();
    expect(screen.getByRole("main")).toBeInTheDocument();
    expect(screen.getByText("工作区 / 任务")).toBeInTheDocument();
  });

  it("exposes the control-room sections and task workspace tabs", async () => {
    const router = createTestRouter([
      "/workspaces/20000000-0000-0000-0000-000000000001/tasks/30000000-0000-0000-0000-000000000001",
    ]);
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    );

    expect(screen.getByRole("link", { name: "信箱" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "聊天" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "历史" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "能力" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "任务总览" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /任务 30000000/ })).toBeInTheDocument();
  });

  it("returns to the previous semantic tab when a direct child tab is closed", async () => {
    const workspaceId = "20000000-0000-0000-0000-000000000001";
    const rootTaskId = "30000000-0000-0000-0000-000000000001";
    const router = createTestRouter([
      `/workspaces/${workspaceId}/tasks/${rootTaskId}?view=dag`,
    ]);
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const user = userEvent.setup();
    render(
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    );

    await waitFor(() => {
      expect(screen.getByRole("link", { name: /任务 30000000/ })).toBeInTheDocument();
    });

    await act(async () => {
      await router.navigate(
        `/workspaces/${workspaceId}/tasks/${rootTaskId}/subtasks/implementation?view=trace`,
      );
    });
    const closeSubtask = await screen.findByRole("button", { name: "关闭implementation" });
    await user.click(closeSubtask);

    await waitFor(() => {
      expect(router.state.location.pathname).toBe(
        `/workspaces/${workspaceId}/tasks/${rootTaskId}`,
      );
      expect(router.state.location.search).toBe("?view=dag");
    });

    await user.click(screen.getByRole("button", { name: /关闭任务 30000000/ }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe(`/workspaces/${workspaceId}/tasks`);
      expect(router.state.location.search).toBe("");
    });
  });
});
