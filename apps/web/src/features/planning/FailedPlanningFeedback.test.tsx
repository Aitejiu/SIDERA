import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { buildNativeCommand } from "@/features/assignments/commands";
import type { CommandRequest } from "@/features/assignments/types";
import { workContextByPlanningRequestQueryKey, workContextByRootTaskQueryKey } from "@/features/context/api";
import type { WorkContextDetail } from "@/features/context/types";
import type { LocalSession } from "@/features/local-session/api";
import { server } from "@/test/server";
// @ts-expect-error Explicit TSX avoids the case-insensitive helper basename collision.
import { FailedPlanningFeedback } from "./FailedPlanningFeedback.tsx";
import { failedPlanningFeedbackDraftKey, reviewFailedPlanningFeedback } from "./failedPlanningFeedback.js";
import { loadPlanningDraft, savePlanningDraft } from "./planningConfirmationDraft";
import type { PlanningConfirmationDraft, PlanningDraftScope } from "./planningConfirmationDraft";
import type { PlanningRequest, PlanningRun } from "./types";
import { useNativePlanningCommand } from "./useNativePlanningCommand";

const id = (n: number) => `00000000-0000-0000-0000-${String(n).padStart(12, "0")}`;
const ACTION = "补充要求并重新规划";
const NOW = "2026-10-09T00:00:00Z";
function fixture(kind: "create" | "revision") {
  const scope: PlanningDraftScope = kind === "create"
    ? { kind, workspace_id: id(1), request_id: id(2), principal_id: "human-current" }
    : { kind, workspace_id: id(1), request_id: id(2), principal_id: "human-current", root_task_id: id(3) };
  const session: LocalSession = { session_id: id(4), workspace_id: id(1), principal_id: "human-current", expires_at: "2099-01-01T00:00:00Z" };
  const ref = { workspace_id: id(1), profile_id: id(5), revision: 1 };
  const run: PlanningRun = { id: id(6), workspace_id: id(1), planning_request_id: id(2), version: 3,
    brief_revision: 2, attempt: 2, operation_id: id(7), status: "failed", result_kind: "failure", execution_mode: "native_profile",
    planner_agent_ref: ref, capability_snapshot: null, provider_ref: "agent-run", error_code: "invalid_output",
    native_run: { status: "closed", closed: true, run_id: id(8), response_id: id(9), event_cursor: 3, start_operation_id: id(7), reason_code: null },
    usage: { input_tokens: 1, output_tokens: 1, runtime_seconds: 1, context_bytes: 1, cost_microunits: 1 }, queued_at: NOW, started_at: NOW, finished_at: NOW };
  const request: PlanningRequest = { id: id(2), workspace_id: id(1), kind, status: "failed", version: 12,
    current_brief_revision: 2, current_proposal_revision: null, current_proposal_content_hash: null, current_run_id: id(6), active_run: run,
    planning_profile_id: id(10), planning_profile_version: 3, profile_key: "native-planner", requires_attention: true,
    profile: { id: id(10), workspace_id: id(1), version: 3, profile_key: "native-planner", display_name: "Planner", execution_mode: "native_profile",
      status: "active", planner_agent_ref: ref, capabilities: null, planner_interface_version: "1.0", planner_module_instance_id: null,
      planner_module_config_version: null, planner_provider: null, user_selectable: true, created_at: NOW },
    pending_clarifications: [], usage: run.usage, last_change_cursor: 1, source_ref: null, source_type: "user", created_at: NOW, updated_at: NOW,
    revision_base: kind === "revision" ? { workspace_id: id(1), root_task_id: id(3), plan_snapshot_id: id(11), plan_content_hash: "a".repeat(64),
      plan_generation: 1, root_task_version: 2, schema_version: 1, captured_at: NOW } : null };
  const context: WorkContextDetail = { id: id(12), workspace_id: id(1), planning_request_id: kind === "create" ? id(2) : id(99),
    root_task_id: kind === "create" ? null : id(3), status: "active", version: 4, current_revision_id: id(13), current_revision_number: 2,
    created_at: NOW, updated_at: NOW, timeline: [], next_cursor: null,
    current_revision: { id: id(13), workspace_id: id(1), work_context_id: id(12), revision_number: 2, brief_planning_request_id: id(2),
      brief_revision: 2, content_hash: "a".repeat(64), created_at: NOW, created_by: "human-current", reason: "user_intervention",
      plan_snapshot_id: null, proposal_revision_id: null, snapshot: { schema_version: 1, objective: "Retain history", constraints: [],
        acceptance_criteria: [], requested_outputs: [], accepted_decisions: [], clarification_decisions: [], unresolved_questions: [],
        artifact_refs: [], evidence_refs: [], checkpoint_refs: [], knowledge_refs: [], missing_fact_codes: [] } } };
  return { scope, session, request, runs: [run], context };
}
type Fixture = ReturnType<typeof fixture>;

