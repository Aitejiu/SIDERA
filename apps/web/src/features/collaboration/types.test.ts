import { describe, expect, it } from "vitest";

import { workerCollaborationLabel } from "./types";

describe("workerCollaborationLabel", () => {
  it.each([
    ["unmanaged", "按现有流程调度"],
    ["waiting_dependency", "等待上游结果"],
    ["waiting_artifact", "等待必要产物"],
    ["waiting_evidence", "等待必要证据"],
    ["waiting_release", "等待协同放行"],
    ["released", "协同已放行，等待调度"],
    ["materialized", "已进入执行"],
  ])("maps %s to human copy", (state, label) => {
    expect(workerCollaborationLabel(state)).toBe(label);
  });

  it("keeps nullable and unknown states out of the UI", () => {
    expect(workerCollaborationLabel(null)).toBeNull();
    expect(workerCollaborationLabel(undefined)).toBeNull();
    expect(workerCollaborationLabel("future_state")).toBeNull();
  });
});
