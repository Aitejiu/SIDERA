import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { MainAgentItem } from "./types";
import { collectContextReferences } from "./ChatContextPanel";
import { ToolResultCard, parseToolResult } from "./ToolResultCard";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const RESOURCE_ID = "30000000-0000-0000-0000-000000000001";

function toolItem(
  name = "sidera_get_task_status",
  preview = JSON.stringify({
    outcome: "ok",
    summary: "Task progress loaded.",
    resource_ref: `task:${RESOURCE_ID}`,
    web_url: `/workspaces/${WORKSPACE_ID}/tasks/${RESOURCE_ID}`,
  }),
): MainAgentItem {
  return {
    id: "tool-1",
    type: "toolCall",
    data: { name, resultPreview: preview },
  };
}

describe("ToolResultCard", () => {
  it.each(["ok", "needs_user", "failed", "unavailable", "conflict", "result_unknown"])(
    "accepts the %s ToolEnvelope outcome",
    (outcome) => {
      const result = parseToolResult(toolItem(
        "sidera_get_planning_status",
        JSON.stringify({ outcome, summary: `summary-${outcome}`, resource_ref: null }),
      ));

      expect(result?.outcome).toBe(outcome);
      expect(result?.summary).toBe(`summary-${outcome}`);
    },
  );

  it("ignores malformed previews and non-SIDERA tools", () => {
    expect(parseToolResult(toolItem("sidera_get_task_status", "not-json"))).toBeNull();
    expect(parseToolResult(toolItem("other_tool"))).toBeNull();
  });

  it("keeps a resource reference visible when the envelope has no deep link", () => {
    const result = parseToolResult(toolItem(
      "sidera_get_task_status",
      JSON.stringify({ outcome: "ok", summary: "Loaded", resource_ref: `task:${RESOURCE_ID}` }),
    ));

    render(<ToolResultCard result={result!} workspaceId={WORKSPACE_ID} />);

    expect(screen.getByText(`task:${RESOURCE_ID}`)).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("does not turn an external web_url into a clickable link", () => {
    const result = parseToolResult(toolItem(
      "sidera_get_task_status",
      JSON.stringify({
        outcome: "ok",
        summary: "Loaded",
        resource_ref: `task:${RESOURCE_ID}`,
        web_url: "https://outside.example/task",
      }),
    ));

    render(<ToolResultCard result={result!} workspaceId={WORKSPACE_ID} />);

    expect(screen.getByText(`task:${RESOURCE_ID}`)).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("deduplicates supported control-plane references in first-seen order", () => {
    const references = collectContextReferences(
      [
        `task:${RESOURCE_ID}`,
        `planning_request:${RESOURCE_ID}`,
        `task:${RESOURCE_ID}`,
        `subtask:${RESOURCE_ID}:design`,
        `worker_item:${RESOURCE_ID}`,
      ],
      [`task:${RESOURCE_ID}`],
      WORKSPACE_ID,
    );

    expect(references.map((reference) => reference.key)).toEqual([
      `task:${RESOURCE_ID}`,
      `planning_request:${RESOURCE_ID}`,
      `subtask:${RESOURCE_ID}:design`,
      `worker_item:${RESOURCE_ID}`,
    ]);
    expect(references[2]?.href).toContain(`/subtasks/design`);
  });
});