function mount(input: Fixture, disabledReason?: string, onRefresh: () => Promise<void> = async () => undefined) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  function Owner({ data }: { data: Fixture }) {
    const [draft, setDraft] = useState<PlanningConfirmationDraft | null>(() => {
      const saved = loadPlanningDraft(data.scope);
      return saved.ok ? saved.draft ?? { ...data.scope, schema_version: 1, review: null, choices: [], original: null, status: "idle" } : null;
    });
    const command = useNativePlanningCommand({ scope: data.scope, session: data.session, draft,
      onDraftChange: (next) => { const saved = savePlanningDraft(data.scope, next); if (saved.ok) setDraft(next); return saved.ok; },
      onApplied: async () => undefined });
    return <>
      <FailedPlanningFeedback scope={data.scope} session={data.session} request={data.request} runs={data.runs} command={command}
        disabledReason={disabledReason} onRefresh={onRefresh}
        onSubmit={async (expectedVersion, payload) => {
          await command.submitOriginal(buildNativeCommand({ session: data.session, workspaceId: data.scope.workspace_id,
            commandType: "ReviseFailedPlanningRequest", targetType: "planning_request", targetId: data.scope.request_id, expectedVersion, payload }));
        }} />
      <button type="button" onClick={() => void command.lookupOriginal()}>lookup</button>
      <output aria-label="command status">{command.status}</output>
    </>;
  }
  const view = (data: Fixture) => <QueryClientProvider client={client}><Owner data={data} /></QueryClientProvider>;
  const rendered = render(view(input));
  return { ...rendered, client, update: (next: Fixture) => rendered.rerender(view(next)) };
}

function install(input: Fixture) {
  let contextReads = 0;
  const contextPath = input.scope.kind === "create"
    ? `/api/v1/workspaces/${id(1)}/planning-requests/${id(2)}/work-context`
    : `/api/v1/workspaces/${id(1)}/tasks/${id(3)}/work-context`;
  server.use(http.get(contextPath, () => { contextReads += 1; return HttpResponse.json(input.context); }));
  return { contextPath, contextReads: () => contextReads };
}

async function open(user: ReturnType<typeof userEvent.setup>) {
  await screen.findByRole("button", { name: ACTION });
  await waitFor(() => expect(screen.getByRole("button", { name: ACTION })).toBeEnabled());
  await user.click(screen.getByRole("button", { name: ACTION }));
}

beforeEach(() => { server.resetHandlers(); vi.restoreAllMocks(); window.localStorage.clear(); document.cookie = "sidera_csrf=feedback-test; Path=/"; });

