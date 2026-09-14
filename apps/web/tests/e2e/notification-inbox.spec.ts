import type { Page } from "@playwright/test";

import { expect, test, WORKSPACE_ID } from "./fixtures";

const ROOT_TASK_ID = "30000000-0000-0000-0000-000000000001";
const SUBTASK_ID = "40000000-0000-0000-0000-000000000001";
const WORKER_ITEM_ID = "50000000-0000-0000-0000-000000000001";
const THREAD_ID = "60000000-0000-0000-0000-000000000001";
const FACT_ID = "70000000-0000-0000-0000-000000000001";
const DELIVERY_ID = "80000000-0000-0000-0000-000000000001";
const streamPattern = `**/api/v1/workspaces/${WORKSPACE_ID}/projection-changes*`;

type InboxFixtureState = ReturnType<typeof createInboxFixtureState>;

function createInboxFixtureState() {
  return {
    recovered: false,
    markedRead: false,
    archived: false,
    reopened: false,
    deliveryStatus: "exhausted" as "exhausted" | "delivered",
    summary: {
      open_count: 1,
      unread_count: 1,
      progress_count: 1,
      channel_issue_count: 1,
      last_change_cursor: 42,
      recipient_type: "workspace_operator",
      recipient_id: "aitejiu",
    },
    actionThread: {
      id: THREAD_ID,
      workspace_id: WORKSPACE_ID,
      recipient_type: "workspace_operator",
      recipient_id: "aitejiu",
      category: "action",
      business_status: "open",
      organization_status: "active",
      priority: "action",
      root_task_id: ROOT_TASK_ID,
      root_task_title: "支付重构",
      subtask_id: SUBTASK_ID,
      subtask_title: "通知网关",
      worker_item_id: WORKER_ITEM_ID,
      worker_item_title: "投递通道",
      title: "渠道需要处理",
      summary: "外部渠道连续失败，需要确认是否重新投递。",
      action: "确认恢复路径",
      topic: "notification.delivery",
      notification_ref: "notification-ref-1",
      latest_fact_id: FACT_ID,
      latest_fact_occurred_at: "2026-09-04T08:30:00Z",
      last_read_fact_id: null,
      unread_count: 1,
      version: 4,
      deep_link: `/workspaces/${WORKSPACE_ID}/worker-items/${WORKER_ITEM_ID}?view=governance`,
      opened_at: "2026-09-04T08:00:00Z",
      resolved_at: null,
      archived_at: null,
      updated_at: "2026-09-04T08:30:00Z",
    },
    progressThread: {
      id: THREAD_ID,
      workspace_id: WORKSPACE_ID,
      recipient_type: "workspace_operator",
      recipient_id: "aitejiu",
      category: "progress",
      business_status: "resolved",
      organization_status: "active",
      priority: "progress",
      root_task_id: ROOT_TASK_ID,
      root_task_title: "支付重构",
      subtask_id: SUBTASK_ID,
      subtask_title: "通知网关",
      worker_item_id: WORKER_ITEM_ID,
      worker_item_title: "投递通道",
      title: "节点已恢复",
      summary: "投递节点已恢复工作。",
      action: "",
      topic: "notification.delivery",
      notification_ref: "notification-ref-1",
      latest_fact_id: FACT_ID,
      latest_fact_occurred_at: "2026-09-04T08:35:00Z",
      last_read_fact_id: FACT_ID,
      unread_count: 0,
      version: 5,
      deep_link: `/workspaces/${WORKSPACE_ID}/worker-items/${WORKER_ITEM_ID}?view=governance`,
      opened_at: "2026-09-04T08:00:00Z",
      resolved_at: "2026-09-04T08:35:00Z",
      archived_at: null,
      updated_at: "2026-09-04T08:35:00Z",
    },
    archivedThread: {
      id: THREAD_ID,
      workspace_id: WORKSPACE_ID,
      recipient_type: "workspace_operator",
      recipient_id: "aitejiu",
      category: "archived",
      business_status: "resolved",
      organization_status: "archived",
      priority: "progress",
      root_task_id: ROOT_TASK_ID,
      root_task_title: "支付重构",
      subtask_id: SUBTASK_ID,
      subtask_title: "通知网关",
      worker_item_id: WORKER_ITEM_ID,
      worker_item_title: "投递通道",
      title: "历史通知",
      summary: "已归档的通知线程。",
      action: "",
      topic: "notification.delivery",
      notification_ref: "notification-ref-1",
      latest_fact_id: FACT_ID,
      latest_fact_occurred_at: "2026-09-04T08:35:00Z",
      last_read_fact_id: FACT_ID,
      unread_count: 0,
      version: 6,
      deep_link: `/workspaces/${WORKSPACE_ID}/worker-items/${WORKER_ITEM_ID}?view=governance`,
      opened_at: "2026-09-04T08:00:00Z",
      resolved_at: "2026-09-04T08:35:00Z",
      archived_at: "2026-09-04T08:40:00Z",
      updated_at: "2026-09-04T08:40:00Z",
    },
  };
}

