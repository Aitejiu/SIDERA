import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CommandRequest } from "../assignments/types";
import type { LocalSession } from "../local-session/api";
import { server } from "@/test/server";
import { NativeAttentionRecovery } from "./NativeAttentionRecovery";
import { nativeAttentionDraftKey, type NativeAttentionItem } from "./nativeAttentionAcknowledgement";
import { useNativeAttentionAcknowledgement } from "./useNativeAttentionAcknowledgement";

const WS = "20000000-0000-0000-0000-000000000001", ROOT = "30000000-0000-0000-0000-000000000001";
const SESSION: LocalSession = { session_id: "session", workspace_id: WS, principal_id: "operator", expires_at: "2099-01-01T00:00:00Z" };
const ITEM: NativeAttentionItem = {
  key: "blocked:worker", kind: "blocked", correlation_id: "50000000-0000-0000-0000-000000000001",
  resource_id: ROOT, resource_version: 3, occurred_at: "2026-10-07T00:00:00Z", requires_operator: true,
  task_title: "Task", subtask_key: "worker", subtask_title: "Worker", reason: "Review required",
};

function install() {
  const world = { session: SESSION as LocalSession | null, lost: true, appliedNull: false, legacy: false, rejected: false };
  const posted: CommandRequest[] = [], gets: string[] = [], writes: string[] = [];
  const reply = (command: CommandRequest) => ({ command_id: command.command_id, aggregate_id: ROOT, aggregate_version: 11, event_ids: [], data: {} });
  server.use(
    http.get("/main-agent/v1/health", () => HttpResponse.json({ status: "ok", service: world.legacy ? "sidera-main-agent-gateway" : "sidera-product" })),
    http.get("/product/v1/session", () => world.session ? HttpResponse.json(world.session) : new HttpResponse(null, { status: 401 })),
    http.get(`/api/v1/tasks/${ROOT}`, () => HttpResponse.json({
      id: ROOT, workspace_id: WS, version: 11, title: "Task", description: "Review", acceptance_criteria: [],
      requested_by: "operator", priority: 1, status: "running", created_at: "2026-10-07T00:00:00Z", updated_at: "2026-10-07T00:00:00Z",
    })),
    http.get(`/api/v1/tasks/${ROOT}/graph`, () => HttpResponse.json({ schema_version: 1, nodes: [], edges: [], milestones: [], overview: {
      schema_version: 1, workspace_id: WS, root_task_id: ROOT, title: "Task", goal_summary: "Review", plan_snapshot_id: ROOT,
      generation: 1, acceptance_refs: [], current_milestone_key: null, subtask_count: 0, planned_subtask_count: 0,
      running_subtask_count: 0, completed_subtask_count: 0, failed_subtask_count: 0, handoff_blocked_count: 0,
      handoff_running_count: 0, next_subtask_keys: [], attention_items: [ITEM], last_change_cursor: 1, as_of: "2026-10-07T00:00:00Z",
    } })),
    http.post("/api/v1/commands", async ({ request }) => { const original = await request.json() as CommandRequest; posted.push(original); writes.push(new URL(request.url).pathname); return world.lost ? HttpResponse.error() : HttpResponse.json(reply(original)); }),
    http.get("/api/v1/workspaces/:workspaceId/commands/:commandId", ({ params }) => {
      gets.push(String(params.commandId)); const original = posted[0];
      if (!original) return new HttpResponse(null, { status: 404 });
      return HttpResponse.json({ command_id: original.command_id, command_type: original.command_type, target_type: "task", target_id: ROOT,
        expected_version: 11, payload: original.payload, status: world.rejected ? "rejected" : "applied", result: world.rejected || world.appliedNull ? null : reply(original) });
    }),
    http.all("*", ({ request }) => { if (request.method !== "GET") writes.push(new URL(request.url).pathname); return new HttpResponse(null, { status: 404 }); }),
  );
  return { world, posted, gets, writes };
}

function Harness() {
  const actions = useNativeAttentionAcknowledgement({ workspaceId: WS, rootTaskId: ROOT, onRefresh: async () => undefined });
  return <>
    <NativeAttentionRecovery workspaceId={WS} actions={actions} />
    <button disabled={!actions.canAcknowledge} onClick={() => void actions.acknowledge(ITEM, 11)}>Acknowledge fact</button>
    <button onClick={() => actions.onSessionAuthorized(SESSION)}>Read authority</button>
  </>;
}
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><MemoryRouter><Harness /></MemoryRouter></QueryClientProvider>);
}

