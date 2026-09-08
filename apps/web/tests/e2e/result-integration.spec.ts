import type { Page } from "@playwright/test";

import { expect, test } from "./fixtures";

test.describe("R6 result integration control room", () => {
  test.describe.configure({ mode: "serial", timeout: 180_000 });

  test("shows the concrete subtask blocker and current result evidence", async ({
    page,
    seedResultIntegration,
    prepareResultIntegrationBrowser,
  }) => {
    const seed = seedResultIntegration();
    expect(prepareResultIntegrationBrowser(seed.workspace_id).phase).toBe("candidate_failed");

    await page.goto(`/workspaces/${seed.workspace_id}/tasks`);
    const taskLink = page.getByRole("link", { name: "打开任务 Runtime-enabled plan" });
    await expect(taskLink).toBeVisible();
    await taskLink.click();
    await expect(page.locator("#task-detail-title")).toHaveText("Runtime-enabled plan");
    await expect(page.getByRole("button", { name: "打开子任务：Review" })).toBeVisible();

    await page.getByRole("tab", { name: "成果" }).click();
    const results = page.getByRole("region", { name: "成果验收" });
    await expect(results).toContainText("等待用户验收");
    await expect(results).toContainText("Expired credentials are rejected");
    await expect(results).toContainText("检查未通过");
    await expect(results.getByRole("combobox", { name: "Expired credentials are rejected 的决定" })).toBeVisible();
    await expect(results.getByRole("link", { name: "查看 / 下载" })).toHaveCount(4);

    await expectNoHorizontalOverflow(page);
    await expect(page).toHaveScreenshot("result-integration-root.png", {
      fullPage: true,
      animations: "disabled",
      mask: [page.locator(".mono-label"), page.locator(".notification-center")],
    });
  });

  test("keeps the original WorkerItem trace and accepts a repaired candidate", async ({
    page,
    seedResultIntegration,
    prepareResultIntegrationBrowser,
    advanceResultIntegration,
  }) => {
    const seed = seedResultIntegration();
    expect(prepareResultIntegrationBrowser(seed.workspace_id).phase).toBe("candidate_failed");

    const repaired = advanceResultIntegration(seed.workspace_id, "repair");
    expect(repaired.phase).toBe("candidate_passed");
    expect(repaired.current_candidate_id).not.toBe(seed.current_candidate_id);

    await page.goto(resultUrl(seed));
    const results = page.getByRole("region", { name: "成果验收" });
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
    await expect(results).toContainText("收尾中");
    await expect(results.getByRole("button", { name: "接受成果" })).toHaveCount(0);

    await page.goto(workerUrl(seed, "a"));
    await expect(page.locator("#worker-item-title")).toHaveText("Worker A");
    const executionFacts = page.getByRole("region", { name: "执行尝试" });
    await expect(executionFacts).toContainText("当前执行已绑定");
    const traceButton = page.getByRole("button", { name: "查看 Agent Trace" }).first();
    await expect(traceButton).toBeVisible();
    await traceButton.click();
    const traceDialog = page.getByRole("dialog", { name: "Agent Trace" });
    await expect(traceDialog).toBeVisible();
    await expect(traceDialog.getByRole("complementary", { name: "Trace 诊断信息" })).toBeVisible();
    await traceDialog.getByRole("button", { name: "关闭 Agent Trace" }).click();
    await expect(traceDialog).toHaveCount(0);

    const artifactLink = page.getByRole("link", { name: "diff 可查看" }).first();
    await expect(artifactLink).toBeVisible();
    await artifactLink.click();
    await expect(page.locator("#artifact-page-title")).toHaveText("Artifact 查看器");
    await page.goBack();
    await expect(page.locator("#worker-item-title")).toHaveText("Worker A");

    await expectNoHorizontalOverflow(page);
    await expect(page).toHaveScreenshot("result-integration-worker.png", {
      fullPage: true,
      animations: "disabled",
      mask: [page.locator(".mono-label"), page.locator(".notification-center")],
    });
  });

  test("keeps an acceptance draft after a real stale-version response", async ({
    page,
    seedResultIntegration,
    prepareResultIntegrationBrowser,
    advanceResultIntegration,
  }) => {
    const seed = seedResultIntegration();
    expect(prepareResultIntegrationBrowser(seed.workspace_id).phase).toBe("candidate_failed");
    await page.goto(resultUrl(seed));

    const results = page.getByRole("region", { name: "成果验收" });
    const decision = results.getByRole("combobox", { name: "Expired credentials are rejected 的决定" });
    await decision.selectOption("exception_accepted");
    const draftReason = "仅用于演示候选变化后的人工依据";
    await results.getByLabel("例外原因").fill(draftReason);

    await page.route("**/acceptance-basis", async (route) => {
      const response = await route.fetch();
      advanceResultIntegration(seed.workspace_id, "repair");
      await route.fulfill({ response });
    });
    const staleResponse = page.waitForResponse((response) => (
      response.request().method() === "POST"
      && new URL(response.url()).pathname === "/api/v1/commands"
      && response.status() === 409
    ));
    await results.getByRole("button", { name: "接受成果" }).click();
    await staleResponse;
    await expect(page.getByRole("alert")).toContainText("成果已变化，请重新审阅");
    await expect(results.getByLabel("例外原因")).toHaveValue(draftReason);

    await page.unroute("**/acceptance-basis");
    await page.reload();
    await expect(page.getByRole("region", { name: "成果验收" })).toContainText("检查已通过");
    await expect(page.getByRole("region", { name: "成果验收" }).getByRole("button", { name: "接受成果" })).toHaveCount(1);
    await expectNoHorizontalOverflow(page);
  });

  test("returns to the owning tab after closing nested task views", async ({
    page,
    seedResultIntegration,
    prepareResultIntegrationBrowser,
  }) => {
    const seed = seedResultIntegration();
    expect(prepareResultIntegrationBrowser(seed.workspace_id).phase).toBe("candidate_failed");

    await page.goto(`/workspaces/${seed.workspace_id}/tasks`);
    await page.getByRole("link", { name: "打开任务 Runtime-enabled plan" }).click();
    await expect(page.locator("#task-detail-title")).toHaveText("Runtime-enabled plan");
    await page.goto(workerUrl(seed, "a"));
    await expect(page.locator("#worker-item-title")).toHaveText("Worker A");

    const tabs = page.getByRole("navigation", { name: "已打开的任务" });
    await tabs.getByRole("button", { name: /关闭WorkerItem/ }).click();
    await expect(page).toHaveURL(new RegExp(`/workspaces/${seed.workspace_id}/tasks/${seed.root_task_id}$`));
    await expect(page.locator("#task-detail-title")).toHaveText("Runtime-enabled plan");

    await tabs.getByRole("button", { name: "关闭Runtime-enabled plan" }).click();
    await expect(page).toHaveURL(new RegExp(`/workspaces/${seed.workspace_id}/tasks$`));
    await expect(page.getByRole("heading", { name: "任务", exact: true })).toBeVisible();
    await expectNoHorizontalOverflow(page);
  });
});

function resultUrl(seed: { workspace_id: string; root_task_id: string }): string {
  return `/workspaces/${seed.workspace_id}/tasks/${seed.root_task_id}?view=results`;
}

function workerUrl(
  seed: { workspace_id: string; worker_item_ids: Record<string, string> },
  key: string,
): string {
  const workerItemId = seed.worker_item_ids[key];
  if (workerItemId === undefined) throw new Error(`worker item ${key} is missing`);
  return `/workspaces/${seed.workspace_id}/worker-items/${workerItemId}`;
}

async function expectNoHorizontalOverflow(page: Page): Promise<void> {
  const dimensions = await page.evaluate(() => ({
    body: document.body.scrollWidth,
    document: document.documentElement.scrollWidth,
    viewport: document.documentElement.clientWidth,
  }));
  expect(dimensions.body).toBeLessThanOrEqual(dimensions.viewport + 1);
  expect(dimensions.document).toBeLessThanOrEqual(dimensions.viewport + 1);
}
