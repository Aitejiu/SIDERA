import { AxeBuilder } from "@axe-core/playwright";

import { expect, test } from "./fixtures";

test.use({ demoScenario: "mainline" });

test("F18 keyboard and empty task surfaces remain accessible", async ({ interview, page }) => {
  test.info().annotations.push({ type: "action", description: "F18-01" });
  test.info().annotations.push({ type: "action", description: "F18-04" });
  test.info().annotations.push({ type: "action", description: "F18-05" });

  await page.goto(`${interview.manifest.endpoints.web}/workspaces/${interview.manifest.workspace_id}/tasks`);
  await expect(page.locator(".task-list-page").getByRole("link", { name: "新建任务", exact: true })).toBeVisible();
  const emptyAudit = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
  expect(emptyAudit.violations).toEqual([]);

  const newTask = page.locator(".task-list-page").getByRole("link", { name: "新建任务", exact: true });
  await newTask.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { name: "新建任务", exact: true })).toBeVisible();
  const planningAudit = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
  expect(planningAudit.violations).toEqual([]);
});
