import { expect, test } from "./fixtures";

test.use({ demoScenario: "mainline" });

test("F06 Trace opens from the selected WorkerItem, filters records, and preserves empty states", async ({
  interview,
  page,
}) => {
  for (const id of ["F06-01", "F06-02", "F06-05"]) {
    test.info().annotations.push({ type: "action", description: id });
  }

  await interview.openChat();
  await interview.sendRequirement("为项目增加 CSV 批量导入功能，手机号为空的策略在执行时向用户确认");
  await interview.openProposal();
  await interview.confirmPlan();
  await expect.poll(async () => (await interview.getFacts()).parallelObserved).toBe(true);
  await interview.releaseGate("parallel");
  await interview.openWorker("report");
  await expect(page.locator("#worker-item-title")).toBeVisible();

  const traceButton = page.getByRole("button", { name: "查看当前 Agent Trace", exact: true });
  await expect(traceButton).toBeEnabled();
  await traceButton.click();
  const dialog = page.getByRole("dialog", { name: "Agent Trace", exact: true });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText(/条已加载记录|暂无轨迹记录/, { exact: false })).toBeVisible();

  const search = dialog.getByRole("searchbox", { name: "搜索已加载记录" });
  await search.fill("不存在的轨迹关键词");
  await expect(dialog.getByText("已加载记录中没有匹配项", { exact: true })).toBeVisible();
  await dialog.getByRole("button", { name: "清除筛选", exact: true }).click();
  await expect(search).toHaveValue("");

  await page.getByRole("button", { name: "关闭 Agent Trace", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.locator("#worker-item-title")).toBeVisible();
});

test("F06 Trace pagination walks older events exactly once", async ({ interview, page }) => {
  test.info().annotations.push({ type: "action", description: "F06-03" });

  await interview.openChat();
  await interview.sendRequirement("为项目增加 CSV 批量导入功能，手机号为空的策略在执行时向用户确认");
  await interview.openProposal();
  await interview.confirmPlan();
  await expect.poll(async () => (await interview.getFacts()).parallelObserved).toBe(true);
  await interview.releaseGate("parallel");
  await interview.openWorker("report");

  const facts = await interview.getFacts();
  const report = facts.workerItems.report;
  expect(facts.rootTaskId).not.toBeNull();
  expect(report?.executionId).not.toBeNull();
  expect(report?.sessionId).not.toBeNull();

  const sequences: number[] = [];
  let beforeSequence: number | undefined;
  for (let pageNumber = 0; pageNumber < 32; pageNumber += 1) {
    const query = new URLSearchParams({
      workspace_id: interview.manifest.workspace_id,
      limit: "1",
    });
    if (beforeSequence !== undefined) query.set("before_sequence", String(beforeSequence));
    const response = await page.request.get(new URL(
      `/api/v1/tasks/${facts.rootTaskId}/executions/${report?.executionId}/sessions/${report?.sessionId}/trace?${query.toString()}`,
      interview.manifest.endpoints.control_plane,
    ).toString());
    expect(response.ok()).toBeTruthy();
    const payload = await response.json() as {
      events?: Array<{ sequence?: number }>;
      previous_before_sequence?: number | null;
    };
    const events = payload.events ?? [];
    for (const event of events) {
      expect(typeof event.sequence).toBe("number");
      sequences.push(event.sequence!);
    }
    const next = payload.previous_before_sequence ?? undefined;
    if (events.length === 0 || next === undefined) break;
    beforeSequence = next;
  }
  expect(sequences.length).toBeGreaterThan(1);
  expect(new Set(sequences).size).toBe(sequences.length);
  expect(sequences).toEqual([...sequences].sort((left, right) => right - left));
});
