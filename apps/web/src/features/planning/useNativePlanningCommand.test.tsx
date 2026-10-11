import { act, renderHook } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { LocalSession } from "@/features/local-session/api";
import type { CommandRequest } from "@/features/assignments/types";
import { server } from "@/test/server";
import type { PlanningConfirmationDraft, PlanningDraftScope } from "./planningConfirmationDraft";
import { useNativePlanningCommand } from "./useNativePlanningCommand";
import { loadPlanningDraft, savePlanningDraft } from "./planningConfirmationDraft";

type CreatePlanningDraft = Extract<PlanningConfirmationDraft, { kind: "create" }>;

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const REQUEST_ID = "30000000-0000-0000-0000-000000000001";
const COMMAND_ID = "40000000-0000-0000-0000-000000000001";
const SESSION: LocalSession = {
  session_id: "50000000-0000-0000-0000-000000000001",
  workspace_id: WORKSPACE_ID,
  principal_id: "principal-current",
  expires_at: "2099-01-01T00:00:00Z",
};
const SCOPE: Extract<PlanningDraftScope, { kind: "create" }> = {
  kind: "create",
  workspace_id: WORKSPACE_ID,
  request_id: REQUEST_ID,
  principal_id: SESSION.principal_id,
};
const SCOPE_B: PlanningDraftScope = { ...SCOPE, request_id: "30000000-0000-0000-0000-000000000002" };
const REVISION_SCOPE: Extract<PlanningDraftScope, { kind: "revision" }> = {
  kind: "revision",
  workspace_id: WORKSPACE_ID,
  request_id: "30000000-0000-0000-0000-000000000003",
  principal_id: SESSION.principal_id,
  root_task_id: "30000000-0000-0000-0000-000000000004",
};
const REVISION_REVIEW = {
  requestVersion: 4,
  proposalRevision: 2,
  proposalContentHash: "a".repeat(64),
  profileId: "30000000-0000-0000-0000-000000000005",
  profileVersion: 3,
  rootTaskId: REVISION_SCOPE.root_task_id,
  diffHash: "b".repeat(64),
  baseSnapshotId: "30000000-0000-0000-0000-000000000006",
  baseContentHash: "c".repeat(64),
  baseGeneration: 5,
  baseRootTaskVersion: 11,
  targetsFingerprint: "[]",
  reusedBindingsFingerprint: "[]",
};

function original(): CommandRequest {
  return {
    command_id: COMMAND_ID,
    workspace_id: WORKSPACE_ID,
    command_type: "CancelPlanningRequest",
    actor: { type: "user", id: SESSION.principal_id },
    target_type: "planning_request",
    target_id: REQUEST_ID,
    expected_version: 4,
    idempotency_key: "60000000-0000-0000-0000-000000000001",
    correlation_id: "70000000-0000-0000-0000-000000000001",
    payload: {},
  };
}

function initialDraft(): CreatePlanningDraft {
  return {
    ...SCOPE,
    schema_version: 1,
    review: null,
    choices: [],
    original: null,
    status: "idle",
  };
}

function draftFor(
  scope: Extract<PlanningDraftScope, { kind: "create" }>,
  originalCommand: CommandRequest | null = null,
  status: PlanningConfirmationDraft["status"] = "idle",
): Extract<PlanningConfirmationDraft, { kind: "create" }> {
  return { ...initialDraft(), ...scope, original: originalCommand, status };
}

function originalFor(scope: PlanningDraftScope, commandId = COMMAND_ID): CommandRequest {
  const command = original();
  return {
    ...command,
    command_id: commandId,
    target_id: scope.request_id,
    idempotency_key: commandId === COMMAND_ID ? command.idempotency_key : "70000000-0000-0000-0000-000000000002",
    correlation_id: commandId === COMMAND_ID ? command.correlation_id : "80000000-0000-0000-0000-000000000002",
  };
}

function revisionOriginal(commandId = COMMAND_ID): CommandRequest {
  return {
    ...originalFor(REVISION_SCOPE, commandId),
    command_type: "ConfirmPlanRevision",
    payload: {
      expected_proposal_revision: REVISION_REVIEW.proposalRevision,
      expected_proposal_content_hash: REVISION_REVIEW.proposalContentHash,
      expected_diff_hash: REVISION_REVIEW.diffHash,
      expected_base_snapshot_id: REVISION_REVIEW.baseSnapshotId,
      expected_base_content_hash: REVISION_REVIEW.baseContentHash,
      profile_id: REVISION_REVIEW.profileId,
      profile_version: REVISION_REVIEW.profileVersion,
      assignee_bindings: [],
    },
  };
}

