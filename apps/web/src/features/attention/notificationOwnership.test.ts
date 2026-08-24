import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ProjectionChange } from "@/shared/api/projectionStream";

import {
  clearNotificationOwnershipCache,
  resolveNotificationOwner,
  type NotificationOwnershipApi,
} from "./notificationOwnership";

const WORKSPACE_ID = "workspace-1";
const REQUEST_ID = "request-1";
const ROOT_TASK_ID = "root-task-1";

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
    occurred_at: "2026-08-24T00:00:00Z",
    correlation_id: "correlation-1",
  };
}

function api(overrides: Partial<NotificationOwnershipApi> = {}): NotificationOwnershipApi {
  return {
    getPlanningRequest: vi.fn(async () => ({ source_type: "sidera_web_chat" })),
    listPlanningRequests: vi.fn(async () => ({
      items: [],
      next_cursor: null,
    })),
    getPlanningMaterialization: vi.fn(async () => ({ root_task_id: ROOT_TASK_ID })),
    ...overrides,
  };
}

describe("notification ownership", () => {
  beforeEach(() => {
    clearNotificationOwnershipCache();
  });

  it.each([
    ["web", "web"],
    ["sidera_web_chat", "web"],
    ["akashic_private", "private"],
    ["future_channel", "unknown"],
  ])("maps planning source %s to %s", async (sourceType, expected) => {
    const ownershipApi = api({
      getPlanningRequest: vi.fn(async () => ({ source_type: sourceType })),
    });

    await expect(
      resolveNotificationOwner(
        WORKSPACE_ID,
        change("planning_request", REQUEST_ID),
        ownershipApi,
      ),
    ).resolves.toBe(expected);
  });

  it("resolves a task overview through the first 100 materialized requests and caches the root owner", async () => {
    const ownershipApi = api({
      listPlanningRequests: vi.fn(async () => ({
        items: [
          {
            id: REQUEST_ID,
            status: "materialized",
            source_type: "sidera_web_chat",
          },
        ],
        next_cursor: null,
      })),
      getPlanningMaterialization: vi.fn(async () => ({ root_task_id: ROOT_TASK_ID })),
    });

    await expect(
      resolveNotificationOwner(
        WORKSPACE_ID,
        change("task_overview", ROOT_TASK_ID),
        ownershipApi,
      ),
    ).resolves.toBe("web");
    await expect(
      resolveNotificationOwner(
        WORKSPACE_ID,
        change("task_overview", ROOT_TASK_ID),
        ownershipApi,
      ),
    ).resolves.toBe("web");

    expect(ownershipApi.listPlanningRequests).toHaveBeenCalledTimes(1);
    expect(ownershipApi.getPlanningMaterialization).toHaveBeenCalledTimes(1);
  });

  it("preheats the root cache from a materialization change", async () => {
    const ownershipApi = api({
      getPlanningRequest: vi.fn(async () => ({ source_type: "akashic_private" })),
      getPlanningMaterialization: vi.fn(async () => ({ root_task_id: ROOT_TASK_ID })),
    });

    await expect(
      resolveNotificationOwner(
        WORKSPACE_ID,
        change("planning_materialization", REQUEST_ID),
        ownershipApi,
      ),
    ).resolves.toBe("private");
    await expect(
      resolveNotificationOwner(
        WORKSPACE_ID,
        change("task_overview", ROOT_TASK_ID),
        ownershipApi,
      ),
    ).resolves.toBe("private");

    expect(ownershipApi.listPlanningRequests).not.toHaveBeenCalled();
  });

  it("does not inspect more than 100 materialized requests", async () => {
    const ownershipApi = api({
      listPlanningRequests: vi.fn(async () => ({
        items: Array.from({ length: 101 }, (_, index) => ({
          id: `request-${index}`,
          status: "materialized",
          source_type: "sidera_web_chat",
        })),
        next_cursor: null,
      })),
    });

    await expect(
      resolveNotificationOwner(
        WORKSPACE_ID,
        change("task_overview", "not-found-root"),
        ownershipApi,
      ),
    ).resolves.toBe("unknown");

    expect(ownershipApi.getPlanningMaterialization).toHaveBeenCalledTimes(100);
  });

  it.each([
    ["404", new Error("not found")],
    ["network error", new Error("socket closed")],
  ])("returns unknown for a planning lookup %s", async (_label, error) => {
    const ownershipApi = api({
      getPlanningRequest: vi.fn(async () => {
        throw error;
      }),
    });

    await expect(
      resolveNotificationOwner(
        WORKSPACE_ID,
        change("planning_request", REQUEST_ID),
        ownershipApi,
      ),
    ).resolves.toBe("unknown");
  });

  it("returns unknown for non-root or malformed changes without logging response data", async () => {
    const ownershipApi = api();
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);

    await expect(
      resolveNotificationOwner(
        WORKSPACE_ID,
        change("worker_item_workspace", "worker-item-1"),
        ownershipApi,
      ),
    ).resolves.toBe("unknown");
    await expect(
      resolveNotificationOwner(
        WORKSPACE_ID,
        change("task_overview", null),
        ownershipApi,
      ),
    ).resolves.toBe("unknown");

    expect(consoleError).not.toHaveBeenCalled();
    consoleError.mockRestore();
  });
});
