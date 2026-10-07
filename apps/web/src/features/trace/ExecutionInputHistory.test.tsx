import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ExecutionInput } from "./types";
import { ExecutionInputHistory } from "./ExecutionInputHistory";

const statusLabels = [
  ["accepted", "已接受"],
  ["queued", "已排队"],
  ["dispatching", "投递中"],
  ["delivered", "已投递"],
  ["blocked", "已阻塞"],
  ["failed", "投递失败"],
  ["cancelled", "已取消"],
  ["superseded", "已失效"],
  ["canceled", "已取消"],
  ["rejected", "已拒绝"],
  ["unknown_native_status", "unknown_native_status"],
] as const;

function input(status: string, index: number): ExecutionInput {
  const suffix = String(index + 1).padStart(12, "0");
  return {
    id: `50000000-0000-0000-0000-${suffix}`,
    workspace_id: "20000000-0000-0000-0000-000000000001",
    execution_id: "60000000-0000-0000-0000-000000000001",
    session_id: "70000000-0000-0000-0000-000000000001",
    client_idempotency_key: `key-${status}`,
    status,
    operation_id: null,
    failure_code: null,
    version: 1,
    text_length: index + 1,
    attachment_count: 0,
    created_at: "2026-10-07T00:00:00Z",
    updated_at: "2026-10-07T00:00:00Z",
  };
}

describe("ExecutionInputHistory", () => {
  it("labels native delivery states and preserves legacy or unknown statuses literally", () => {
    render(<ExecutionInputHistory inputs={statusLabels.map(([status], index) => input(status, index))} />);

    for (const [status, label] of statusLabels) {
      const key = screen.getByText(`key-${status}`);
      const row = key.closest("li");
      if (row === null) throw new Error(`missing history row for ${status}`);
      expect(within(row).getByText(label)).toBeVisible();
    }
  });
});
