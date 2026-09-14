import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { TraceDetails } from "./TraceDetails";
import type { WorkerTraceEvent } from "./types";

const traceEvent: WorkerTraceEvent = {
  schema_version: 1, workspace_id: "workspace", root_task_id: "root",
  execution_id: "execution", session_id: "session", plan_snapshot_id: null,
  work_item_key: "backend", sequence: 1, occurred_at: "2026-09-12T10:00:00Z",
  category: "tool", status: "running", title: "pytest", summary: "Running backend tests",
  duration_ms: null, usage: null, artifact_refs: [], provider_payload_ref: null,
  provider_extensions: {},
};

describe("TraceDetails", () => {
  it.each(["future_provider_event", "__proto__", "constructor"])("keeps unknown category %s readable", (category) => {
    render(<TraceDetails event={{ ...traceEvent, category, status: category }} onArtifactSelect={vi.fn()} />);
    expect(screen.getByRole("region", { name: "轨迹详情" })).toHaveTextContent(category);
  });
  it("does not present the event summary as tool input or output", () => {
    render(<TraceDetails event={traceEvent} onArtifactSelect={vi.fn()} />);
    expect(screen.getByRole("region", { name: "摘要" })).toHaveTextContent("Running backend tests");
    for (const name of ["输入", "输出"]) {
      const field = screen.getByRole("region", { name });
      expect(field).toHaveTextContent("未提供");
      expect(field).not.toHaveTextContent("Running backend tests");
    }
  });

  it("keeps supplied raw input and output distinct, including falsy values", () => {
    render(<TraceDetails event={{ ...traceEvent, provider_extensions: { rawInput: { command: "pytest -q" }, rawOutput: false } }} onArtifactSelect={vi.fn()} />);
    expect(screen.getByRole("region", { name: "输入" })).toHaveTextContent('"command": "pytest -q"');
    expect(screen.getByRole("region", { name: "输出" })).toHaveTextContent("false");
  });

  it("renders markdown and code without executing HTML or loading remote images", () => {
    render(<TraceDetails event={{ ...traceEvent, category: "assistant_summary", summary: "## Checks\n\n```sh\npytest -q\n```\n\n<script>alert(1)</script>\n![remote](https://example.com/pixel)" }} onArtifactSelect={vi.fn()} />);
    expect(screen.getByRole("heading", { name: "Checks" })).toBeVisible();
    expect(screen.getByText("pytest -q").tagName).toBe("CODE");
    expect(document.querySelector("script")).toBeNull();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });

  it("keeps unknown provider content and technical provenance available on demand", async () => {
    const user = userEvent.setup();
    render(<TraceDetails event={{ ...traceEvent, provider_extensions: { "vendor:unknown": { safe: true } } }} onArtifactSelect={vi.fn()} />);
    await user.click(screen.getByText("技术详情"));
    const metadata = screen.getByRole("group", { name: "技术详情" });
    expect(within(metadata).getByText("vendor:unknown")).toBeVisible();
    expect(metadata).toHaveTextContent("backend");
    expect(metadata).toHaveTextContent("未提供");
  });
});