describe("native attention recovery with the real owner", () => {
  beforeEach(() => { localStorage.clear(); document.cookie = "sidera_csrf=recovery-csrf; Path=/"; });
  afterEach(() => { vi.restoreAllMocks(); document.cookie = "sidera_csrf=; Max-Age=0; Path=/"; });

  it("exposes current-human entry at idle expiry even with cached Product authority", async () => {
    const h = install(), user = userEvent.setup(); mount();
    await waitFor(() => expect(screen.getByRole("button", { name: "Acknowledge fact" })).toBeEnabled());
    expect(screen.queryByRole("region", { name: "待处理事项确认记录" })).not.toBeInTheDocument();
    const identity = vi.spyOn(crypto, "randomUUID"), save = vi.spyOn(Storage.prototype, "setItem");
    h.world.session = { ...SESSION, expires_at: new Date(Date.now() + 400).toISOString() };
    await user.click(screen.getByRole("button", { name: "Read authority" }));
    expect(await screen.findByRole("button", { name: "本地会话" }, { timeout: 1500 })).toBeVisible();
    expect(screen.getByText("当前操作身份未确认")).toBeVisible();
    expect(screen.getByRole("button", { name: "Acknowledge fact" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "查询原确认" })).not.toBeInTheDocument();
    expect(identity).not.toHaveBeenCalled(); expect(save).not.toHaveBeenCalled(); expect(h.writes).toEqual([]);
    h.world.session = SESSION; await user.click(screen.getByRole("button", { name: "Read authority" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Acknowledge fact" })).toBeEnabled());
    expect(screen.queryByRole("region", { name: "待处理事项确认记录" })).not.toBeInTheDocument();
  });

  it("keeps original-only query visible after response loss and exposes legal mapped acknowledgement", async () => {
    const h = install(), user = userEvent.setup(); mount();
    await waitFor(() => expect(screen.getByRole("button", { name: "Acknowledge fact" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Acknowledge fact" }));
    expect(await screen.findByText("原确认结果未知")).toBeVisible();
    const identity = vi.spyOn(crypto, "randomUUID");
    await user.click(screen.getByRole("button", { name: "查询原确认" }));
    expect(await screen.findByText("已确认处理")).toBeVisible(); expect(screen.getByText("原确认已处理")).toBeVisible();
    expect(h.gets).toEqual([h.posted[0]?.command_id]); expect(h.posted).toHaveLength(1); expect(identity).not.toHaveBeenCalled();
    expect(screen.queryByText(/任务已完成|已开始执行/)).not.toBeInTheDocument();
    expect(h.writes).toEqual(["/api/v1/commands"]);
  });

  it("keeps handled-unmapped original GET and blocks a new acknowledgement", async () => {
    const h = install(); h.world.appliedNull = true; const user = userEvent.setup(); mount();
    await waitFor(() => expect(screen.getByRole("button", { name: "Acknowledge fact" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Acknowledge fact" }));
    await user.click(await screen.findByRole("button", { name: "查询原确认" }));
    expect(await screen.findByText("原确认已处理")).toBeVisible(); expect(screen.getByText("结果尚未映射")).toBeVisible();
    expect(screen.queryByText("已确认处理")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Acknowledge fact" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "查询原确认" })).toBeEnabled(); expect(h.posted).toHaveLength(1);
  });

  it("preserves GET and exposes explicit retry after a post-submit save failure", async () => {
    const h = install(), user = userEvent.setup(); mount();
    await waitFor(() => expect(screen.getByRole("button", { name: "Acknowledge fact" })).toBeEnabled());
    const realSet = Storage.prototype.setItem; let saves = 0;
    const save = vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (this: Storage, key, value) {
      if (++saves > 1) throw new Error("private detail"); return realSet.call(this, key, value);
    });
    await user.click(screen.getByRole("button", { name: "Acknowledge fact" }));
    expect(await screen.findByRole("button", { name: "重试本地记录" })).toBeVisible();
    expect(screen.getByRole("button", { name: "查询原确认" })).toBeEnabled();
    save.mockRestore(); await user.click(screen.getByRole("button", { name: "重试本地记录" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "重试本地记录" })).not.toBeInTheDocument());
    expect(JSON.parse(localStorage.getItem(nativeAttentionDraftKey({ workspaceId: WS, rootTaskId: ROOT, principalId: "operator" }))!).status).toBe("unknown");
    expect(h.posted).toHaveLength(1);
  });

  it("renders explicit journal rejection without calling it an acknowledged fact", async () => {
    const h = install(); h.world.rejected = true; const user = userEvent.setup(); mount();
    await waitFor(() => expect(screen.getByRole("button", { name: "Acknowledge fact" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Acknowledge fact" }));
    await user.click(await screen.findByRole("button", { name: "查询原确认" }));
    expect(await screen.findByText("原确认被拒绝")).toBeVisible(); expect(screen.queryByText("已确认处理")).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Acknowledge fact" })).toBeEnabled());
    expect(h.posted).toHaveLength(1);
  });

  it("has no native recovery or local-session exchange on positive legacy health", async () => {
    const h = install(); h.world.legacy = true; mount();
    await waitFor(() => expect(screen.queryByText("正在确认操作身份")).not.toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "本地会话" })).not.toBeInTheDocument();
    expect(h.writes).toEqual([]);
  });
});
