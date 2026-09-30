import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { server } from "@/test/server";

import { DemoModeIndicator, demoStatusLabel } from "./DemoModeIndicator";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";

function renderIndicator() {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <DemoModeIndicator workspaceId={WORKSPACE_ID} />
    </QueryClientProvider>,
  );
}

describe("DemoModeIndicator", () => {
  it("labels only the interview demo mode", () => {
    expect(demoStatusLabel("interview-demo")).toBe("模拟演示");
    expect(demoStatusLabel("ordinary")).toBeNull();
    expect(demoStatusLabel(null)).toBeNull();
  });

  it("shows the source only after the control plane reports demo mode", async () => {
    server.use(
      http.get("/api/v1/demo/status", () => HttpResponse.json({
        mode: "interview-demo",
        run_id: "run",
        workspace_id: WORKSPACE_ID,
        scenario: "mainline",
        phase: "running",
      })),
    );
    renderIndicator();
    expect(await screen.findByText("模拟演示")).toBeVisible();
  });

  it("does not turn an offline status into a connected label", async () => {
    server.use(http.get("/api/v1/demo/status", () => HttpResponse.error()));
    renderIndicator();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByText("模拟演示")).not.toBeInTheDocument();
    expect(screen.queryByText("已连接")).not.toBeInTheDocument();
  });
});
