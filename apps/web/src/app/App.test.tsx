import { render, screen } from "@testing-library/react";
import { RouterProvider } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { createTestRouter } from "./router";

describe("app shell", () => {
  it("opens the task control room with workspace navigation", async () => {
    const router = createTestRouter([
      "/workspaces/20000000-0000-0000-0000-000000000001/tasks",
    ]);
    render(<RouterProvider router={router} />);

    expect(await screen.findByText("SIDERA")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "任务" })).toBeInTheDocument();
    expect(screen.getByRole("main")).toBeInTheDocument();
    expect(screen.getByText("工作区 / 任务")).toBeInTheDocument();
  });
});
