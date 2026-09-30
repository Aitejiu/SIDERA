import type { Page, TestInfo } from "@playwright/test";

import {
  expect,
  test,
} from "./fixtures";

test.describe("R6.3.4 Task First result-input workflows", () => {
  test.describe.configure({ mode: "serial", timeout: 180_000 });

  test("keeps independent root attention causes visible", async ({
    page,
    seedR6ResultInputs,
    prepareR6ResultInputsBrowser,
  }, testInfo) => {
    const seed = seedR6ResultInputs();
    expect(prepareR6ResultInputsBrowser(seed.workspace_id).phase).toBe("browser_ready");

    await page.goto(`/workspaces/${seed.workspace_id}/tasks/${seed.root_task_id}`);
    await expect(page.locator("#task-detail-title")).toBeVisible();

    const pending = page.getByLabel("待决定 Proposal");
    await expect(pending).toBeVisible();
    await expect(pending).toContainText("Review");

    const blocked = page.getByLabel("协同卡点");
    await expect(blocked).toBeVisible();
    await expect(blocked).toContainText("Integration gate");
    await expect(page.getByRole("region", { name: "需要我" })).toContainText("Review");
    await expect(blocked.getByRole("button", { name: "打开子任务：Integration gate" })).toBeVisible();

    await expectNoHorizontalOverflow(page);
    await capture(page, testInfo, "root");
  });

  test("moves through the board, graph, browser history, and semantic tabs", async ({
    page,
    seedR6ResultInputs,
    prepareR6ResultInputsBrowser,
  }, testInfo) => {
    const seed = seedR6ResultInputs();
    prepareR6ResultInputsBrowser(seed.workspace_id);

    await page.goto(`/workspaces/${seed.workspace_id}/tasks/${seed.root_task_id}`);
    const rootTitle = (await page.locator("#task-detail-title").textContent())?.trim();
    expect(rootTitle).toBeTruthy();
    const tabs = page.getByRole("navigation", { name: "已打开的任务" });
    await expect(tabs.getByRole("link", { name: rootTitle ?? "", exact: true })).toBeVisible();

    await page.getByRole("button", { name: "打开子任务：Build" }).click();
    await expect(page.locator("#subtask-title")).toHaveText("Build");
    await expect(page.getByRole("heading", { name: "Worker 看板" })).toBeVisible();
    const subtaskTab = tabs.locator(".workspace-tab").filter({
      has: page.locator(".workspace-tab__detail", { hasText: "子任务" }),
    });
    await expect(subtaskTab).toHaveCount(1);

    await page.getByRole("tab", { name: "Graph", exact: true }).click();
    await expect(page.getByRole("region", { name: "Worker Graph" })).toBeVisible();
    await expect(page.getByTestId("worker-graph-canvas")).toBeVisible();
    await expectNoHorizontalOverflow(page);
    await capture(page, testInfo, "subtask-graph");

    await page.getByRole("tab", { name: "列表", exact: true }).click();
    await page.getByRole("button", { name: "打开 Worker：Worker B", exact: true }).click();
    await expect(page.locator("#worker-item-title")).toHaveText("Worker B");
    const workerTabLabel = `WorkerItem ${seed.source_worker_item_ids[1]?.slice(0, 8) ?? ""}`;
    await expect(tabs.getByRole("link", { name: workerTabLabel, exact: true })).toBeVisible();

    await page.reload();
    await expect(page.locator("#worker-item-title")).toHaveText("Worker B");
    await page.goBack();
    await expect(page.locator("#subtask-title")).toHaveText("Build");
    await page.goForward();
    await expect(page.locator("#worker-item-title")).toHaveText("Worker B");

    await subtaskTab.getByRole("button").click();
    await expect(subtaskTab).toHaveCount(0);
    await expect(page.locator("#worker-item-title")).toHaveText("Worker B");

    await tabs.getByRole("button", { name: `关闭${workerTabLabel}`, exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`/workspaces/${seed.workspace_id}/tasks/${seed.root_task_id}$`));
    await expect(page.locator("#task-detail-title")).toBeVisible();
  });

  test("@closure-intervention approves a fixed selection, opens sources and artifacts, then intervenes", async ({
    page,
    seedR6ResultInputs,
    prepareR6ResultInputsBrowser,
    runR6ResultInputAction,
  }, testInfo) => {
    const seed = seedR6ResultInputs();
    prepareR6ResultInputsBrowser(seed.workspace_id);

    await page.goto(`/workspaces/${seed.workspace_id}/tasks/${seed.root_task_id}`);
    await page
      .getByRole("region", { name: "子任务状态看板" })
      .getByRole("button", { name: "打开子任务：Review", exact: true })
      .click();
    await expect(page.locator("#subtask-title")).toHaveText("Review");

    const approvalWorkspaceResponse = page.waitForResponse((response) => (
      response.request().method() === "GET"
      && response.status() === 200
      && new URL(response.url()).pathname.match(/\/worker-items\/[^/]+\/workspace$/) !== null
    ));
    await page.getByRole("button", { name: "打开 Worker：Approval worker", exact: true }).click();
    const approvalWorkspace = await approvalWorkspaceResponse.then((response) => response.json()) as ApprovalWorkspaceResponse;
    await expect(page.locator("#worker-item-title")).toHaveText("Approval worker");

    const governance = page
      .getByRole("complementary", { name: "WorkerItem 事实" })
      .getByRole("region", { name: "协同放行控制" });
    await expect(governance).toContainText("等待确认");
    await expect(governance.getByRole("button", { name: "批准 Proposal" })).toBeVisible();
    await capture(page, testInfo, "approval");

    const proposal = approvalWorkspace.collaboration?.active_proposal;
    expect(proposal?.proposal_id).toEqual(expect.any(String));
    expect(proposal?.resource_version).toEqual(expect.any(Number));
    expect(proposal?.input_selection_id).toEqual(expect.any(String));
    expect(proposal?.input_selection_hash).toEqual(expect.any(String));

    await governance.getByRole("button", { name: "批准 Proposal" }).click();
    const approveDialog = page.getByRole("dialog", { name: "批准协同放行" });
    await expect(approveDialog).toBeVisible();
    await expect(approveDialog).toBeFocused();
    const approveRequest = page.waitForRequest((request) => (
      request.method() === "POST"
      && new URL(request.url()).pathname === "/api/v1/commands"
      && commandType(request) === "ApproveCollaborationReleaseProposal"
    ));
    await approveDialog.getByRole("button", { name: "批准这次放行" }).click();
    const approveBody = await approveRequest.then((request) => request.postDataJSON()) as CommandRequest;
    expect(approveBody.target_type).toBe("collaboration_release_proposal");
    expect(approveBody.target_id).toBe(proposal?.proposal_id);
    expect(approveBody.expected_version).toBe(proposal?.resource_version);
    expect(approveBody.payload.expected_input_selection_id).toBe(proposal?.input_selection_id);
    expect(approveBody.payload.expected_input_selection_hash).toBe(proposal?.input_selection_hash);
    await expect(approveDialog).toHaveCount(0);
    await expect(governance).toContainText("已放行");

    expect(runR6ResultInputAction(seed.workspace_id, "recover-body").phase).toBe("recovered");
    const summaryWorkspaceResponse = page.waitForResponse((response) => (
      response.request().method() === "GET"
      && response.status() === 200
      && new URL(response.url()).pathname === `/api/v1/workspaces/${seed.workspace_id}/worker-items/${seed.summary_worker_item_id}/workspace`
    ));
    await page.goto(`/workspaces/${seed.workspace_id}/worker-items/${seed.summary_worker_item_id}`);
    await expect(page.locator("#worker-item-title")).toHaveText("Summary worker");
    const summaryWorkspace = await summaryWorkspaceResponse.then((response) => response.json()) as {
      intervention_context?: Record<string, unknown> | null;
    };
    const currentContext = summaryWorkspace.intervention_context;
    expect(currentContext).toBeTruthy();

    const resultInputs = page.getByRole("region", { name: "结果输入" });
    await expect(resultInputs).toBeVisible();
    await expect(resultInputs).toContainText("内容交付");
    await expect(resultInputs.getByRole("button", { name: "查看固定 Artifact：build" }).first()).toBeVisible();

    await resultInputs.getByRole("button", { name: "查看固定 Artifact：build" }).first().click();
    await expect(page.locator("#artifact-page-title")).toHaveText("Artifact 查看器");
    await expect(page).toHaveURL(/\/artifacts\/[0-9a-f-]+$/);
    await page.goBack();
    await expect(page.locator("#worker-item-title")).toHaveText("Summary worker");

    await page.getByRole("region", { name: "结果输入" })
      .getByRole("button", { name: "查看来源 WorkerItem：Worker B" })
      .click();
    await expect(page.locator("#worker-item-title")).toHaveText("Worker B");
    const tabs = page.getByRole("navigation", { name: "已打开的任务" });
    await expect(tabs.getByRole("link", { name: "Worker B", exact: true })).toBeVisible();
    await tabs.getByRole("link", { name: "Summary worker", exact: true }).click();
    await expect(page.locator("#worker-item-title")).toHaveText("Summary worker");

    const traceButton = page.getByRole("button", { name: "查看 Agent Trace" }).first();
    await expect(traceButton).toBeVisible();
    await traceButton.click();
    const traceDialog = page.getByRole("dialog", { name: "Agent Trace" });
    await expect(traceDialog).toBeVisible();
    await expect(traceDialog.getByRole("button", { name: "关闭 Agent Trace" })).toBeFocused();
    await expect(traceDialog.locator(".trace-reader")).toBeVisible();
    await traceDialog.getByRole("button", { name: "关闭 Agent Trace" }).click();
    await expect(traceDialog).toHaveCount(0);

    const interventionText = "保留结果输入并继续汇总";
    const interventionRequest = page.waitForRequest((request) => (
      request.method() === "POST"
      && new URL(request.url()).pathname === "/api/v1/commands"
      && commandType(request) === "SendExecutionInput"
    ));
    await page.getByRole("textbox", { name: "介入这个 Worker" }).fill(interventionText);
    await page.getByRole("button", { name: "发送到当前 Session" }).click();
    const interventionBody = await interventionRequest.then((request) => request.postDataJSON()) as CommandRequest;
    expect(interventionBody.target_type).toBe("execution_session");
    expect(interventionBody.payload.text).toBe(interventionText);
    const submittedContext = interventionBody.payload.context as Record<string, unknown>;
    expect(submittedContext).toMatchObject({
      worker_item_id: seed.summary_worker_item_id,
      session_id: currentContext?.session_id,
      plan_generation: currentContext?.plan_generation,
      context_version: currentContext?.context_version,
      session_version: currentContext?.session_version,
    });
    expect(interventionBody.target_id).toBe(currentContext?.session_id);
    expect(interventionBody.target_id).toBe(submittedContext.session_id);
    await expect(page.getByText(interventionText, { exact: true })).toBeVisible();

    await page.goto(`/workspaces/${seed.workspace_id}/inbox`);
    await expect(page.locator("#notification-inbox-title")).toHaveText("信箱");
    await page.getByRole("tab", { name: /进度通报/ }).click();
    const notification = page.locator(".notification-thread").first();
    await expect(notification).toBeVisible();
    await notification.locator(".notification-thread__toggle").click();
    await expect(notification.getByRole("button", { name: "打开 WorkerItem" })).toBeVisible();
    await notification.getByRole("button", { name: "打开 WorkerItem" }).click();
    await expect.poll(() => new URL(page.url()).pathname).toBe(
      `/workspaces/${seed.workspace_id}/worker-items/${seed.summary_worker_item_id}`,
    );
    await expect(page).toHaveURL(/execution_request=/);
    await expect(page.locator("#worker-item-title")).toHaveText("Summary worker");
    await expectNoHorizontalOverflow(page);
    await capture(page, testInfo, "worker");
  });
});

type ApprovalWorkspaceResponse = {
  collaboration?: {
    active_proposal?: {
      proposal_id: string;
      resource_version: number;
      input_selection_id?: string | null;
      input_selection_hash?: string | null;
    } | null;
  };
};

type CommandRequest = {
  target_type: string;
  target_id: string;
  expected_version: number;
  payload: Record<string, unknown>;
};

function commandType(request: import("@playwright/test").Request): unknown {
  try {
    return request.postDataJSON()?.command_type;
  } catch {
    return undefined;
  }
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

async function capture(page: Page, testInfo: TestInfo, surface: string): Promise<void> {
  await page.screenshot({
    path: testInfo.outputPath(`r6-3-4-${surface}-${testInfo.project.name}.png`),
    fullPage: true,
  });
}
