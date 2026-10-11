import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { createElement, type PropsWithChildren } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CommandRequest } from "@/features/assignments/types";
import type { LocalSession } from "@/features/local-session/api";
import * as client from "@/shared/api/client";
import { server } from "@/test/server";

import type { TaskResultOverview } from "./api";
import { loadNativeAcceptanceDraft, nativeAcceptanceDraftKey } from "./nativeResultAcceptance";
import { completeTaskInput, RESULT_CANDIDATE_ID, RESULT_ROOT_TASK_ID, RESULT_WORKSPACE_ID, resultAction, resultView } from "./testFixtures";
import { useNativeResultAcceptance } from "./useNativeResultAcceptance";

const rootB = "71000000-0000-0000-0000-000000000099";
const base = `/api/v1/workspaces/${RESULT_WORKSPACE_ID}/tasks/${RESULT_ROOT_TASK_ID}/results`;
const scope = { workspace_id: RESULT_WORKSPACE_ID, root_task_id: RESULT_ROOT_TASK_ID, principal_id: "human-current" };
const session: LocalSession = { session_id: "current-human-session", workspace_id: RESULT_WORKSPACE_ID, principal_id: scope.principal_id, expires_at: "2099-01-01T00:00:00Z" };
const criteria = completeTaskInput().payload.criteria;

function deferred() {
  let release!: () => void;
  const promise = new Promise<void>(resolve => { release = resolve; });
  return { promise, release };
}

function acceptanceReceipt(command: CommandRequest) {
  return {
    command_id: command.command_id, aggregate_id: RESULT_ROOT_TASK_ID, aggregate_version: 11, event_ids: [],
    data: {
      status: "accepted", acceptance_id: "71000000-0000-0000-0000-000000000040", candidate_id: RESULT_CANDIDATE_ID,
      result_version: 7, task_status: "waiting", apply_operation_id: null, apply_status: null,
      finalization_operation_id: "71000000-0000-0000-0000-000000000041", result_stage: "finalizing",
    },
  };
}

function routes() {
  const observed = {
    basis: [] as unknown[], commands: [] as CommandRequest[], lookups: [] as string[],
    csrf: [] as (string | null)[], savedAtPost: [] as unknown[], results: 0,
  };
  server.use(
    http.get("/main-agent/v1/health", () => HttpResponse.json({ status: "ok", service: "sidera-product" })),
    http.get("/product/v1/session", () => HttpResponse.json(session)),
    http.get(base, () => { observed.results += 1; return HttpResponse.json(resultView()); }),
    http.get(`/api/v1/workspaces/${RESULT_WORKSPACE_ID}/tasks/${rootB}/results`, () => HttpResponse.json(resultView({ root_task_id: rootB, available_actions: [{ ...resultAction(), target_id: rootB }] }))),
    http.post(`${base}/${RESULT_CANDIDATE_ID}/acceptance-basis`, async ({ request }) => {
      observed.basis.push(await request.json());
      return HttpResponse.json({ candidate_id: RESULT_CANDIDATE_ID, candidate_hash: "c".repeat(64), expected_result_version: 7, acceptance_basis_hash: "f".repeat(64) });
    }),
    http.post("/api/v1/commands", async ({ request }) => {
      const command = await request.json() as CommandRequest;
      observed.commands.push(command);
      observed.csrf.push(request.headers.get("X-Sidera-CSRF"));
      observed.savedAtPost.push(loadNativeAcceptanceDraft(scope));
      return HttpResponse.json(acceptanceReceipt(command));
    }),
    http.get(`/api/v1/workspaces/${RESULT_WORKSPACE_ID}/commands/:commandId`, ({ params }) => {
      observed.lookups.push(String(params.commandId));
      const command = observed.commands[0]!;
      return HttpResponse.json({ command_id: command.command_id, command_type: "CompleteTask", target_type: "task", target_id: RESULT_ROOT_TASK_ID, expected_version: 11, payload: command.payload, status: "applied", result: acceptanceReceipt(command) });
    }),
  );
  return observed;
}

