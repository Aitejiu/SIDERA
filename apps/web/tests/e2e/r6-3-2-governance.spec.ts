import { AxeBuilder } from "@axe-core/playwright";

import {
  expect,
  test,
} from "./fixtures";

const ROOT_TITLE = "Collaboration runtime rollout";
const DOWNSTREAM_TITLE = "Integrate downstream";
const UPSTREAM_A_TITLE = "Prepare upstream A";

test.describe("R6.3.2 human release governance", () => {
  test.describe.configure({ mode: "serial", timeout: 180_000 });

  test("follows root attention to one WorkerItem and approves one Proposal", async ({
    page,
    seedR6Governance,
    runR6GovernanceOnce,
    prepareR6GovernanceBrowser,
    runR6GovernanceAfterPause,
  }, testInfo) => {
    await page.addInitScript(() => localStorage.clear());
    const seed = seedR6Governance();
    expect(runR6GovernanceOnce(seed.workspace_id).run_status).toBe("active");
    expect(prepareR6GovernanceBrowser(seed.workspace_id).run_status).toBe("active");

    await page.goto(`/workspaces/${seed.workspace_id}/tasks/${seed.root_task_id}`);
    await expect(page.locator("#task-detail-title")).toHaveText(ROOT_TITLE);
    await expect(page.getByRole("region", { name: "需要我" })).toContainText(
      "当前没有待确认事项",
    );
    await audit(page, "root task before governance");

    const collaborationSummary = page.getByRole("region", { name: "协同调度" });
    await collaborationSummary
      .getByRole("button", { name: `打开子任务：${DOWNSTREAM_TITLE}` })
      .click();
    await expect(page.locator("#subtask-title")).toHaveText(DOWNSTREAM_TITLE);
    await expect(page.getByRole("heading", { name: "Worker 看板" })).toBeVisible();
    await audit(page, "downstream subtask before governance");

    await page.getByRole("button", { name: `打开 Worker：${DOWNSTREAM_TITLE}` }).click();
    await expect(page.locator("#worker-item-title")).toHaveText(DOWNSTREAM_TITLE);
    const workerFacts = page.getByRole("complementary", { name: "WorkerItem 事实" });
    const governance = workerFacts.getByRole("region", { name: "协同放行控制" });
    await expect(governance).toContainText("自动放行");
    await expect(page.getByRole("heading", { name: "持续工作记录" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "介入这个 Worker" })).toBeVisible();
    await expect(page.getByRole("link", { name: "与主 Agent 讨论调整" })).toBeVisible();

    await governance.getByRole("button", { name: "暂停自动放行" }).click();
    const pauseDialog = page.getByRole("dialog", { name: "暂停自动放行" });
    await expect(pauseDialog).toBeVisible();
    await expect(pauseDialog).toContainText("不会停止当前 Worker 正在运行的 Execution");
    await pauseDialog.getByRole("textbox", { name: "暂停原因" }).fill("浏览器验收前暂停放行");
    await pauseDialog.getByRole("button", { name: "确认暂停自动放行" }).click();
    await expect(pauseDialog).toHaveCount(0);
    await expect(governance).toContainText("已暂停");
    await expect(governance.getByRole("button", { name: "恢复自动放行" })).toBeVisible();

    expect(runR6GovernanceAfterPause(seed.workspace_id).run_status).toBe("active");
    await page.goto(`/workspaces/${seed.workspace_id}/tasks/${seed.root_task_id}`);
    await expect(page.locator("#task-detail-title")).toHaveText(ROOT_TITLE);
    await expect(page.getByRole("region", { name: "需要我" })).toContainText(DOWNSTREAM_TITLE);
    const suggestion = page.getByRole("link", { name: `查看建议：${DOWNSTREAM_TITLE}` });
    await expect(suggestion).toBeVisible();
    await suggestion.click();
    await expect(page.locator("#worker-item-title")).toHaveText(DOWNSTREAM_TITLE);

    const proposalGovernance = page
      .getByRole("complementary", { name: "WorkerItem 事实" })
      .getByRole("region", { name: "协同放行控制" });
    await expect(proposalGovernance).toContainText("等待确认");
    await expect(proposalGovernance).toContainText("Proposal");
    await expect(proposalGovernance.getByRole("button", { name: "批准 Proposal" })).toBeVisible();
    await expect(proposalGovernance.getByRole("button", { name: "驳回 Proposal" })).toBeVisible();
    await audit(page, "WorkerItem with Proposal");

    await proposalGovernance.getByRole("button", { name: "批准 Proposal" }).click();
    const approveDialog = page.getByRole("dialog", { name: "批准协同放行" });
    await expect(approveDialog).toBeVisible();
    await expect(approveDialog).toContainText("不会改变任务或 Worker 排布");
    await approveDialog.getByRole("button", { name: "批准这次放行" }).click();
    await expect(approveDialog).toHaveCount(0);
    await expect(proposalGovernance).toContainText("已放行");
    await expect(proposalGovernance).toContainText("已暂停");
    await expect(proposalGovernance.getByRole("button", { name: "批准 Proposal" })).toHaveCount(0);
    await expect(proposalGovernance.getByRole("button", { name: "驳回 Proposal" })).toHaveCount(0);

    const upstreamLink = workerFacts.getByRole("link", { name: `查看上游：${UPSTREAM_A_TITLE}` });
    await expect(upstreamLink).toBeVisible();
    await upstreamLink.click();
    await expect(page.locator("#subtask-title")).toHaveText(UPSTREAM_A_TITLE);
    await page.getByRole("button", { name: `打开 Worker：${UPSTREAM_A_TITLE}` }).click();
    await expect(page.locator("#worker-item-title")).toHaveText(UPSTREAM_A_TITLE);
    await page.getByRole("button", { name: "查看 Agent Trace" }).first().click();
    const traceDialog = page.getByRole("dialog", { name: "Agent Trace" });
    await expect(traceDialog).toBeVisible();
    await expect(traceDialog).toContainText("pytest");
    await page.keyboard.press("Escape");
    await expect(traceDialog).toHaveCount(0);

    const tabs = page.getByRole("navigation", { name: "已打开的任务" });
    await page.goto(`/workspaces/${seed.workspace_id}/tasks/${seed.root_task_id}`);
    await expect(page.locator("#task-detail-title")).toHaveText(ROOT_TITLE);
    const workerTabLabel = `WorkerItem ${seed.worker_item_ids.downstream.slice(0, 8)}`;
    await expect(tabs.getByRole("link", { name: workerTabLabel })).toBeVisible();
    await tabs.getByRole("button", { name: `关闭${workerTabLabel}` }).click();
    await expect(page).toHaveURL(new RegExp(`/workspaces/${seed.workspace_id}/tasks/${seed.root_task_id}$`));
    await expect(page.locator("#task-detail-title")).toHaveText(ROOT_TITLE);
    await expectNoHorizontalOverflow(page);
    await page.screenshot({
      path: testInfo.outputPath(`r6-3-2-governance-${testInfo.project.name}.png`),
      fullPage: true,
    });
  });

  test("rejects a Proposal, keeps the Hold, and refreshes after resuming", async ({
    page,
    seedR6Governance,
    runR6GovernanceOnce,
    runR6GovernanceAfterPause,
  }) => {
    await page.addInitScript(() => localStorage.clear());
    const seed = seedR6Governance();
    expect(runR6GovernanceOnce(seed.workspace_id).run_status).toBe("active");
    await page.goto(`/workspaces/${seed.workspace_id}/tasks/${seed.root_task_id}/subtasks/subtask-downstream`);
    await page.getByRole("button", { name: `打开 Worker：${DOWNSTREAM_TITLE}` }).click();

    const governance = page
      .getByRole("complementary", { name: "WorkerItem 事实" })
      .getByRole("region", { name: "协同放行控制" });
    await governance.getByRole("button", { name: "暂停自动放行" }).click();
    const pauseDialog = page.getByRole("dialog", { name: "暂停自动放行" });
    await pauseDialog.getByRole("textbox", { name: "暂停原因" }).fill("验证驳回路径");
    await pauseDialog.getByRole("button", { name: "确认暂停自动放行" }).click();
    await expect(governance).toContainText("已暂停");

    expect(runR6GovernanceAfterPause(seed.workspace_id).run_status).toBe("active");
    await page.reload();
    await governance.getByRole("button", { name: "驳回 Proposal" }).click();
    const rejectDialog = page.getByRole("dialog", { name: "驳回协同放行" });
    await rejectDialog.getByRole("textbox", { name: "驳回原因" }).fill("证据仍需补齐");
    await rejectDialog.getByRole("button", { name: "驳回这次放行" }).click();
    await expect(rejectDialog).toHaveCount(0);
    await expect(governance).toContainText("已暂停");
    await expect(governance.getByRole("button", { name: "批准 Proposal" })).toHaveCount(0);
    await expect(governance.getByRole("button", { name: "驳回 Proposal" })).toHaveCount(0);

    await governance.getByRole("button", { name: "恢复自动放行" }).click();
    const resumeDialog = page.getByRole("dialog", { name: "恢复自动放行" });
    await expect(resumeDialog).toContainText("待处理 Proposal 会变为 stale");
    await resumeDialog.getByRole("button", { name: "确认恢复自动放行" }).click();
    await expect(resumeDialog).toHaveCount(0);
    await expect(governance).toContainText("自动放行");

    expect(runR6GovernanceOnce(seed.workspace_id).run_status).toBe("active");
    await page.reload();
    await expect(governance).toContainText("已放行");
    await expectNoHorizontalOverflow(page);
    await expectSeriousAxeFree(page, "reject and resume");
  });
});

async function audit(page: import("@playwright/test").Page, surface: string): Promise<void> {
  await expectSeriousAxeFree(page, surface);
  await expectNoHorizontalOverflow(page);
}

async function expectSeriousAxeFree(
  page: import("@playwright/test").Page,
  surface: string,
): Promise<void> {
  const result = await new AxeBuilder({ page }).analyze();
  const serious = result.violations.filter(
    (violation) => violation.impact === "serious" || violation.impact === "critical",
  );
  expect(serious, `${surface} has serious accessibility violations`).toEqual([]);
}

async function expectNoHorizontalOverflow(page: import("@playwright/test").Page): Promise<void> {
  const metrics = await page.evaluate(() => ({
    body: document.body.scrollWidth,
    document: document.documentElement.scrollWidth,
    viewport: window.innerWidth,
  }));
  expect(metrics.body).toBeLessThanOrEqual(metrics.viewport + 1);
  expect(metrics.document).toBeLessThanOrEqual(metrics.viewport + 1);
}