function revisionDraft(
  originalCommand: CommandRequest | null = null,
  status: PlanningConfirmationDraft["status"] = "idle",
  scope: Extract<PlanningDraftScope, { kind: "revision" }> = REVISION_SCOPE,
): Extract<PlanningConfirmationDraft, { kind: "revision" }> {
  return {
    ...scope,
    schema_version: 1,
    review: REVISION_REVIEW,
    choices: [],
    original: originalCommand,
    status,
  };
}

function deferred<T = void>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  const promise = new Promise<T>((settle) => { resolve = settle; });
  return { promise, resolve };
}

function appliedStatus(command: CommandRequest) {
  return {
    command_id: command.command_id,
    command_type: command.command_type,
    target_type: command.target_type,
    target_id: command.target_id,
    expected_version: command.expected_version,
    status: "applied",
    result: {
      command_id: command.command_id,
      aggregate_id: command.target_id,
      aggregate_version: 5,
      event_ids: [],
    },
  };
}

function failedFeedbackOriginal(scope: PlanningDraftScope): CommandRequest {
  return { ...originalFor(scope), command_type: "ReviseFailedPlanningRequest", payload: {
    failed_planning_run_id: "c0000000-0000-0000-0000-000000000001", expected_failed_planning_run_version: 3,
    expected_work_context_version: 4, work_context_revision_id: "d0000000-0000-0000-0000-000000000001",
    work_context_content_hash: "a".repeat(64), feedback: "Retain the original requirements" } };
}

function emptyFeedbackDraft(scope: PlanningDraftScope): PlanningConfirmationDraft {
  return { ...scope, schema_version: 1, review: null, choices: [], original: null, status: "idle" };
}

afterEach(() => {
  server.resetHandlers();
  vi.restoreAllMocks();
});