async function installInboxApi(page: Page, state: InboxFixtureState): Promise<void> {
  await page.route(`**/api/v1/workspaces/${WORKSPACE_ID}/notification-summary`, async (route) => {
    await route.fulfill({ status: 200, contentType: "application/json", json: {
      ...state.summary,
      open_count: state.recovered || state.archived ? 0 : 1,
      progress_count: state.recovered ? 2 : 1,
    } });
  });
  await page.route(`**/api/v1/workspaces/${WORKSPACE_ID}/notification-threads*`, async (route) => {
    const category = new URL(route.request().url()).searchParams.get("category") ?? "action";
    const item = category === "action"
      ? (state.recovered || state.archived ? [] : [state.actionThread])
      : category === "progress"
        ? (state.recovered ? [state.progressThread] : [])
        : (state.archived ? [state.archivedThread] : []);
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      json: { items: item, next_cursor: null, last_change_cursor: state.recovered ? 43 : 42 },
    });
  });
  await page.route(`**/api/v1/workspaces/${WORKSPACE_ID}/notification-threads/${THREAD_ID}`, async (route) => {
    const thread = state.archived
      ? state.archivedThread
      : state.recovered
        ? state.progressThread
        : state.actionThread;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      json: {
        thread,
        facts: {
          items: [{
            id: FACT_ID,
            thread_id: THREAD_ID,
            policy_version: 1,
            kind: "blocked",
            priority: state.recovered ? "progress" : "action",
            transition: state.recovered ? "recovered" : "opened",
            topic: "notification.delivery",
            root_task_id: ROOT_TASK_ID,
            subtask_id: SUBTASK_ID,
            worker_item_id: WORKER_ITEM_ID,
            source_projection_cursor: 10,
            source_projection_type: "worker_item",
            source_resource_id: WORKER_ITEM_ID,
            source_resource_version: 3,
            title: "渠道需要处理",
            summary: "外部渠道连续失败，需要确认是否重新投递。",
            action: "确认恢复路径",
            deep_link: thread.deep_link,
            correlation_id: "90000000-0000-0000-0000-000000000001",
            baseline_import: false,
            occurred_at: "2026-09-04T08:30:00Z",
            created_at: "2026-09-04T08:30:00Z",
          }],
          next_cursor: null,
        },
        deliveries: [{
          delivery_id: DELIVERY_ID,
          route_id: "a0000000-0000-0000-0000-000000000001",
          channel_type: "akashic_private",
          contract_version: 1,
          status: state.deliveryStatus,
          attempts: state.deliveryStatus === "exhausted" ? 10 : 11,
          next_attempt_at: null,
          last_error_code: state.deliveryStatus === "exhausted" ? "provider_unavailable" : null,
          delivered_at: state.deliveryStatus === "delivered" ? "2026-09-04T08:45:00Z" : null,
          exhausted_at: state.deliveryStatus === "exhausted" ? "2026-09-04T09:00:00Z" : null,
          created_at: "2026-09-04T08:30:00Z",
          updated_at: "2026-09-04T09:00:00Z",
        }],
        notification_ref: "notification-ref-1",
        available_commands: state.archived || state.recovered ? [] : ["mark_read"],
        deep_link: thread.deep_link,
        last_change_cursor: state.recovered ? 43 : 42,
      },
    });
  });
  await page.route(`**/api/v1/workspaces/${WORKSPACE_ID}/notification-deliveries/${DELIVERY_ID}/attempts*`, async (route) => {
    await route.fulfill({ status: 200, contentType: "application/json", json: {
      items: [{
        delivery_id: DELIVERY_ID,
        attempt_number: 10,
        status: "failed",
        outcome: "terminal_failure",
        error_code: "provider_unavailable",
        provider_message_ref: null,
        fencing_token: 10,
        next_attempt_at: null,
      }],
      next_cursor: null,
    } });
  });
  await page.route("**/api/v1/modules*", async (route) => {
    await route.fulfill({ status: 200, contentType: "application/json", json: [{
      id: "b0000000-0000-0000-0000-000000000001",
      instance_name: "akashic-private",
      provider: "akashic",
      module_type: "notification_channel",
      config_version: 1,
      definition_lifecycle: "active",
      instance_lifecycle: "active",
    }] });
  });
  await page.route(`**/api/v1/workspaces/${WORKSPACE_ID}/notification-readiness`, async (route) => {
    await route.fulfill({ status: 200, contentType: "application/json", json: {
      component: "notification_gateway",
      as_of: "2026-09-04T08:00:00Z",
      status: "ready",
      checks: [{ name: "worker_running", ready: true, code: "ok", summary: "Notification worker is running" }],
    } });
  });
  await page.route("**/api/v1/commands", async (route) => {
    const body = route.request().postDataJSON() as { command_type?: string };
    if (body.command_type === "MarkNotificationThreadRead") {
      state.markedRead = true;
    }
    if (body.command_type === "RequeueNotificationDelivery") {
      state.deliveryStatus = "delivered";
    }
    if (body.command_type === "ArchiveNotificationThread") {
      state.archived = true;
    }
    await route.fulfill({ status: 200, contentType: "application/json", json: {
      aggregate_id: THREAD_ID,
      aggregate_version: 5,
      command_id: "c0000000-0000-0000-0000-000000000001",
      event_ids: [],
    } });
  });
}

