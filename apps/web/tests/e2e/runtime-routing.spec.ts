import {
  expect,
  IMPLEMENTER_WORKER_ITEM_ID,
  RUNTIME_WORKER_ID,
  runSchedulerOnce,
  test,
  WORKSPACE_ID,
} from "./fixtures";

test.describe("runtime-aware worker routing", () => {
  test.beforeEach(({ seedR4 }) => {
    seedR4("runtime-routing");
  });

  test("pins a queued attempt, surfaces the wait, then refreshes after runtime binding", async ({ page }) => {
    await page.goto(`/workspaces/${WORKSPACE_ID}/worker-items/${IMPLEMENTER_WORKER_ITEM_ID}`);
    await expect(page.locator("#worker-item-title")).toHaveText("实现者");
    await page.getByText("更多任务信息", { exact: true }).click();

    const routing = page.locator(".worker-item-routing");
    await expect(routing.getByText("自动选择", { exact: true })).toBeVisible();
    await routing.getByRole("button", { name: "调整执行节点" }).click();

    const chooser = page.getByRole("region", { name: "执行节点选择器" });
    const runtimeWorker = chooser.getByRole("button", { name: /Runtime Routing Worker/ });
    await expect(runtimeWorker).toContainText("运行配置未配置");
    await runtimeWorker.click();
    await expect(chooser).toContainText("该执行会等待节点完成运行配置");
    await chooser.getByRole("button", { name: "固定到此节点" }).click();
    await expect(routing).toContainText("已固定到 Runtime Routing Worker");

    runSchedulerOnce();
    await expect(routing).toContainText("等待调度");
    await expect(routing).toContainText("运行环境未配置");

    const workerPage = await page.context().newPage();
    await workerPage.goto(`/workspaces/${WORKSPACE_ID}/workers/${RUNTIME_WORKER_ID}`);
    await expect(workerPage.locator("#worker-node-title")).toHaveText("Runtime Routing Worker");
    await workerPage.getByRole("button", { name: "调整运行配置" }).click();
    const dialog = workerPage.getByRole("dialog", { name: "调整运行配置" });
    await expect(dialog).toBeVisible();
    await dialog.getByLabel("Adapter 模块").selectOption({ index: 1 });
    await dialog.getByLabel("工作区提供器").selectOption({ index: 1 });
    await dialog.getByRole("button", { name: "保存运行配置" }).click();
    await expect(workerPage.getByText("配置可用", { exact: true })).toBeVisible();

    runSchedulerOnce();
    await expect(routing).toContainText("已租约");
    await expect(routing).not.toContainText("等待调度");
    await expect(routing).not.toContainText("运行环境未配置");
    await workerPage.close();

    await page.reload();
    await page.getByText("更多任务信息", { exact: true }).click();
    await expect(page.locator(".worker-item-routing")).toContainText("已租约");
    await expect(page.locator(".worker-item-routing")).toContainText("固定到 Runtime Routing Worker");
    await expect(page.locator(".worker-item-routing")).not.toContainText("等待调度");
  });
});
