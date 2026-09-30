import { expect, test } from "@playwright/test";

import {
  assertProviderFreeDemo,
  runBasicPlanningJourney,
} from "./support/basic-capability-journey";
import {
  batchId,
  graphEdgeFacts,
  graphNodeFacts,
  graphOverviewTitle,
  graphRootTaskId,
  readBatchState,
  readStableTaskGraph,
  writeBatchState,
  type BasicCapabilityBatchState,
} from "./support/basic-capability-batch";

test.describe("R6 provider-free shared basic capability batch", () => {
  test("creates the shared task batch once, then reads it at every viewport", async ({
    page,
    request,
  }, testInfo) => {
    const writes: string[] = [];
    page.on("request", (requestEvent) => {
      if (!new Set(["GET", "HEAD", "OPTIONS"]).has(requestEvent.method())) {
        writes.push(`${requestEvent.method()} ${new URL(requestEvent.url()).pathname}`);
      }
    });

    const project = testInfo.project.name;
    const workspaceId = process.env.SIDERA_BASIC_FAKE_WORKSPACE_ID;
    expect(workspaceId).toMatch(/^[0-9a-f-]{36}$/i);
    await assertProviderFreeDemo(request, workspaceId!);

    if (project === "basic-batch-desktop") {
      const journey = await runBasicPlanningJourney(page, request);
      const graph = await readStableTaskGraph(request, journey.workspaceId, journey.rootTaskId);
      const nodes = graphNodeFacts(graph);
      const edges = graphEdgeFacts(graph);
      expect(nodes.length).toBeGreaterThan(0);
      expect(new Set(nodes.map((node) => node.subtask_key)).size).toBe(nodes.length);
      expect(graphRootTaskId(graph)).toBe(journey.rootTaskId);
      expect(writes.some((item) => item.startsWith("POST /main-agent/v1/threads"))).toBe(true);
      expect(writes.some((item) => item.startsWith("POST /api/v1/commands"))).toBe(true);

      const state: BasicCapabilityBatchState = {
        schema_version: 1,
        batch_id: batchId(),
        workspace_id: journey.workspaceId,
        planning_request_id: journey.planningRequestId,
        root_task_id: journey.rootTaskId,
        task_title: graphOverviewTitle(graph),
        nodes,
        edges,
        transport: {
          provider_calls: 0,
          desktop_non_get_requests: [...writes],
        },
      };
      writeBatchState(state);
      await expect(page.locator("#task-detail-title")).toHaveText(state.task_title);
      return;
    }

    const state = readBatchState();
    expect(state.workspace_id).toBe(workspaceId);
    expect(state.transport.provider_calls).toBe(0);
    expect(state.transport.desktop_non_get_requests.length).toBeGreaterThan(0);

    const graph = await readStableTaskGraph(request, state.workspace_id, state.root_task_id);
    expect(graphRootTaskId(graph)).toBe(state.root_task_id);
    expect(graphOverviewTitle(graph)).toBe(state.task_title);
    expect(graphNodeFacts(graph)).toEqual(state.nodes);
    expect(graphEdgeFacts(graph)).toEqual(state.edges);

    await page.goto(`/workspaces/${state.workspace_id}/tasks/${state.root_task_id}`);
    await expect(page.locator("#task-detail-title")).toHaveText(state.task_title);
    await page.getByRole("tab", { name: "DAG", exact: true }).click();
    await expect(page.getByRole("region", { name: "任务 DAG" })).toBeVisible();
    await page.getByRole("tab", { name: "看板", exact: true }).click();
    await expect(page.getByRole("region", { name: "子任务状态看板" })).toBeVisible();
    expect(writes).toEqual([]);
  });
});