describe.each(["create", "revision"] as const)("failed feedback surface %s", (kind) => {
  it("persists text, retains it on cancel and sends one complete original after a double click", async () => {
    const data = fixture(kind);
    install(data);
    let posts = 0;
    let sent: CommandRequest | null = null;
    server.use(http.post("/api/v1/commands", async ({ request }) => {
      posts += 1; sent = await request.json() as CommandRequest;
      expect(request.headers.get("X-Sidera-CSRF")).toBe("feedback-test");
      expect(loadPlanningDraft(data.scope)).toMatchObject({ ok: true, draft: { status: "pending", original: sent } });
      return HttpResponse.json({ command_id: sent.command_id, aggregate_id: id(2), aggregate_version: 13, event_ids: [] });
    }));
    const user = userEvent.setup(); mount(data); await open(user);
    expect(screen.getByRole("button", { name: ACTION })).toBeDisabled();
    await user.type(screen.getByLabelText("补充要求"), "  Keep four workers  ");
    await user.click(screen.getByRole("button", { name: "取消" }));
    await open(user);
    expect(screen.getByLabelText("补充要求")).toHaveValue("  Keep four workers  ");
    await user.dblClick(screen.getByRole("button", { name: ACTION }));
    await waitFor(() => expect(screen.getByLabelText("command status")).toHaveTextContent("applied"));
    expect(posts).toBe(1);
    expect(sent).toMatchObject({ workspace_id: id(1), actor: { type: "user", id: "human-current" }, command_type: "ReviseFailedPlanningRequest",
      target_type: "planning_request", target_id: id(2), expected_version: 12,
      payload: { failed_planning_run_id: id(6), expected_failed_planning_run_version: 3, expected_work_context_version: 4,
        work_context_revision_id: id(13), work_context_content_hash: "a".repeat(64), feedback: "Keep four workers" } });
    expect(Object.keys(sent!).sort()).toEqual(["actor", "command_id", "command_type", "correlation_id", "expected_version", "idempotency_key", "payload", "target_id", "target_type", "workspace_id"]);
  });

  it("keeps text and the original query-only through uncertainty and remount", async () => {
    const data = fixture(kind); install(data);
    let posts = 0, reads = 0; let sent: CommandRequest | null = null;
    let status = "pending";
    server.use(http.post("/api/v1/commands", async ({ request }) => { posts += 1; sent = await request.json() as CommandRequest; return HttpResponse.error(); }),
      http.get(`/api/v1/workspaces/${id(1)}/commands/:commandId`, ({ params }) => {
        reads += 1; expect(params.commandId).toBe(sent!.command_id);
        return HttpResponse.json({ command_id: sent!.command_id, command_type: "ReviseFailedPlanningRequest", target_type: "planning_request",
          target_id: id(2), expected_version: 12, status, result: null });
      }));
    const user = userEvent.setup(); const first = mount(data); await open(user);
    await user.type(screen.getByLabelText("补充要求"), "Retain history"); await user.click(screen.getByRole("button", { name: ACTION }));
    await waitFor(() => expect(screen.getByLabelText("command status")).toHaveTextContent("unknown"));
    expect(screen.getByLabelText("补充要求")).toHaveAttribute("readonly"); first.unmount();
    mount(data); await user.click(screen.getByRole("button", { name: "lookup" }));
    await waitFor(() => expect(screen.getByLabelText("command status")).toHaveTextContent("pending"));
    status = "unknown"; await user.click(screen.getByRole("button", { name: "lookup" }));
    await waitFor(() => expect(screen.getByLabelText("command status")).toHaveTextContent("unknown"));
    status = "applied"; await user.click(screen.getByRole("button", { name: "lookup" }));
    await waitFor(() => expect(screen.getByLabelText("command status")).toHaveTextContent("applied"));
    expect(posts).toBe(1); expect(reads).toBe(3);
  });

  it("pins old text through Run changes and rebases only after explicit recheck", async () => {
    const data = fixture(kind); install(data); const user = userEvent.setup(); const page = mount(data); await open(user);
    await user.type(screen.getByLabelText("补充要求"), "Retained text");
    const old = reviewFailedPlanningFeedback(data); if (!old.ok) throw new Error(old.reason);
    const oldKey = failedPlanningFeedbackDraftKey(old.basis); const bytes = window.localStorage.getItem(oldKey);
    const next = structuredClone(data); next.request.version += 1; next.runs[0]!.id = id(66); next.runs[0]!.version = 4;
    next.request.current_run_id = id(66); next.request.active_run = next.runs[0]!;
    page.update(next);
    expect(screen.getByLabelText("补充要求")).toHaveValue("Retained text");
    expect(screen.getByRole("button", { name: ACTION })).toBeDisabled();
    const nextReview = reviewFailedPlanningFeedback(next); if (!nextReview.ok) throw new Error(nextReview.reason);
    const nextKey = failedPlanningFeedbackDraftKey(nextReview.basis);
    expect(window.localStorage.getItem(nextKey)).toBeNull();
    await user.click(screen.getByRole("button", { name: "重新核对" }));
    await waitFor(() => expect(screen.getByRole("button", { name: ACTION })).toBeEnabled());
    expect(JSON.parse(window.localStorage.getItem(nextKey)!).feedback).toBe("Retained text");
    expect(window.localStorage.getItem(oldKey)).toBe(bytes);
  });

  it("preserves in-memory text on write failure and requires successful saving", async () => {
    const data = fixture(kind); install(data); const user = userEvent.setup(); mount(data); await open(user);
    const fail = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("quota"); });
    await user.type(screen.getByLabelText("补充要求"), "Long requirements");
    expect(screen.getByLabelText("补充要求")).toHaveValue("Long requirements");
    expect(screen.getByRole("alert")).toHaveTextContent("无法保存补充要求草稿");
    expect(screen.getByRole("button", { name: ACTION })).toBeDisabled();
    fail.mockRestore(); await user.click(screen.getByRole("button", { name: "重新核对" }));
    await waitFor(() => expect(screen.getByRole("button", { name: ACTION })).toBeEnabled());
  });

  it("does not read context for expired or mismatched sessions or ordinary request branches", async () => {
    const data = fixture(kind); const reads = install(data);
    data.session.expires_at = "2000-01-01T00:00:00Z"; const page = mount(data);
    expect(screen.getByRole("button", { name: ACTION })).toBeDisabled(); expect(reads.contextReads()).toBe(0);
    page.unmount(); data.session.expires_at = "2099-01-01T00:00:00Z"; data.session.principal_id = "foreign";
    const foreign = mount(data); expect(screen.getByRole("button", { name: ACTION })).toBeDisabled(); expect(reads.contextReads()).toBe(0);
    foreign.unmount(); data.session.principal_id = "human-current"; data.request.status = "proposal_ready";
    mount(data); expect(reads.contextReads()).toBe(0);
  });

  it("keeps stale or unavailable context disabled with an explicit read recovery", async () => {
    const data = fixture(kind); const reads = install(data);
    server.use(http.get(reads.contextPath, () => HttpResponse.json({ error: { code: "unavailable", message: "unavailable" } }, { status: 503 })));
    const user = userEvent.setup(); mount(data);
    expect(await screen.findByRole("alert")).toHaveTextContent("无法读取需求上下文");
    expect(screen.getByRole("button", { name: ACTION })).toBeDisabled();
    server.use(http.get(reads.contextPath, () => HttpResponse.json(data.context)));
    await user.click(screen.getByRole("button", { name: "重新读取" }));
    await waitFor(() => expect(screen.getByRole("button", { name: ACTION })).toBeEnabled());
  });

  it("keeps editor state separate when the principal changes", async () => {
    const data = fixture(kind); install(data); const user = userEvent.setup(); const page = mount(data); await open(user);
    await user.type(screen.getByLabelText("补充要求"), "Private requirements");
    const next = structuredClone(data); next.scope.principal_id = "other-human"; next.session.principal_id = "other-human";
    page.update(next); await open(user);
    expect(screen.getByLabelText("补充要求")).toHaveValue("");
    expect([...Object.keys(window.localStorage)].some((key) => window.localStorage.getItem(key)?.includes("Private requirements"))).toBe(true);
  });

  it("refuses a corrupt text draft without overwriting it", async () => {
    const data = fixture(kind); install(data); const reviewed = reviewFailedPlanningFeedback(data); if (!reviewed.ok) throw new Error(reviewed.reason);
    const key = failedPlanningFeedbackDraftKey(reviewed.basis); window.localStorage.setItem(key, "{");
    const user = userEvent.setup(); mount(data); await open(user);
    expect(screen.getByRole("alert")).toHaveTextContent("无法读取补充要求草稿");
    expect(screen.getByLabelText("补充要求")).toHaveAttribute("readonly");
    expect(window.localStorage.getItem(key)).toBe("{");
  });

  it("keeps refetched versions stale without automatically writing a new text key", async () => {
    const data = fixture(kind); install(data); const user = userEvent.setup(); const page = mount(data); await open(user);
    await user.type(screen.getByLabelText("补充要求"), "Retain text");
    const contextKey = kind === "create" ? workContextByPlanningRequestQueryKey(id(1), id(2)) : workContextByRootTaskQueryKey(id(1), id(3));
    await act(async () => page.client.setQueryData(contextKey, { ...data.context, version: 5 }));
    expect(screen.getByLabelText("补充要求")).toHaveValue("Retain text");
    await waitFor(() => expect(screen.getByRole("button", { name: ACTION })).toBeDisabled());
  });

  it("blocks retained text after a failed fact refresh until a successful explicit recheck", async () => {
    const data = fixture(kind); install(data); let unavailable = true;
    const refresh = vi.fn(async () => { if (unavailable) throw new Error("unavailable"); });
    const user = userEvent.setup(); mount(data, undefined, refresh); await open(user);
    await user.type(screen.getByLabelText("补充要求"), "Retain text");
    await user.click(screen.getByRole("button", { name: "重新核对" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("无法读取规划事实");
    expect(screen.getByRole("button", { name: ACTION })).toBeDisabled();
    expect(screen.getByLabelText("补充要求")).toHaveValue("Retain text");
    unavailable = false; await user.click(screen.getByRole("button", { name: "重新核对" }));
    await waitFor(() => expect(screen.getByRole("button", { name: ACTION })).toBeEnabled());
  });

  it("never copies old text after a delayed recheck crosses a principal boundary", async () => {
    const data = fixture(kind); install(data); let release!: () => void;
    const refresh = vi.fn(() => new Promise<void>((resolve) => { release = resolve; }));
    const user = userEvent.setup(); const page = mount(data, undefined, refresh); await open(user);
    await user.type(screen.getByLabelText("补充要求"), "Private old text");
    const before = { ...window.localStorage };
    await user.click(screen.getByRole("button", { name: "重新核对" }));
    expect(refresh).toHaveBeenCalledOnce();
    const next = structuredClone(data); next.scope.principal_id = "other-human"; next.session.principal_id = "other-human";
    page.update(next); await open(user); expect(screen.getByLabelText("补充要求")).toHaveValue("");
    await act(async () => release());
    expect(screen.getByLabelText("补充要求")).toHaveValue("");
    expect({ ...window.localStorage }).toEqual(before);
  });

  it.each(["missing", "unclosed", "wrong-current", "conflicting-active"] as const)("refuses a %s failed run without POST", async (failure) => {
    const data = fixture(kind); install(data); let posts = 0;
    if (failure === "missing") data.runs = [];
    if (failure === "unclosed") data.runs[0]!.native_run!.closed = false;
    if (failure === "wrong-current") data.request.current_run_id = id(77);
    if (failure === "conflicting-active") data.request.active_run = { ...data.runs[0]!, version: 99 };
    server.use(http.post("/api/v1/commands", () => { posts += 1; return HttpResponse.json({}); }));
    mount(data); await waitFor(() => expect(within(screen.getByRole("region", { name: "补充规划要求" })).getByRole("status"))
      .not.toHaveTextContent("正在读取需求上下文"));
    expect(screen.getByRole("button", { name: ACTION })).toBeDisabled(); expect(posts).toBe(0);
  });
});
