import { expect, test } from "@playwright/test";

import { waitForProposalOrPlanningFailure } from "./support/planning-proposal-gate";
import { revisionResultLink } from "./support/revision-result-link";

test("waits for a visible proposal in the current request", async ({ page }) => {
  await page.setContent(`
    <div class="planning-status planning-status--proposal_ready"></div>
    <h2 id="planning-proposal-title">方案待确认</h2>
  `);

  await expect(waitForProposalOrPlanningFailure(page, 1_000)).resolves.toBeUndefined();
});

test("recognizes a ready plan revision page", async ({ page }) => {
  await page.setContent(`
    <span class="plan-revision-status plan-revision-status--proposal_ready"></span>
    <h1 id="plan-revision-title">调整任务计划</h1>
  `);

  await expect(waitForProposalOrPlanningFailure(page, 250)).resolves.toBeUndefined();
});

test("reports a failed plan revision promptly", async ({ page }) => {
  await page.setContent('<span class="plan-revision-status plan-revision-status--failed"></span>');

  await expect(waitForProposalOrPlanningFailure(page, 250))
    .rejects.toThrow("planning_failed_before_proposal");
});

test("waits while planning, then resolves when the current proposal is published", async ({ page }) => {
  await page.setContent(`
    <div class="planning-status planning-status--planning"></div>
    <section class="planning-run-status planning-run-status--running"></section>
  `);
  await page.evaluate(() => {
    window.setTimeout(() => {
      const requestStatus = document.querySelector(".planning-status");
      const runStatus = document.querySelector(".planning-run-status");
      if (requestStatus !== null) {
        requestStatus.className = "planning-status planning-status--proposal_ready";
      }
      if (runStatus !== null) {
        runStatus.className = "planning-run-status planning-run-status--succeeded";
      }
      const proposal = document.createElement("h2");
      proposal.id = "planning-proposal-title";
      proposal.textContent = "方案待确认";
      document.body.append(proposal);
    }, 50);
  });

  await expect(waitForProposalOrPlanningFailure(page, 1_000)).resolves.toBeUndefined();
});

test("reports a failed planning request promptly", async ({ page }) => {
  await page.setContent('<div class="planning-status planning-status--failed"></div>');

  await expect(waitForProposalOrPlanningFailure(page, 1_000))
    .rejects.toThrow("planning_failed_before_proposal");
});

test("reports a blocked request or run promptly", async ({ page }) => {
  await page.setContent('<div class="planning-status planning-status--blocked"></div>');
  await expect(waitForProposalOrPlanningFailure(page, 1_000))
    .rejects.toThrow("planning_blocked_before_proposal");

  await page.setContent(`
    <div class="planning-status planning-status--planning"></div>
    <section class="planning-run-status planning-run-status--blocked"></section>
  `);
  await expect(waitForProposalOrPlanningFailure(page, 1_000))
    .rejects.toThrow("planning_blocked_before_proposal");
});

test("reports a failed run while the request is still planning", async ({ page }) => {
  await page.setContent(`
    <div class="planning-status planning-status--planning"></div>
    <section class="planning-run-status planning-run-status--failed"></section>
  `);

  await expect(waitForProposalOrPlanningFailure(page, 1_000))
    .rejects.toThrow("planning_failed_before_proposal");
});

test("does not accept a stale proposal heading while a revised request is planning", async ({ page }) => {
  await page.setContent(`
    <div class="planning-status planning-status--planning"></div>
    <h2 id="planning-proposal-title">旧方案</h2>
  `);

  await expect(waitForProposalOrPlanningFailure(page, 150)).rejects.toThrow(/150ms/);
});

test("selects the successful revision link after transient results from the same tool", async ({ page }) => {
  const workspaceId = "acf5b341-02f8-44f2-9dad-5776bb7c857b";
  const rootTaskId = "c1ef3d39-bdb7-468b-8d79-10640b0629b4";
  const revisionId = "392f9e57-126f-419a-969e-2da1e6a61eb8";
  const expectedHref = `/workspaces/${workspaceId}/tasks/${rootTaskId}/revisions/${revisionId}`;

  await page.setContent(`
    <article class="tool-result-card"><span>sidera_create_plan_revision</span><p>The SIDERA control plane is temporarily unavailable.</p><a href="/workspaces/${workspaceId}/tasks/${rootTaskId}">查看对象</a></article>
    <article class="tool-result-card"><span>sidera_create_plan_revision</span><p>The SIDERA control plane is temporarily unavailable.</p><a href="/workspaces/${workspaceId}/tasks/${rootTaskId}">查看对象</a></article>
    <article class="tool-result-card"><span>sidera_create_plan_revision</span><p>The SIDERA control plane is temporarily unavailable.</p><a href="/workspaces/${workspaceId}/tasks/${rootTaskId}">查看对象</a></article>
    <article class="tool-result-card"><span>sidera_create_plan_revision</span><p>Revision proposal ready.</p><a href="${expectedHref}">查看对象</a></article>
  `);

  const revisionLink = revisionResultLink(page, rootTaskId);
  await expect(revisionLink).toHaveCount(1);
  await expect(revisionLink).toHaveAttribute("href", expectedHref);
});