async function openInbox(page: Page, state = createInboxFixtureState()): Promise<InboxFixtureState> {
  await installInboxApi(page, state);
  await page.route(streamPattern, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body: ": waiting for a projection change\n\n",
    });
  });
  await page.goto(`/workspaces/${WORKSPACE_ID}/inbox`);
  await expect(page.getByRole("heading", { name: "信箱", exact: true })).toBeVisible();
  return state;
}

test.describe("notification inbox", () => {
  test("shows the task path, keeps MarkRead open, and returns from a WorkerItem tab", async ({ page }) => {
    const state = await openInbox(page);

    await expect(page.locator("[aria-label='需要处理 1']")).toBeVisible();
    await expect(page.getByText("支付重构 / 通知网关 / 投递通道")).toBeVisible();
    await page.getByRole("button", { name: "展开渠道需要处理" }).click();
    await expect(page.getByRole("region", { name: "渠道需要处理的线程历史" })).toBeVisible();
    await expect(page.getByText("notification-ref-1")).toBeVisible();
    await expect(page.getByText("已耗尽")).toBeVisible();

    await page.getByRole("button", { name: "标记已读" }).click();
    await expect.poll(() => state.markedRead).toBe(true);
    await expect(page.getByRole("button", { name: "收起渠道需要处理" })).toBeVisible();

    await page.getByRole("button", { name: "打开 WorkerItem" }).click();
    await expect(page).toHaveURL(new RegExp(`/workspaces/${WORKSPACE_ID}/worker-items/${WORKER_ITEM_ID}\\?view=governance$`));
    await page.locator(".workspace-tab.is-active").getByRole("button", { name: /^关闭/ }).click();
    await expect(page).toHaveURL(new RegExp(`/workspaces/${WORKSPACE_ID}/inbox$`));
  });

  test("moves the thread to progress after a targeted recovery projection", async ({ page }) => {
    const state = await openInbox(page);
    let streamCalls = 0;
    let recoveryPending = false;
    await page.route(streamPattern, async (route) => {
      streamCalls += 1;
      if (!recoveryPending) {
        await route.fulfill({
          status: 200,
          contentType: "text/event-stream",
          body: ": waiting for recovery\n\n",
        });
        return;
      }
      state.recovered = true;
      await route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: `id: 43\nevent: projection.changed\ndata: ${JSON.stringify({
          workspace_id: WORKSPACE_ID,
          cursor: 43,
          schema_version: 1,
          projection_type: "notification_thread",
          resource_id: THREAD_ID,
          change_type: "notification.thread_updated",
          resource_version: 5,
          occurred_at: "2026-09-04T08:35:00Z",
          correlation_id: "d0000000-0000-0000-0000-000000000001",
        })}\n\n`,
      });
    });

    const previousCalls = streamCalls;
    recoveryPending = true;
    await expect.poll(() => streamCalls).toBeGreaterThan(previousCalls);
    await expect(page.getByText("当前没有需要处理的事项")).toBeVisible();
    await page.getByRole("tab", { name: "进度通报 2" }).click();
    await expect(page.getByRole("button", { name: "展开节点已恢复" })).toBeVisible();
  });

  test("shows provider failure diagnostics before the delivery recovery action", async ({ page }) => {
    const state = await openInbox(page);
    await page.getByRole("button", { name: "展开渠道需要处理" }).click();
    await page.getByRole("button", { name: "查看投递诊断" }).click();
    await expect(page.getByText("第 10 次")).toBeVisible();
    await expect(page.getByText("terminal_failure")).toBeVisible();
    await expect(page.getByText("provider_unavailable")).toBeVisible();

    await page.getByRole("button", { name: "重新投递" }).click();
    await page.getByLabel("重新投递原因").fill("确认渠道已经恢复");
    await page.getByRole("button", { name: "确认重新投递" }).click();
    await expect.poll(() => state.deliveryStatus).toBe("delivered");
    await expect(page.getByText("已送达")).toBeVisible();
  });

  test("requeues an exhausted delivery and exposes the settings readiness surface", async ({ page }) => {
    const state = await openInbox(page);
    await page.getByRole("button", { name: "展开渠道需要处理" }).click();
    await page.getByRole("button", { name: "重新投递" }).click();
    await page.getByLabel("重新投递原因").fill("确认渠道已经恢复");
    await page.getByRole("button", { name: "确认重新投递" }).click();
    await expect.poll(() => state.deliveryStatus).toBe("delivered");
    await expect(page.getByText("已送达")).toBeVisible();

    await page.getByRole("link", { name: "设置" }).click();
    await expect(page.getByRole("heading", { name: "通知设置", exact: true })).toBeVisible();
    await expect(page.getByText("akashic-private")).toBeVisible();
    await expect(page.getByText("已就绪")).toBeVisible();
  });
});