function mount(view: TaskResultOverview | undefined = resultView(), rootTaskId = RESULT_ROOT_TASK_ID, refresh: () => Promise<void> = async () => {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const wrapper = ({ children }: PropsWithChildren) => createElement(QueryClientProvider, { client: queryClient }, children);
  const initialProps: { view: TaskResultOverview | undefined; rootTaskId: string } = { view, rootTaskId };
  const rendered = renderHook((props: { view: TaskResultOverview | undefined; rootTaskId: string }) => useNativeResultAcceptance({ workspaceId: RESULT_WORKSPACE_ID, rootTaskId: props.rootTaskId, view: props.view, onRefresh: refresh }), { wrapper, initialProps });
  return { ...rendered, queryClient };
}

async function ready(hook: ReturnType<typeof mount>) {
  await waitFor(() => expect(hook.result.current.authorized).toBe(true));
  await waitFor(() => expect(hook.result.current.canAccept).toBe(true));
}

beforeEach(() => {
  localStorage.clear();
  document.cookie = "sidera_csrf=acceptance-test-csrf; path=/";
});
afterEach(() => { vi.restoreAllMocks(); document.cookie = "sidera_csrf=; Max-Age=0; path=/"; });

describe("native acceptance original lifecycle", () => {
  it("renders and hydrates without allocating an identity or reading a basis", async () => {
    const observed = routes();
    const identity = vi.spyOn(client, "createCommandIdentity");
    const hook = mount();
    await ready(hook);
    expect(hook.result.current.authority).toMatchObject({ kind: "product", session });
    expect(identity).not.toHaveBeenCalled();
    expect(observed.basis).toHaveLength(0);
    expect(observed.commands).toHaveLength(0);
    expect(localStorage.length).toBe(0);
  });

  it("locks same-tick submit before awaits, freezes decisions, and saves the original before its sole CSRF/user POST", async () => {
    const observed = routes();
    const hook = mount();
    await ready(hook);
    const choices = [{ ...criteria[0]!, reason: "  reviewed reason  " }];
    await act(async () => {
      const first = hook.result.current.submit(choices);
      const second = hook.result.current.submit(choices);
      choices[0]!.reason = "changed while awaiting";
      await Promise.all([first, second]);
    });
    expect(observed.basis).toEqual([{ criteria: [{ criterion_key: "expired-credentials", decision: "accept_passed", reason: "reviewed reason" }] }]);
    expect(observed.commands).toHaveLength(1);
    expect(observed.commands[0]).toMatchObject({ actor: { type: "user", id: "human-current" }, target_id: RESULT_ROOT_TASK_ID, expected_version: 11, payload: { expected_result_version: 7, acceptance_basis_hash: "f".repeat(64), criteria: [{ criterion_key: "expired-credentials", decision: "accept_passed", reason: "reviewed reason" }] } });
    expect(observed.savedAtPost).toEqual([{ ok: true, draft: expect.objectContaining({ original: observed.commands[0], status: "pending" }) }]);
    expect(observed.csrf).toEqual(["acceptance-test-csrf"]);
    expect(hook.result.current.status).toBe("applied");
    expect(hook.result.current.mappingAvailable).toBe(true);
    expect(hook.result.current.canAccept).toBe(false);
  });

  it("keeps a lost original unchanged after result/action removal and recovers it only by original GET", async () => {
    const observed = routes();
    server.use(http.post("/api/v1/commands", async ({ request }) => { observed.commands.push(await request.json() as CommandRequest); return HttpResponse.error(); }));
    const hook = mount();
    await ready(hook);
    await act(() => hook.result.current.submit(criteria));
    const original = hook.result.current.original;
    expect(hook.result.current.status).toBe("unknown");
    hook.rerender({ view: resultView({ result_version: 8, result_stage: "finalizing", available_actions: [] }), rootTaskId: RESULT_ROOT_TASK_ID });
    await act(() => hook.result.current.submit(criteria));
    const identity = vi.spyOn(client, "createCommandIdentity");
    await act(() => hook.result.current.lookupOriginal());
    expect(hook.result.current.original).toEqual(original);
    expect(hook.result.current.status).toBe("applied");
    expect(observed.lookups).toEqual([original!.command_id]);
    expect(observed.commands).toHaveLength(1);
    expect(observed.basis).toHaveLength(1);
    expect(identity).not.toHaveBeenCalled();
  });

  it("recovers after remount even without a current result", async () => {
    const observed = routes();
    server.use(http.post("/api/v1/commands", async ({ request }) => { observed.commands.push(await request.json() as CommandRequest); return HttpResponse.error(); }));
    const first = mount();
    await ready(first);
    await act(() => first.result.current.submit(criteria));
    const original = first.result.current.original;
    first.unmount();
    const identity = vi.spyOn(client, "createCommandIdentity");
    const second = mount(undefined);
    second.rerender({ view: undefined, rootTaskId: RESULT_ROOT_TASK_ID });
    await waitFor(() => expect(second.result.current.original).toEqual(original));
    await act(() => second.result.current.lookupOriginal());
    expect(observed.lookups).toEqual([original!.command_id]);
    expect(observed.commands).toHaveLength(1);
    expect(observed.basis).toHaveLength(1);
    expect(identity).not.toHaveBeenCalled();
  });

  it("retains known application after a refresh failure", async () => {
    routes();
    const hook = mount(resultView(), RESULT_ROOT_TASK_ID, async () => { throw new Error("private refresh details"); });
    await ready(hook);
    await act(() => hook.result.current.submit(criteria));
    expect(hook.result.current.status).toBe("applied");
    expect(hook.result.current.error).not.toContain("private");
    expect(loadNativeAcceptanceDraft(scope)).toMatchObject({ ok: true, draft: { status: "applied" } });
  });

  it("handles explicit correlated applied journal with result null without a new POST", async () => {
    const observed = routes();
    server.use(http.post("/api/v1/commands", async ({ request }) => { observed.commands.push(await request.json() as CommandRequest); return HttpResponse.error(); }));
    const hook = mount();
    await ready(hook);
    await act(() => hook.result.current.submit(criteria));
    server.use(http.get(`/api/v1/workspaces/${RESULT_WORKSPACE_ID}/commands/:commandId`, () => HttpResponse.json({ command_id: observed.commands[0]!.command_id, command_type: "CompleteTask", target_type: "task", target_id: RESULT_ROOT_TASK_ID, expected_version: 11, status: "applied", result: null })));
    await act(() => hook.result.current.lookupOriginal());
    expect(hook.result.current.status).toBe("applied");
    expect(hook.result.current.mappingAvailable).toBe(false);
    await act(() => hook.result.current.submit(criteria));
    expect(observed.commands).toHaveLength(1);
    expect(observed.basis).toHaveLength(1);
  });

  it("only permits a fresh reviewed intent after explicit journal rejection, preserving that rejection across refresh failure", async () => {
    const observed = routes();
    server.use(http.post("/api/v1/commands", async ({ request }) => { observed.commands.push(await request.json() as CommandRequest); return HttpResponse.json({ error: { code: "acceptance_stale", message: "private HTTP failure" } }, { status: 409 }); }));
    const hook = mount(resultView(), RESULT_ROOT_TASK_ID, async () => { throw new Error("private refresh"); });
    await ready(hook);
    await act(() => hook.result.current.submit(criteria));
    expect(hook.result.current.status).toBe("unknown");
    expect(hook.result.current.error).not.toContain("private");
    const original = hook.result.current.original;
    server.use(http.get(`/api/v1/workspaces/${RESULT_WORKSPACE_ID}/commands/:commandId`, () => HttpResponse.json({ command_id: original!.command_id, command_type: "CompleteTask", target_type: "task", target_id: RESULT_ROOT_TASK_ID, expected_version: 11, status: "rejected", error_code: "private-rejection-details", result: null })));
    await act(() => hook.result.current.lookupOriginal());
    expect(hook.result.current.status).toBe("rejected");
    expect(hook.result.current.error).not.toContain("private");
    expect(hook.result.current.canAccept).toBe(true);
    await act(() => hook.result.current.submit(criteria));
    expect(observed.commands).toHaveLength(2);
    expect(observed.basis).toHaveLength(2);
    expect(observed.commands[1]!.command_id).not.toBe(original!.command_id);
  });

  it("keeps a lookup HTTP/network error unknown and never uses it as rejection", async () => {
    const observed = routes();
    server.use(http.post("/api/v1/commands", async ({ request }) => { observed.commands.push(await request.json() as CommandRequest); return HttpResponse.error(); }));
    const hook = mount();
    await ready(hook);
    await act(() => hook.result.current.submit(criteria));
    server.use(http.get(`/api/v1/workspaces/${RESULT_WORKSPACE_ID}/commands/:commandId`, () => HttpResponse.json({ error: { code: "private", message: "private lookup detail" } }, { status: 500 })));
    await act(() => hook.result.current.lookupOriginal());
    expect(hook.result.current.status).toBe("unknown");
    expect(hook.result.current.canAccept).toBe(false);
    expect(hook.result.current.error).not.toContain("private");
    expect(observed.commands).toHaveLength(1);
  });
});

describe("native acceptance pre-command fences", () => {
  it("disables authorization when the reviewed human session expires", async () => {
    const observed = routes();
    server.use(http.get("/product/v1/session", () => HttpResponse.json({ ...session, expires_at: new Date(Date.now() + 300).toISOString() })));
    const hook = mount();
    await ready(hook);
    await waitFor(() => expect(hook.result.current.authorized).toBe(false), { timeout: 2_000 });
    expect(hook.result.current.canAccept).toBe(false);
    expect(observed.commands).toHaveLength(0);
    expect(observed.basis).toHaveLength(0);
  });

  it.each(["expired", "foreign", "absent", "unavailable-host"])("rejects fresh %s authority without basis, identity, or command", async mode => {
    const observed = routes();
    const hook = mount();
    await ready(hook);
    if (mode === "unavailable-host") server.use(http.get("/main-agent/v1/health", () => HttpResponse.json({ status: "ok", service: "unknown" })));
    else server.use(http.get("/product/v1/session", () => mode === "absent" ? HttpResponse.json({ error: { code: "local_session_required", message: "private auth" } }, { status: 401 }) : HttpResponse.json({ ...session, ...(mode === "expired" ? { expires_at: "2000-01-01T00:00:00Z" } : { principal_id: "another-human" }) })));
    const identity = vi.spyOn(client, "createCommandIdentity");
    await act(() => hook.result.current.submit(criteria));
    expect(observed.commands).toHaveLength(0);
    expect(observed.basis).toHaveLength(0);
    expect(identity).not.toHaveBeenCalled();
    expect(localStorage.length).toBe(0);
  });

  it.each(["root", "candidate", "task-version", "result-version"])("refuses changed canonical %s before basis or identity", async field => {
    const observed = routes();
    const hook = mount();
    await ready(hook);
    server.use(http.get(base, () => HttpResponse.json(resultView(field === "root" ? { root_task_id: rootB } : field === "candidate" ? { available_actions: [{ ...resultAction(), candidate_id: rootB }] } : field === "task-version" ? { task_version: 12 } : { result_version: 8 }))));
    const identity = vi.spyOn(client, "createCommandIdentity");
    await act(() => hook.result.current.submit(criteria));
    expect(observed.basis).toHaveLength(0);
    expect(observed.commands).toHaveLength(0);
    expect(identity).not.toHaveBeenCalled();
    expect(hook.result.current.error).toBe("成果已变化，请重新审阅");
  });

  it.each(["failure", "candidate", "hash", "version"])("does not create a pending original from basis %s", async mode => {
    const observed = routes();
    const hook = mount();
    await ready(hook);
    server.use(http.post(`${base}/${RESULT_CANDIDATE_ID}/acceptance-basis`, async ({ request }) => {
      observed.basis.push(await request.json());
      return mode === "failure" ? HttpResponse.json({ error: { code: "private", message: "private basis detail" } }, { status: 409 }) : HttpResponse.json({ candidate_id: mode === "candidate" ? rootB : RESULT_CANDIDATE_ID, candidate_hash: mode === "hash" ? "b".repeat(64) : "c".repeat(64), expected_result_version: mode === "version" ? 8 : 7, acceptance_basis_hash: "f".repeat(64) });
    }));
    const identity = vi.spyOn(client, "createCommandIdentity");
    await act(() => hook.result.current.submit(criteria));
    expect(observed.basis).toHaveLength(1);
    expect(observed.commands).toHaveLength(0);
    expect(identity).not.toHaveBeenCalled();
    expect(hook.result.current.original).toBeNull();
    expect(hook.result.current.error).not.toContain("private");
    expect(localStorage.length).toBe(0);
  });

  it("exposes a failed storage retry without automatically submitting", async () => {
    const observed = routes();
    const hook = mount();
    await ready(hook);
    const set = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("private storage failure"); });
    await act(() => hook.result.current.submit(criteria));
    expect(observed.commands).toHaveLength(0);
    expect(hook.result.current.storageError).not.toBeNull();
    set.mockRestore();
    act(() => hook.result.current.retryStorage());
    expect(hook.result.current.storageError).toBeNull();
    expect(observed.commands).toHaveLength(0);
    expect(observed.basis).toHaveLength(1);
    await act(() => hook.result.current.submit(criteria));
    expect(observed.commands).toHaveLength(1);
    expect(observed.basis).toHaveLength(2);
  });

  it("will not GET an original with newly foreign principal authority", async () => {
    const observed = routes();
    server.use(http.post("/api/v1/commands", async ({ request }) => { observed.commands.push(await request.json() as CommandRequest); return HttpResponse.error(); }));
    const hook = mount();
    await ready(hook);
    await act(() => hook.result.current.submit(criteria));
    server.use(http.get("/product/v1/session", () => HttpResponse.json({ ...session, principal_id: "different-human" })));
    await act(() => hook.result.current.lookupOriginal());
    expect(observed.lookups).toHaveLength(0);
    expect(observed.commands).toHaveLength(1);
    expect(loadNativeAcceptanceDraft(scope)).toMatchObject({ ok: true, draft: { status: "unknown" } });
  });
});

