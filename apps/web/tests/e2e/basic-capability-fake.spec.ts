import { expect, test } from "./fixtures";

import {
  assertProviderFreeDemo,
  inspectBasicWorkerTrace,
  runBasicPlanningJourney,
  waitForBasicExecutionChain,
} from "./support/basic-capability-journey";

test.describe("R6 provider-free basic planning journey", () => {
  test("runs Web -> Main Agent -> Planner -> accepted result @closure-chain", async ({
    page,
    request,
  }, testInfo) => {
    test.skip(testInfo.project.name !== "basic-fake-desktop", "planning writes belong to the desktop fake project");

    const workspaceId = process.env.SIDERA_BASIC_FAKE_WORKSPACE_ID;
    expect(workspaceId).toBeDefined();
    await assertProviderFreeDemo(request, workspaceId!);

    const journey = await runBasicPlanningJourney(page, request);
    expect(journey.planningRequestId).toMatch(/^[0-9a-f-]{36}$/i);
    expect(journey.rootTaskId).toMatch(/^[0-9a-f-]{36}$/i);
    await expect(page.locator("#task-detail-title")).toBeVisible();
    const chain = await waitForBasicExecutionChain(request, journey);
    await inspectBasicWorkerTrace(page, request, journey, chain);

    await page.goto(`/workspaces/${journey.workspaceId}/tasks/${journey.rootTaskId}?view=results`);
    const results = page.getByRole("region", { name: "成果验收" });
    await expect(results).toContainText("等待用户验收");
    for (const decision of await results.getByRole("combobox").all()) {
      await decision.selectOption("accept_passed");
    }
    await results.getByRole("button", { name: "接受成果" }).click();
    await expect.poll(async () => {
      const response = await request.get(
        `/api/v1/workspaces/${journey.workspaceId}/tasks/${journey.rootTaskId}/results`,
      );
      if (!response.ok()) return "http_error";
      const overview = await response.json() as Record<string, unknown>;
      const acceptance = overview.acceptance;
      return typeof acceptance === "object" && acceptance !== null ? "accepted" : "pending";
    }, { timeout: 60_000, intervals: [250, 500, 1_000] }).toBe("accepted");
    expect(chain.resultOverview.current_candidate).not.toBeNull();
  });

  test("reads selected result evidence before acceptance @closure-result", async ({
    page,
    seedResultIntegration,
    prepareResultIntegrationBrowser,
    advanceResultIntegration,
  }, testInfo) => {
    test.skip(testInfo.project.name !== "basic-fake-desktop", "result closure belongs to the desktop fake project");

    const seed = seedResultIntegration();
    expect(prepareResultIntegrationBrowser(seed.workspace_id).phase).toBe("candidate_failed");

    await page.goto(`/workspaces/${seed.workspace_id}/tasks/${seed.root_task_id}?view=results`);
    const results = page.getByRole("region", { name: "成果验收" });
    await expect(results).toContainText("等待用户验收");
    await expect(results).toContainText("Expired credentials are rejected");
    await expect(results).toContainText("检查未通过");
    await expect(results.getByRole("link", { name: "查看 / 下载" })).toHaveCount(6);

    const repaired = advanceResultIntegration(seed.workspace_id, "repair");
    expect(repaired.phase).toBe("candidate_passed");
    expect(repaired.current_candidate_id).not.toBe(seed.current_candidate_id);
    await page.reload();
    await expect(results).toContainText("检查已通过");
    const decision = results.getByRole("combobox", { name: "Expired credentials are rejected 的决定" });
    await decision.selectOption("accept_passed");
    const commandResponse = page.waitForResponse((response) => (
      response.request().method() === "POST"
      && new URL(response.url()).pathname === "/api/v1/commands"
    ));
    await results.getByRole("button", { name: "接受成果" }).click();
    expect((await commandResponse).status()).toBe(200);
    await page.reload();
    await expect(results).toContainText(/收尾中|已完成/);
    await expect(results.getByRole("button", { name: "接受成果" })).toHaveCount(0);

    const workerItemId = seed.worker_item_ids.a;
    await page.goto(`/workspaces/${seed.workspace_id}/worker-items/${workerItemId}`);
    await expect(page.locator("#worker-item-title")).toHaveText("Worker A");
    await expect(page.getByRole("region", { name: "执行尝试" })).toContainText("当前执行已绑定");
  });
});
