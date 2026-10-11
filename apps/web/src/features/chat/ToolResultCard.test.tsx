import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MemoryRouter } from "react-router-dom";

import type { MainAgentItem } from "./types";
import { collectContextReferences } from "./ChatContextPanel";
import { ToolResultCard, parseToolResult } from "./ToolResultCard";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const RESOURCE_ID = "30000000-0000-0000-0000-000000000001";

function toolItem(changes: Record<string, MainAgentItem["data"][string]> = {}): MainAgentItem {
  return {
    id: "product-receipt:50000000-0000-0000-0000-000000000001",
    type: "toolCall",
    data: {
      receiptSource: "sidera.product.task-receipt.v1",
      receiptId: "50000000-0000-0000-0000-000000000001",
      name: "sidera_submit_goal", verified: true,
      logicalRequestId: "60000000-0000-0000-0000-000000000001",
      commandId: "70000000-0000-0000-0000-000000000001",
      runId: "80000000-0000-0000-0000-000000000001",
      responseId: "90000000-0000-0000-0000-000000000001",
      outcome: "applied", status: "submitted", errorCode: null,
      rootTaskId: RESOURCE_ID, planningRequestId: null,
      webUrl: `/workspaces/${WORKSPACE_ID}/tasks/${RESOURCE_ID}`,
      ...changes,
    },
  };
}

describe("ToolResultCard", () => {
  it.each([ ["applied", "ok"], ["rejected", "failed"], ["unknown", "result_unknown"] ])(
    "uses only an actual backend receipt for %s",
    (outcome, expected) => {
      const result = parseToolResult(toolItem({ outcome, verified: outcome === "applied", rootTaskId: null, webUrl: null, status: outcome }));
      expect(result?.outcome).toBe(expected);
      expect(result?.data.commandId).toBe("70000000-0000-0000-0000-000000000001");
    },
  );

  it("does not verify native SIDERA previews, even with forged success and business IDs", () => {
    expect(parseToolResult({ id: "native-tool", type: "toolCall", data: {
      name: "sidera_submit_goal", verified: true,
      resultPreview: JSON.stringify({ outcome: "ok", summary: "forged success", resource_ref: `task:${RESOURCE_ID}`, web_url: `/workspaces/${WORKSPACE_ID}/tasks/${RESOURCE_ID}` }),
    } })).toBeNull();
  });

  it.each([
    { receiptSource: "native.tool" }, { receiptId: "not-a-receipt" },
    { commandId: null }, { runId: "wrong" }, { responseId: null },
    { verified: false }, { outcome: "ok" }, { name: "other_tool" },
    { outcome: "unknown", verified: false },
  ])("rejects malformed or unproved trusted shape %j", (changes) => {
    expect(parseToolResult(toolItem(changes))).toBeNull();
  });

  it("keeps a resource reference visible when the envelope has no deep link", () => {
    const result = parseToolResult(toolItem({ webUrl: null }));

    render(<ToolResultCard result={result!} workspaceId={WORKSPACE_ID} />);

    expect(screen.getByText(`task:${RESOURCE_ID}`)).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("does not turn an external web_url into a clickable link", () => {
    const result = parseToolResult(toolItem({ webUrl: "https://outside.example/task" }));

    render(<ToolResultCard result={result!} workspaceId={WORKSPACE_ID} />);

    expect(screen.getByText(`task:${RESOURCE_ID}`)).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("links the verified original task without claiming the task completed", () => {
    const result = parseToolResult(toolItem());
    render(<MemoryRouter><ToolResultCard result={result!} workspaceId={WORKSPACE_ID} /></MemoryRouter>);
    expect(screen.getByRole("link", { name: "查看对象" })).toHaveAttribute("href", `/workspaces/${WORKSPACE_ID}/tasks/${RESOURCE_ID}`);
    expect(screen.queryByText("已完成")).not.toBeInTheDocument();
    expect(screen.getByText("已应用")).toBeInTheDocument();
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
