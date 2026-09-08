import { expect, test } from "@playwright/test";

const REAL_RUN = process.env.SIDERA_RUN_REAL_BASIC === "1";
const SECRET_PATTERN = /(?:authorization|token|api[_-]?key|secret|credential)\s*[:=]/i;

test.describe("R6 basic real planning journey", () => {
  test("submits a brief, reviews a Proposal, and confirms one plan", async ({ page, request }) => {
    test.skip(!REAL_RUN, "SIDERA_RUN_REAL_BASIC is not enabled");

    const workspaceId = required("SIDERA_BASIC_WORKSPACE_ID");
    const gatewayURL = required("SIDERA_MAIN_AGENT_GATEWAY_URL").replace(/\/$/, "");
    const marker = `SIDERA-R6-BASIC-${Date.now().toString(36)}`;
    const violations: string[] = [];
    page.on("request", (request) => {
      const headers = Object.keys(request.headers()).join(" ");
      const body = request.postData() ?? "";
      if (SECRET_PATTERN.test(headers) || SECRET_PATTERN.test(body)) {
        violations.push("provider credential crossed browser boundary");
      }
    });

    const health = await request.get(`${gatewayURL}/main-agent/v1/health`);
    expect(health.ok()).toBeTruthy();
    expect((await health.json() as { provider?: string }).provider).toBe("available");

    await page.goto(`/workspaces/${workspaceId}/tasks`);
    await page.getByRole("region", { name: "任务" }).getByRole("link", { name: "新建任务" }).click();
    await page.getByLabel("需求目标").fill(
      `请完成一个小型代码需求，唯一标记为 ${marker}：实现数据校验、文本格式化和集成输出。`,
    );
    await page.getByRole("textbox", { name: "验收条件" }).first().fill(
      "两个实现 WorkerItem 可以并行，集成等待两者完成，并运行仓库 unittest。",
    );
    await page.getByRole("button", { name: "提交需求" }).click();
    await expect(page).toHaveURL(new RegExp(`/workspaces/${escapeRegExp(workspaceId)}/planning/[0-9a-f-]{36}$`));
    await expect(page.locator("#planning-request-title")).toHaveText("规划请求");
    await expect(page.locator(".planning-status")).toContainText(/规划中|方案待确认|等待你的回答/);

    const clarification = page.getByRole("button", { name: "提交澄清答复" });
    if (await clarification.count() > 0) {
      throw new Error("basic real planning unexpectedly requested a clarification");
    }
    await expect(page.getByText(/Proposal V1|方案待确认/)).toBeVisible({ timeout: 120_000 });
    await expect(page.getByRole("button", { name: "确认并创建任务" })).toBeVisible();
    await page.getByRole("button", { name: "确认并创建任务" }).click();
    await expect(page).toHaveURL(new RegExp(`/workspaces/${escapeRegExp(workspaceId)}/tasks/[0-9a-f-]{36}$`));
    await expect(page.locator("#task-detail-title")).toBeVisible();

    expect(violations).toEqual([]);
    const storage = await page.evaluate(() => {
      const values = (source: Storage) => Array.from({ length: source.length }, (_, index) => {
        const key = source.key(index) ?? "";
        return `${key}=${source.getItem(key) ?? ""}`;
      });
      return [...values(window.localStorage), ...values(window.sessionStorage)].join("\n");
    });
    expect(SECRET_PATTERN.test(storage)).toBeFalsy();
  });
});

function required(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is required for the real basic journey`);
  return value;
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
