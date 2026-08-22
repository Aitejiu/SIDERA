import { describe, expect, it } from "vitest";

import { matchesProjectionQuery } from "./WorkspaceStreamProvider";
import type { ProjectionChange } from "@/shared/api/projectionStream";

const WORKSPACE_ID = "workspace-1";
const ROOT_TASK_ID = "root-task-1";
const WORKER_ITEM_ID = "worker-item-1";

function change(
  projection_type: string,
  resource_id: string | null,
): ProjectionChange {
  return {
    workspace_id: WORKSPACE_ID,
    cursor: 1,
    schema_version: 1,
    projection_type,
    resource_id,
    change_type: "updated",
    resource_version: 2,
    occurred_at: "2026-08-22T00:00:00Z",
    correlation_id: "correlation-1",
  };
}

describe("WorkspaceStreamProvider projection matching", () => {
  it("invalidates only the affected resource for worker and subtask workspaces", () => {
    expect(
      matchesProjectionQuery(
        ["worker-item-workspace", WORKSPACE_ID, WORKER_ITEM_ID],
        WORKSPACE_ID,
        change("worker_item_workspace", WORKER_ITEM_ID),
      ),
    ).toBe(true);
    expect(
      matchesProjectionQuery(
        ["worker-item-workspace", WORKSPACE_ID, "other-worker-item"],
        WORKSPACE_ID,
        change("worker_item_workspace", WORKER_ITEM_ID),
      ),
    ).toBe(false);
    expect(
      matchesProjectionQuery(
        ["subtask-workspace", WORKSPACE_ID, ROOT_TASK_ID, "callback"],
        WORKSPACE_ID,
        change("subtask_workspace", ROOT_TASK_ID),
      ),
    ).toBe(true);
  });

  it("treats a null resource as workspace-wide for its projection type", () => {
    expect(
      matchesProjectionQuery(
        ["worker-item-activity", WORKSPACE_ID, WORKER_ITEM_ID],
        WORKSPACE_ID,
        change("worker_item_activity", null),
      ),
    ).toBe(true);
    expect(
      matchesProjectionQuery(
        ["task-graph", WORKSPACE_ID, ROOT_TASK_ID],
        WORKSPACE_ID,
        change("worker_item_activity", null),
      ),
    ).toBe(false);
  });
});
