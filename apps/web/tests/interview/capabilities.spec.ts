import { expect, test } from "./fixtures";

test.use({ demoScenario: "controls" });

test("F16 and F17 capability status comes from the local services", async ({
  interview,
  page,
}) => {
  test.info().annotations.push({ type: "action", description: "F16-01" });
  test.info().annotations.push({ type: "action", description: "F17-01" });

  await page.goto(new URL(
    `/workspaces/${interview.manifest.workspace_id}/capabilities`,
    interview.manifest.endpoints.web,
  ).toString());
  await expect(page.getByRole("heading", { name: "能力", exact: true })).toBeVisible();
  await expect(page.getByText("interview-demo-worker / local_verification", { exact: true })).toBeVisible();
  await expect(page.getByText("git-worktree", { exact: true })).toBeVisible();
  await expect(page.getByText("已连接", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("可配置", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("模拟演示", { exact: true })).toBeVisible();

  await page.route("**/main-agent/v1/health", (route) => route.abort());
  await page.reload();
  await expect(page.getByText("状态未知", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("已连接", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("模拟演示", { exact: true })).toBeVisible();
  await page.unroute("**/main-agent/v1/health");
});
