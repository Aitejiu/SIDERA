import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { RouterProvider } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { createTestRouter } from "./router";

describe("app shell", () => {
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
});