describe("native planning command lifecycle", () => {
  it.each([SCOPE, REVISION_SCOPE])("stores failed planning feedback before one POST in $kind scope", async (scope) => {
    window.localStorage.clear();
    const command = failedFeedbackOriginal(scope);
    const draft: PlanningConfirmationDraft = { ...scope, schema_version: 1, review: null, choices: [], original: null, status: "idle" };
    let posts = 0;
    server.use(http.post("/api/v1/commands", async ({ request }) => {
      posts += 1;
      expect(await request.json()).toEqual(command);
      expect(loadPlanningDraft(scope)).toMatchObject({ ok: true, draft: { original: command, status: "pending" } });
      return HttpResponse.json({ command_id: command.command_id, aggregate_id: scope.request_id, aggregate_version: 5, event_ids: [] });
    }));
    const { result } = renderHook(() => useNativePlanningCommand({
      scope, session: SESSION, draft,
      onDraftChange: (next) => savePlanningDraft(scope, next).ok,
      onApplied: vi.fn(),
    }));
    await act(async () => { await Promise.all([result.current.submitOriginal(command), result.current.submitOriginal(command)]); });
    expect(posts).toBe(1);
    expect(result.current.status).toBe("applied");
  });
  it("persists the complete original before one POST and applies only a matching result", async () => {
    const events: string[] = [];
    let sent: CommandRequest | null = null;
    const onDraftChange = vi.fn((next: PlanningConfirmationDraft) => {
      events.push(`saved:${next.status}`);
      return true;
    });
    const onApplied = vi.fn();
    server.use(http.post("/api/v1/commands", async ({ request }) => {
      events.push("post");
      sent = await request.json() as CommandRequest;
      return HttpResponse.json({
        command_id: COMMAND_ID,
        aggregate_id: REQUEST_ID,
        aggregate_version: 5,
        event_ids: [],
      });
    }));
    const { result } = renderHook(() => useNativePlanningCommand({
      scope: SCOPE,
      session: SESSION,
      draft: initialDraft(),
      onDraftChange,
      onApplied,
    }));

    await act(async () => { await result.current.submitOriginal(original()); });

    expect(events.slice(0, 2)).toEqual(["saved:pending", "post"]);
    expect(sent).toEqual(original());
    expect(result.current.status).toBe("applied");
    expect(result.current.original).toEqual(original());
    expect(onApplied).toHaveBeenCalledWith(original(), expect.objectContaining({ command_id: COMMAND_ID }));
  });

  it("keeps a lost reply unresolved, locks resubmission, and looks up only the original", async () => {
    let posts = 0;
    let reads = 0;
    const saved: PlanningConfirmationDraft[] = [];
    server.use(
      http.post("/api/v1/commands", () => {
        posts += 1;
        return HttpResponse.error();
      }),
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/commands/${COMMAND_ID}`, () => {
        reads += 1;
        return HttpResponse.json({
          command_id: COMMAND_ID,
          command_type: "CancelPlanningRequest",
          target_type: "planning_request",
          target_id: REQUEST_ID,
          expected_version: 4,
          status: "applied",
          result: null,
        });
      }),
    );
    const onDraftChange = (next: PlanningConfirmationDraft) => {
      saved.push(next);
      return true;
    };
    const { result } = renderHook(() => useNativePlanningCommand({
      scope: SCOPE,
      session: SESSION,
      draft: saved.at(-1) ?? initialDraft(),
      onDraftChange,
      onApplied: vi.fn(),
    }));

    await act(async () => { await result.current.submitOriginal(original()); });
    expect(result.current.status).toBe("unknown");
    expect(result.current.unresolved).toBe(true);
    await act(async () => { await result.current.submitOriginal(original()); });
    await act(async () => { await result.current.lookupOriginal(); });

    expect(posts).toBe(1);
    expect(reads).toBe(1);
    expect(result.current.status).toBe("applied");
    expect(result.current.unresolved).toBe(false);
    expect(result.current.error).toBeNull();
  });

  it("treats an uncorrelated successful POST response as unknown", async () => {
    let posts = 0;
    const onApplied = vi.fn();
    server.use(http.post("/api/v1/commands", () => {
      posts += 1;
      return HttpResponse.json({
        command_id: COMMAND_ID,
        aggregate_id: "another-planning-request",
        aggregate_version: 5,
        event_ids: [],
      });
    }));
    const { result } = renderHook(() => useNativePlanningCommand({
      scope: SCOPE,
      session: SESSION,
      draft: initialDraft(),
      onDraftChange: () => true,
      onApplied,
    }));

    await act(async () => { await result.current.submitOriginal(original()); });

    expect(posts).toBe(1);
    expect(result.current.status).toBe("unknown");
    expect(result.current.unresolved).toBe(true);
    expect(result.current.original).toEqual(original());
    expect(onApplied).not.toHaveBeenCalled();
  });

  it.each([
    ["pending", "pending"],
    ["rejected", "rejected"],
  ] as const)("observes the original command as %s without another POST", async (serverStatus, expectedStatus) => {
    let posts = 0;
    let reads = 0;
    server.use(
      http.post("/api/v1/commands", () => {
        posts += 1;
        return HttpResponse.error();
      }),
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/commands/${COMMAND_ID}`, () => {
        reads += 1;
        return HttpResponse.json({
          command_id: COMMAND_ID,
          command_type: "CancelPlanningRequest",
          target_type: "planning_request",
          target_id: REQUEST_ID,
          expected_version: 4,
          status: serverStatus,
          result: null,
        });
      }),
    );
    const { result } = renderHook(() => useNativePlanningCommand({
      scope: SCOPE,
      session: SESSION,
      draft: initialDraft(),
      onDraftChange: () => true,
      onApplied: vi.fn(),
    }));

    await act(async () => { await result.current.submitOriginal(original()); });
    expect(result.current.status).toBe("unknown");
    await act(async () => { await result.current.lookupOriginal(); });

    expect(posts).toBe(1);
    expect(reads).toBe(1);
    expect(result.current.status).toBe(expectedStatus);
    expect(result.current.unresolved).toBe(expectedStatus === "pending");
  });

  it("does not POST when the original cannot be durably preserved", async () => {
    let posts = 0;
    server.use(http.post("/api/v1/commands", () => {
      posts += 1;
      return HttpResponse.json({});
    }));
    const { result } = renderHook(() => useNativePlanningCommand({
      scope: SCOPE,
      session: SESSION,
      draft: initialDraft(),
      onDraftChange: () => false,
      onApplied: vi.fn(),
    }));

    await act(async () => { await result.current.submitOriginal(original()); });

    expect(posts).toBe(0);
    expect(result.current.error).toBe("无法保存本地确认草稿");
  });

  it("hides old original state immediately after the principal scope changes", async () => {
    const savedDraft: PlanningConfirmationDraft = { ...initialDraft(), original: original(), status: "unknown" };
    const { result, rerender } = renderHook(
      ({ scope, session, draft }) => useNativePlanningCommand({
        scope,
        session,
        draft,
        onDraftChange: vi.fn(),
        onApplied: vi.fn(),
      }),
      { initialProps: { scope: SCOPE, session: SESSION, draft: savedDraft } },
    );

    expect(result.current.original).toEqual(original());
    rerender({
      scope: { ...SCOPE, principal_id: "another-principal" },
      session: { ...SESSION, principal_id: "another-principal" },
      draft: initialDraft(),
    });

    expect(result.current.original).toBeNull();
    expect(result.current.status).toBe("idle");
  });

  it("releases a deferred POST's scope lock without disturbing another tab's original", async () => {
    const postStarted = deferred();
    const releasePost = deferred();
    const postedA = originalFor(SCOPE);
    const originalB = originalFor(SCOPE_B, "40000000-0000-0000-0000-000000000002");
    const saved = new Map<string, CreatePlanningDraft>();
    const onAppliedA = vi.fn();
    const onAppliedB = vi.fn();
    let posts = 0;
    let reads = 0;
    server.use(
      http.post("/api/v1/commands", async ({ request }) => {
        posts += 1;
        expect(await request.json()).toEqual(postedA);
        postStarted.resolve();
        await releasePost.promise;
        return HttpResponse.json({
          command_id: postedA.command_id,
          aggregate_id: postedA.target_id,
          aggregate_version: 5,
          event_ids: [],
        });
      }),
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/commands/${COMMAND_ID}`, () => {
        reads += 1;
        return HttpResponse.json(appliedStatus(postedA));
      }),
    );
    const persist = (next: PlanningConfirmationDraft) => {
      if (next.kind === "create") saved.set(next.request_id, next);
      return true;
    };
    const { result, rerender } = renderHook(
      ({ activeScope, activeDraft, onApplied }) => useNativePlanningCommand({
        scope: activeScope,
        session: SESSION,
        draft: activeDraft,
        onDraftChange: persist,
        onApplied,
      }),
      { initialProps: { activeScope: SCOPE, activeDraft: draftFor(SCOPE), onApplied: onAppliedA } },
    );
    let submit!: Promise<void>;
    act(() => { submit = result.current.submitOriginal(postedA); });
    await postStarted.promise;

    rerender({ activeScope: SCOPE_B, activeDraft: draftFor(SCOPE_B, originalB, "unknown"), onApplied: onAppliedB });
    expect(result.current.original).toEqual(originalB);
    releasePost.resolve();
    await act(async () => { await submit; });
    expect(result.current.original).toEqual(originalB);
    expect(onAppliedB).not.toHaveBeenCalled();

    rerender({ activeScope: SCOPE, activeDraft: saved.get(SCOPE.request_id) ?? draftFor(SCOPE), onApplied: onAppliedA });
    expect(result.current.original).toEqual(postedA);
    expect(result.current.busy).toBe(false);
    await act(async () => { await result.current.lookupOriginal(); });

    expect(posts).toBe(1);
    expect(reads).toBe(1);
    expect(result.current.original).toEqual(postedA);
    expect(result.current.status).toBe("applied");
    expect(onAppliedA).toHaveBeenCalledTimes(1);
    expect(onAppliedB).not.toHaveBeenCalled();
  });

  it("releases a deferred GET's scope lock so returning can check the same original again", async () => {
    const getStarted = deferred();
    const releaseGet = deferred();
    const originalA = originalFor(SCOPE);
    const originalB = originalFor(SCOPE_B, "40000000-0000-0000-0000-000000000002");
    let posts = 0;
    let reads = 0;
    const onAppliedA = vi.fn();
    const onAppliedB = vi.fn();
    server.use(
      http.post("/api/v1/commands", () => {
        posts += 1;
        return HttpResponse.json({});
      }),
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/commands/${COMMAND_ID}`, async () => {
        reads += 1;
        if (reads === 1) {
          getStarted.resolve();
          await releaseGet.promise;
        }
        return HttpResponse.json(appliedStatus(originalA));
      }),
    );
    const { result, rerender } = renderHook(
      ({ activeScope, activeDraft, onApplied }) => useNativePlanningCommand({
        scope: activeScope,
        session: SESSION,
        draft: activeDraft,
        onDraftChange: () => true,
        onApplied,
      }),
      { initialProps: {
        activeScope: SCOPE,
        activeDraft: draftFor(SCOPE, originalA, "unknown"),
        onApplied: onAppliedA,
      } },
    );
    let firstLookup!: Promise<void>;
    act(() => { firstLookup = result.current.lookupOriginal(); });
    await getStarted.promise;

    rerender({ activeScope: SCOPE_B, activeDraft: draftFor(SCOPE_B, originalB, "unknown"), onApplied: onAppliedB });
    releaseGet.resolve();
    await act(async () => { await firstLookup; });
    expect(onAppliedB).not.toHaveBeenCalled();

    rerender({ activeScope: SCOPE, activeDraft: draftFor(SCOPE, originalA, "unknown"), onApplied: onAppliedA });
    expect(result.current.original).toEqual(originalA);
    expect(result.current.busy).toBe(false);
    await act(async () => { await result.current.lookupOriginal(); });

    expect(posts).toBe(0);
    expect(reads).toBe(2);
    expect(result.current.original).toEqual(originalA);
    expect(result.current.status).toBe("applied");
    expect(onAppliedA).toHaveBeenCalledTimes(1);
    expect(onAppliedB).not.toHaveBeenCalled();
  });

  it("does not notify the next scope when applied persistence finishes after a scope switch", async () => {
    const appliedWriteStarted = deferred();
    const releaseAppliedWrite = deferred<boolean>();
    const originalA = originalFor(SCOPE);
    const savedA: CreatePlanningDraft[] = [];
    const onAppliedA = vi.fn();
    const onAppliedB = vi.fn();
    server.use(http.post("/api/v1/commands", () => HttpResponse.json({
      command_id: originalA.command_id,
      aggregate_id: originalA.target_id,
      aggregate_version: 5,
      event_ids: [],
    })));
    const persistA = (next: PlanningConfirmationDraft) => {
      if (next.kind === "create") savedA.push(next);
      if (next.status === "applied") {
        appliedWriteStarted.resolve();
        return releaseAppliedWrite.promise;
      }
      return true;
    };
    const { result, rerender } = renderHook(
      ({ activeScope, activeDraft, onDraftChange, onApplied }) => useNativePlanningCommand({
        scope: activeScope,
        session: SESSION,
        draft: activeDraft,
        onDraftChange,
        onApplied,
      }),
      { initialProps: {
        activeScope: SCOPE,
        activeDraft: draftFor(SCOPE),
        onDraftChange: persistA,
        onApplied: onAppliedA,
      } },
    );
    let submit!: Promise<void>;
    act(() => { submit = result.current.submitOriginal(originalA); });
    await appliedWriteStarted.promise;

    rerender({
      activeScope: SCOPE_B,
      activeDraft: draftFor(SCOPE_B),
      onDraftChange: () => true,
      onApplied: onAppliedB,
    });
    releaseAppliedWrite.resolve(true);
    await act(async () => { await submit; });
    expect(onAppliedB).not.toHaveBeenCalled();

    rerender({
      activeScope: SCOPE,
      activeDraft: savedA.find((savedDraft) => savedDraft.status === "pending") ?? draftFor(SCOPE),
      onDraftChange: persistA,
      onApplied: onAppliedA,
    });
    expect(result.current.original).toEqual(originalA);
    expect(result.current.status).toBe("pending");
    expect(onAppliedA).not.toHaveBeenCalled();
  });

  it.each([
    ["missing", undefined],
    ["unknown", "unknown"],
  ] as const)("keeps an original unresolved when GET status is %s", async (_label, status) => {
    const originalA = originalFor(SCOPE);
    let posts = 0;
    const onApplied = vi.fn();
    server.use(http.get(`/api/v1/workspaces/${WORKSPACE_ID}/commands/${COMMAND_ID}`, () => HttpResponse.json({
      command_id: originalA.command_id,
      command_type: originalA.command_type,
      target_type: originalA.target_type,
      target_id: originalA.target_id,
      expected_version: originalA.expected_version,
      ...(status === undefined ? {} : { status }),
      result: null,
    })));
    server.use(http.post("/api/v1/commands", () => {
      posts += 1;
      return HttpResponse.json({});
    }));
    const { result } = renderHook(() => useNativePlanningCommand({
      scope: SCOPE,
      session: SESSION,
      draft: draftFor(SCOPE, originalA, "unknown"),
      onDraftChange: () => true,
      onApplied,
    }));

    await act(async () => { await result.current.lookupOriginal(); });

    expect(posts).toBe(0);
    expect(result.current.status).toBe("unknown");
    expect(result.current.unresolved).toBe(true);
    expect(result.current.original).toEqual(originalA);
    expect(onApplied).not.toHaveBeenCalled();
  });

  it("persists and submits one exact ConfirmPlanRevision under the root-scoped revision key", async () => {
    const events: string[] = [];
    const command = revisionOriginal();
    let sent: CommandRequest | null = null;
    server.use(http.post("/api/v1/commands", async ({ request }) => {
      events.push("post");
      sent = await request.json() as CommandRequest;
      return HttpResponse.json({
        command_id: command.command_id,
        aggregate_id: command.target_id,
        aggregate_version: 5,
        event_ids: [],
      });
    }));
    const { result } = renderHook(() => useNativePlanningCommand({
      scope: REVISION_SCOPE,
      session: SESSION,
      draft: revisionDraft(),
      onDraftChange: (next) => { events.push(`saved:${next.status}`); return true; },
      onApplied: vi.fn(),
    }));

    await act(async () => { await result.current.submitOriginal(command); });

    expect(events.slice(0, 2)).toEqual(["saved:pending", "post"]);
    expect(sent).toEqual(command);
    expect(result.current.status).toBe("applied");
  });

  it("does not issue a second applied ConfirmPlanRevision for the same root-scoped draft", async () => {
    let posts = 0;
    server.use(http.post("/api/v1/commands", () => {
      posts += 1;
      return HttpResponse.json({});
    }));
    const { result } = renderHook(() => useNativePlanningCommand({
      scope: REVISION_SCOPE,
      session: SESSION,
      draft: revisionDraft(revisionOriginal(), "applied"),
      onDraftChange: () => true,
      onApplied: vi.fn(),
    }));

    await act(async () => {
      await result.current.submitOriginal(revisionOriginal("40000000-0000-0000-0000-000000000009"));
    });

    expect(posts).toBe(0);
    expect(result.current.status).toBe("applied");
  });

  it("rejects non-revision commands from a revision scope before POST", async () => {
    let posts = 0;
    server.use(http.post("/api/v1/commands", () => {
      posts += 1;
      return HttpResponse.json({});
    }));
    const { result } = renderHook(() => useNativePlanningCommand({
      scope: REVISION_SCOPE,
      session: SESSION,
      draft: revisionDraft(),
      onDraftChange: () => true,
      onApplied: vi.fn(),
    }));

    await act(async () => { await result.current.submitOriginal(originalFor(REVISION_SCOPE)); });

    expect(posts).toBe(0);
    expect(result.current.error).toBe("命令结果不可确认");
  });

  it("hides a revision draft captured for another root task", async () => {
    let posts = 0;
    server.use(http.post("/api/v1/commands", () => {
      posts += 1;
      return HttpResponse.json({});
    }));
    const { result } = renderHook(() => useNativePlanningCommand({
      scope: REVISION_SCOPE,
      session: SESSION,
      draft: revisionDraft(null, "idle", { ...REVISION_SCOPE, root_task_id: "30000000-0000-0000-0000-000000000007" }),
      onDraftChange: () => true,
      onApplied: vi.fn(),
    }));

    await act(async () => { await result.current.submitOriginal(revisionOriginal()); });

    expect(posts).toBe(0);
    expect(result.current.original).toBeNull();
  });
});

