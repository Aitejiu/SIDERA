import type { Page } from "@playwright/test";

import {
  expect,
  KNOWLEDGE_PENDING_CANDIDATE_ID,
  KNOWLEDGE_STALE_CANDIDATE_ID,
  ROOT_TASK_ID,
  SUBTASK_TASK_ID,
  test,
  WORKSPACE_ID,
} from "./fixtures";

test.describe("Knowledge Service workflow", () => {
  test.beforeEach(({ seedR4, seedKnowledge }) => {
    seedR4("context-knowledge");
    seedKnowledge();
  });

  test("searches history, reviews a task candidate, and refuses stale merge", async ({ page }, testInfo) => {
    await page.goto(`/workspaces/${WORKSPACE_ID}/knowledge`);
    await expect(page.locator("#knowledge-library-title")).toHaveText("知识库");

    const search = page.getByRole("searchbox", { name: "搜索知识" });
    await search.fill("Worker context");
    await search.press("Enter");
    const articleLink = page.getByRole("link", { name: "打开知识页 Worker context reference" });
    await expect(articleLink).toBeVisible();
    await articleLink.click();

    await expect(page.locator("#knowledge-article-title")).toHaveText("Worker context reference");
    await expect(page.getByText("Exact knowledge used by the execution context package.", { exact: true })).toBeVisible();
    await expect(page.getByRole("definition").filter({ hasText: "REV 2" })).toBeVisible();

    await page.getByRole("button", { name: /REV 1/ }).click();
    await expect(page).toHaveURL(new RegExp(`revision_id=${"c0000000-0000-0000-0000-000000000003"}$`));
    await expect(page.getByText("Initial context reference.", { exact: true })).toBeVisible();

    await page.goto(
      `/workspaces/${WORKSPACE_ID}/knowledge/inbox?source_task_id=${ROOT_TASK_ID}&source_subtask_id=${SUBTASK_TASK_ID}`,
    );
    await expect(page.locator("#knowledge-inbox-title")).toHaveText("知识收件箱");
    await expect(page.getByLabel("来源任务筛选")).toHaveValue(ROOT_TASK_ID);
    await expect(page.getByLabel("来源子任务筛选")).toHaveValue(SUBTASK_TASK_ID);

    const pendingCandidateLink = page.getByRole("link", { name: "打开候选 Worker context reference" });
    await expect(pendingCandidateLink).toHaveAttribute(
      "href",
      `/workspaces/${WORKSPACE_ID}/knowledge/candidates/${KNOWLEDGE_PENDING_CANDIDATE_ID}`,
    );
    await pendingCandidateLink.click();
    await expect(page.locator("#knowledge-candidate-title")).toHaveText("Worker context reference");
    await expect(page.getByText("The worker receives an exact immutable context package and records a safe handoff.", { exact: true })).toBeVisible();
    await expect(page.getByRole("heading", { name: "结构化 diff" })).toBeVisible();

    await page.getByRole("button", { name: "接受候选" }).click();
    await page.getByLabel("审核原因").fill("确认 worker 的精确上下文交接");
    await page.getByRole("button", { name: "提交接受" }).click();
    await expect(page).toHaveURL(new RegExp(`/knowledge/pages/${"c0000000-0000-0000-0000-000000000002"}\\?revision_id=[0-9a-f-]{36}$`));
    await expect(page.locator("#knowledge-article-title")).toHaveText("Worker context reference");
    await expect(page.getByText("The worker receives an exact immutable context package and records a safe handoff.", { exact: true })).toBeVisible();

    await page.goto(`/workspaces/${WORKSPACE_ID}/knowledge/candidates/${KNOWLEDGE_STALE_CANDIDATE_ID}`);
    await expect(page.locator("#knowledge-candidate-title")).toHaveText("Stale worker notes");
    await expect(page.getByRole("alert")).toContainText("目标页面已经变化");
    await expect(page.getByRole("button", { name: "接受候选" })).toBeDisabled();
    await expect(page.getByRole("button", { name: "拒绝候选" })).toBeDisabled();

    await assertResponsive(page);
    await page.screenshot({ path: testInfo.outputPath("knowledge-stale-candidate.png"), fullPage: true });
  });

  test("imports Markdown as a pending candidate", async ({ page }, testInfo) => {
    await page.goto(`/workspaces/${WORKSPACE_ID}/knowledge/import`);
    await expect(page.locator("#knowledge-import-title")).toHaveText("导入 Markdown");

    await page.locator('input[type="file"][aria-label="Markdown 文件"]').setInputFiles({
      name: "browser-import.md",
      mimeType: "text/markdown",
      buffer: Buffer.from("# Browser import\n\nThis candidate came from a browser workflow.\n", "utf8"),
    });
    const preview = page.getByRole("complementary", { name: "内容预览" });
    await expect(preview).toContainText("# Browser import");
    await expect(preview).toContainText(
      "This candidate came from a browser workflow.",
    );
    await expect(preview).toContainText("This candidate came from a browser workflow.");

    await page.getByLabel("标签").fill("browser, r5");
    await page.getByRole("button", { name: "提交待审核候选" }).click();
    await expect(page).toHaveURL(new RegExp(`/knowledge/candidates/[0-9a-f-]{36}$`));
    await expect(page.locator("#knowledge-candidate-title")).toHaveText("Browser import");
    await expect(page.getByText("待审核", { exact: true })).toBeVisible();
    await expect(page.getByText("This candidate came from a browser workflow.", { exact: true })).toBeVisible();

    await assertResponsive(page);
    await page.screenshot({ path: testInfo.outputPath("knowledge-import-candidate.png"), fullPage: true });
  });
});

async function assertResponsive(page: Page): Promise<void> {
  const dimensions = await page.evaluate(() => ({
    documentWidth: document.documentElement.scrollWidth,
    viewportWidth: window.innerWidth,
  }));
  expect(dimensions.documentWidth).toBeLessThanOrEqual(dimensions.viewportWidth + 1);

  const focusTarget = page.getByRole("button", { name: "刷新候选" });
  await focusTarget.focus();
  await expect(focusTarget).toBeFocused();
}