describe("native acceptance mounted scope boundaries", () => {
  it("drops late basis A after unmount/mount B without identity, storage, command, or B mutation", async () => {
    const observed = routes();
    const held = deferred();
    server.use(http.post(`${base}/${RESULT_CANDIDATE_ID}/acceptance-basis`, async ({ request }) => {
      observed.basis.push(await request.json());
      await held.promise;
      return HttpResponse.json({ candidate_id: RESULT_CANDIDATE_ID, candidate_hash: "c".repeat(64), expected_result_version: 7, acceptance_basis_hash: "f".repeat(64) });
    }));
    let refreshes = 0;
    const first = mount(resultView(), RESULT_ROOT_TASK_ID, async () => { refreshes += 1; });
    await ready(first);
    let submission!: Promise<void>;
    act(() => { submission = first.result.current.submit(criteria); });
    await waitFor(() => expect(observed.basis).toHaveLength(1));
    first.unmount();
    const second = mount(resultView({ root_task_id: rootB, available_actions: [{ ...resultAction(), target_id: rootB }] }), rootB);
    await ready(second);
    const identity = vi.spyOn(client, "createCommandIdentity");
    const set = vi.spyOn(Storage.prototype, "setItem");
    await act(async () => { held.release(); await submission; });
    expect(identity).not.toHaveBeenCalled();
    expect(set).not.toHaveBeenCalled();
    expect(observed.commands).toHaveLength(0);
    expect(second.result.current.original).toBeNull();
    expect(second.result.current.error).toBeNull();
    expect(refreshes).toBe(0);
  });

  it("drops late applied command A after unmount/mount B and leaves A pending and GET-recoverable", async () => {
    const observed = routes();
    const held = deferred();
    server.use(http.post("/api/v1/commands", async ({ request }) => {
      const command = await request.json() as CommandRequest;
      observed.commands.push(command);
      await held.promise;
      return HttpResponse.json(acceptanceReceipt(command));
    }));
    let refreshes = 0;
    const first = mount(resultView(), RESULT_ROOT_TASK_ID, async () => { refreshes += 1; });
    await ready(first);
    let submission!: Promise<void>;
    act(() => { submission = first.result.current.submit(criteria); });
    await waitFor(() => expect(observed.commands).toHaveLength(1));
    const stored = localStorage.getItem(nativeAcceptanceDraftKey(scope));
    expect(loadNativeAcceptanceDraft(scope)).toMatchObject({ ok: true, draft: { status: "pending" } });
    first.unmount();
    const second = mount(resultView({ root_task_id: rootB, available_actions: [{ ...resultAction(), target_id: rootB }] }), rootB);
    await ready(second);
    const set = vi.spyOn(Storage.prototype, "setItem");
    const cache = vi.spyOn(first.queryClient, "setQueryData");
    await act(async () => { held.release(); await submission; });
    expect(set).not.toHaveBeenCalled();
    expect(cache).not.toHaveBeenCalled();
    expect(localStorage.getItem(nativeAcceptanceDraftKey(scope))).toBe(stored);
    expect(second.result.current.original).toBeNull();
    expect(refreshes).toBe(0);
    const reopened = mount();
    await waitFor(() => expect(reopened.result.current.original?.command_id).toBe(observed.commands[0]!.command_id));
    await act(() => reopened.result.current.lookupOriginal());
    expect(reopened.result.current.status).toBe("applied");
    expect(observed.lookups).toEqual([observed.commands[0]!.command_id]);
    expect(observed.commands).toHaveLength(1);
    expect(observed.basis).toHaveLength(1);
  });
});