describe.each([SCOPE, REVISION_SCOPE])("failed feedback command recovery in $kind scope", (scope) => {
  it.each(["rejected", "applied"] as const)("retains original identities after remount through pending, unknown, mismatched and %s receipts without POST", async (terminal) => {
    window.localStorage.clear();
    const command = failedFeedbackOriginal(scope); let posts = 0, reads = 0; let status = "pending"; let mismatch = false;
    const onApplied = vi.fn();
    server.use(http.post("/api/v1/commands", () => { posts += 1; return HttpResponse.error(); }),
      http.get(`/api/v1/workspaces/${WORKSPACE_ID}/commands/${command.command_id}`, () => {
        reads += 1; return HttpResponse.json({ ...appliedStatus(command), status, result: null,
          target_id: mismatch ? SCOPE_B.request_id : scope.request_id });
      }));
    const mount = () => renderHook(() => {
      const loaded = loadPlanningDraft(scope); if (!loaded.ok) throw new Error(loaded.reason);
      return useNativePlanningCommand({ scope, session: SESSION, draft: loaded.draft ?? emptyFeedbackDraft(scope),
        onDraftChange: (next) => savePlanningDraft(scope, next).ok, onApplied });
    });
    const first = mount(); await act(async () => { await first.result.current.submitOriginal(command); });
    expect(first.result.current.status).toBe("unknown");
    await act(async () => { await first.result.current.submitOriginal(command); }); first.unmount();
    const second = mount();
    await act(async () => { await second.result.current.submitOriginal(command); });
    for (const next of ["pending", "unknown", "mismatched", terminal]) {
      mismatch = next === "mismatched"; status = mismatch ? "applied" : next;
      await act(async () => { await second.result.current.lookupOriginal(); });
      expect(second.result.current.status).toBe(mismatch ? "unknown" : next);
      expect(second.result.current.original).toEqual(command);
      expect(loadPlanningDraft(scope)).toMatchObject({ ok: true, draft: { original: command } });
    }
    expect(posts).toBe(1); expect(reads).toBe(4);
    if (terminal === "applied") expect(onApplied).toHaveBeenCalledExactlyOnceWith(command, null);
    else expect(onApplied).not.toHaveBeenCalled();
  });

  it.each(["storage", "expired", "foreign-principal", "foreign-workspace"] as const)("refuses feedback POST with %s", async (failure) => {
    const session = { ...SESSION,
      ...(failure === "expired" ? { expires_at: "2000-01-01T00:00:00Z" } : {}),
      ...(failure === "foreign-principal" ? { principal_id: "other" } : {}),
      ...(failure === "foreign-workspace" ? { workspace_id: "20000000-0000-0000-0000-000000000002" } : {}) };
    let posts = 0;
    server.use(http.post("/api/v1/commands", () => { posts += 1; return HttpResponse.json({}); }));
    const { result } = renderHook(() => useNativePlanningCommand({ scope, session, draft: emptyFeedbackDraft(scope),
      onDraftChange: () => failure !== "storage", onApplied: vi.fn() }));
    await act(async () => { await result.current.submitOriginal(failedFeedbackOriginal(scope)); });
    expect(posts).toBe(0); expect(result.current.status).toBe("idle");
  });

  it.each(["POST", "lookup"] as const)("fences a delayed feedback %s across a principal switch", async (transport) => {
    const started = deferred(), release = deferred(); const original = failedFeedbackOriginal(scope);
    const nextScope = { ...scope, principal_id: "other" }; const onApplied = vi.fn(); const saved: PlanningConfirmationDraft[] = [];
    let posts = 0, reads = 0;
    server.use(http.post("/api/v1/commands", async () => {
      posts += 1; started.resolve(); await release.promise; return HttpResponse.json(appliedStatus(original).result);
    }), http.get(`/api/v1/workspaces/${WORKSPACE_ID}/commands/${original.command_id}`, async () => {
      reads += 1; started.resolve(); await release.promise; return HttpResponse.json({ ...appliedStatus(original), result: null });
    }));
    const draft = { ...emptyFeedbackDraft(scope), ...(transport === "lookup" ? { original, status: "unknown" as const } : {}) };
    const { result, rerender } = renderHook(({ activeScope, activeDraft }) => useNativePlanningCommand({ scope: activeScope,
      session: { ...SESSION, principal_id: activeScope.principal_id }, draft: activeDraft,
      onDraftChange: (next) => { saved.push(next); return true; }, onApplied }),
    { initialProps: { activeScope: scope, activeDraft: draft } });
    let operation!: Promise<void>;
    act(() => { operation = transport === "POST" ? result.current.submitOriginal(original) : result.current.lookupOriginal(); });
    await started.promise;
    rerender({ activeScope: nextScope, activeDraft: emptyFeedbackDraft(nextScope) });
    release.resolve(); await act(async () => { await operation; });
    expect(result.current.status).toBe("idle"); expect(result.current.original).toBeNull(); expect(onApplied).not.toHaveBeenCalled();
    expect(saved.every((value) => value.principal_id === scope.principal_id && value.status === "pending")).toBe(true);
    expect(posts).toBe(transport === "POST" ? 1 : 0); expect(reads).toBe(transport === "lookup" ? 1 : 0);
  });
});
