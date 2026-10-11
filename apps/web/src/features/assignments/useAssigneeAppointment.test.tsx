import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { createElement, type PropsWithChildren } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PROFILE, PROFILE_REF, profileAt } from "@/features/agents/testFixtures";
import type { LocalSession } from "@/features/local-session/api";
import type { TaskView } from "@/features/tasks/types";
import { server } from "@/test/server";
import type { AgentBindingView, AssigneeValue, CommandRequest, CommandResponse } from "./types";
import {
  appointmentDraftKey,
  loadAppointmentDraft,
  saveAppointmentDraft,
  type AppointmentScope,
} from "./assigneeAppointmentState";
import { useAssigneeAppointment } from "./useAssigneeAppointment";

const WORKSPACE_ID = "20000000-0000-0000-0000-000000000001";
const SUBTASK_ID = "30000000-0000-0000-0000-000000000002";
const OTHER_TASK_ID = "30000000-0000-0000-0000-000000000003";
const SESSION: LocalSession = {
  session_id: "50000000-0000-0000-0000-000000000001",
  workspace_id: WORKSPACE_ID,
  principal_id: "principal-current",
  expires_at: "2099-01-01T00:00:00Z",
};
const scope: AppointmentScope = {
  workspace_id: WORKSPACE_ID,
  principal_id: SESSION.principal_id,
  target_kind: "subtask",
  target_id: SUBTASK_ID,
};
const task: TaskView = {
  id: SUBTASK_ID,
  workspace_id: WORKSPACE_ID,
  title: "Implementation",
  description: "Implement the dependency",
  acceptance_criteria: ["Tests pass"],
  requested_by: SESSION.principal_id,
  priority: 1,
  status: "running",
  version: 7,
  created_at: "2026-10-07T00:00:00Z",
  updated_at: "2026-10-07T00:00:00Z",
};
const binding: AgentBindingView = {
  workspace_id: WORKSPACE_ID,
  target_type: "subtask",
  target_id: SUBTASK_ID,
  assignee: { kind: "human", id: SESSION.principal_id },
  profile_ref: null,
  version: 2,
  runnable: false,
  not_runnable_reasons: [],
  created_at: "2026-10-07T00:00:00Z",
  changed_at: "2026-10-07T00:00:00Z",
};
const BASE = `/api/v1/workspaces/${WORKSPACE_ID}`;

function wrapper() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  function Provider({ children }: PropsWithChildren) {
    return createElement(QueryClientProvider, { client }, children);
  }
  return { client, Provider };
}

function installSubtaskReads(options: {
  session?: LocalSession;
  assignment?: AgentBindingView | null;
  taskView?: TaskView;
  profiles?: readonly (typeof PROFILE)[];
  exactProfile?: typeof PROFILE;
} = {}) {
  server.use(
    http.get("/product/v1/session", () => HttpResponse.json(options.session ?? SESSION)),
    http.get(`${BASE}/assignments/subtask/${SUBTASK_ID}`, () =>
      HttpResponse.json(options.assignment === undefined ? binding : options.assignment)),
    http.get(`/api/v1/tasks/${SUBTASK_ID}`, () => HttpResponse.json(options.taskView ?? task)),
    http.get(`/agents/v1/workspaces/${WORKSPACE_ID}/profiles`, () =>
      HttpResponse.json({ items: options.profiles ?? [PROFILE] })),
    http.get(`/agents/v1/workspaces/${WORKSPACE_ID}/profiles/${PROFILE_REF.profile_id}`, ({ request }) => {
      const revision = new URL(request.url).searchParams.get("revision");
      if (revision !== String(PROFILE_REF.revision)) return HttpResponse.json({}, { status: 404 });
      return HttpResponse.json(options.exactProfile ?? PROFILE);
    }),
  );
}

function appointment(onRefresh = vi.fn(async () => Promise.resolve())) {
  const query = wrapper();
  const refresh = onRefresh;
  const hook = renderHook(() => useAssigneeAppointment({
    workspaceId: WORKSPACE_ID,
    targetKind: "subtask",
    targetId: SUBTASK_ID,
    onRefresh: refresh,
  }), { wrapper: query.Provider });
  return { ...query, ...hook, refresh };
}

function profileBinding(version: number): AgentBindingView {
  return {
    ...binding,
    assignee: { kind: "profile", id: PROFILE_REF.profile_id },
    profile_ref: PROFILE_REF,
    version,
    changed_at: "2026-10-07T01:00:00Z",
  };
}

afterEach(() => {
  server.resetHandlers();
  localStorage.clear();
  document.cookie = "sidera_csrf=; Max-Age=0; Path=/";
  vi.restoreAllMocks();
});

describe("useAssigneeAppointment", () => {
  it("submits one current-human appointment against the materialized Task version", async () => {
    installSubtaskReads();
    document.cookie = "sidera_csrf=owned-csrf; Path=/";
    const newBinding: AgentBindingView = {
      ...binding,
      assignee: { kind: "profile", id: PROFILE_REF.profile_id },
      profile_ref: PROFILE_REF,
      version: 3,
      changed_at: "2026-10-07T01:00:00Z",
    };
    let canonicalBinding: AgentBindingView | null = binding;
    let posts = 0;
    const posted: CommandRequest[] = [];
    server.use(
      http.get(`${BASE}/assignments/subtask/${SUBTASK_ID}`, () => HttpResponse.json(canonicalBinding)),
      http.post("/api/v1/commands", async ({ request }) => {
        posts += 1;
        const command = await request.json() as CommandRequest;
        posted.push(command);
        const stored = localStorage.getItem(appointmentDraftKey(scope));
        expect(stored).not.toBeNull();
        expect(JSON.parse(stored ?? "null")).toMatchObject({ original: command, status: "pending" });
        canonicalBinding = newBinding;
        return HttpResponse.json({
          command_id: command.command_id,
          aggregate_id: SUBTASK_ID,
          aggregate_version: 8,
          event_ids: ["60000000-0000-0000-0000-000000000009"],
          data: { binding: newBinding },
        } satisfies CommandResponse);
      }),
    );

    const view = appointment();
    await waitFor(() => expect(view.result.current.review).not.toBeNull());
    expect(view.result.current.review?.aggregateVersion).toBe(7);
    expect(view.result.current.choice).toEqual({
      kind: "human",
      id: SESSION.principal_id,
      profileRef: null,
    });
    expect(view.result.current.canEditChoice).toBe(true);
    expect(view.result.current.canSave).toBe(false);
    act(() => view.result.current.onChoiceChange({
      kind: "profile",
      id: PROFILE_REF.profile_id,
      profileRef: PROFILE_REF,
    } satisfies AssigneeValue));
    await waitFor(() => expect(view.result.current.canSave).toBe(true));

    await act(async () => view.result.current.save());

    expect(posts).toBe(1);
    expect(posted).toHaveLength(1);
    expect(posted[0]).toMatchObject({
      command_type: "SetSubtaskAssignee",
      actor: { type: "user", id: SESSION.principal_id },
      target_type: "task",
      target_id: SUBTASK_ID,
      expected_version: 7,
      payload: {
        assignee_kind: "profile",
        assignee_id: PROFILE_REF.profile_id,
        profile_revision: 1,
      },
    });
    expect(posted[0]?.payload).toEqual({
      assignee_kind: "profile",
      assignee_id: PROFILE_REF.profile_id,
      profile_revision: 1,
    });
    expect(view.refresh).toHaveBeenCalledTimes(1);
  });

  it("requires a canonical binding version advance before an applied owner is saved", async () => {
    installSubtaskReads();
    let canonicalBinding: AgentBindingView | null = binding;
    let currentTask = task;
    let posts = 0;
    const statusIds: string[] = [];
    server.use(
      http.get(`${BASE}/assignments/subtask/${SUBTASK_ID}`, () => HttpResponse.json(canonicalBinding)),
      http.get(`/api/v1/tasks/${SUBTASK_ID}`, () => HttpResponse.json(currentTask)),
      http.post("/api/v1/commands", async ({ request }) => {
        posts += 1;
        const command = await request.json() as CommandRequest;
        canonicalBinding = profileBinding(2);
        currentTask = { ...task, version: 8 };
        return HttpResponse.json({
          command_id: command.command_id,
          aggregate_id: SUBTASK_ID,
          aggregate_version: 8,
          event_ids: [],
        } satisfies CommandResponse);
      }),
      http.get(/\/api\/v1\/workspaces\/.*\/commands\/.*/, ({ request }) => {
        const commandId = new URL(request.url).pathname.split("/").at(-1) ?? "";
        statusIds.push(commandId);
        return HttpResponse.json({
          command_id: commandId,
          command_type: "SetSubtaskAssignee",
          target_type: "task",
          target_id: SUBTASK_ID,
          expected_version: 7,
          status: "applied",
          result: null,
        });
      }),
    );

    const first = appointment();
    await waitFor(() => expect(first.result.current.review?.aggregateVersion).toBe(7));
    act(() => first.result.current.onChoiceChange({
      kind: "profile",
      id: PROFILE_REF.profile_id,
      profileRef: PROFILE_REF,
    }));
    await waitFor(() => expect(first.result.current.canSave).toBe(true));
    await act(async () => first.result.current.save());
    await waitFor(() => expect(first.result.current.status).toBe("applied"));

    const original = first.result.current.original;
    expect(original).not.toBeNull();
    expect(posts).toBe(1);
    expect(first.result.current.saved).toBe(false);
    first.unmount();

    const recovered = appointment();
    await waitFor(() => expect(recovered.result.current.original).toEqual(original));
    await act(async () => recovered.result.current.revisit());
    expect(recovered.result.current.status).toBe("applied");
    expect(recovered.result.current.original).toEqual(original);
    expect(recovered.result.current.saved).toBe(false);
    expect(recovered.result.current.canSave).toBe(false);

    await act(async () => recovered.result.current.lookupOriginal());
    expect(statusIds).toEqual([original?.command_id]);
    expect(recovered.result.current.original).toEqual(original);
    expect(recovered.result.current.canLookupOriginal).toBe(true);
    expect(recovered.result.current.saved).toBe(false);
    expect(posts).toBe(1);

    canonicalBinding = profileBinding(3);
    await act(async () => recovered.result.current.lookupOriginal());
    await waitFor(() => expect(recovered.result.current.saved).toBe(true));
    expect(statusIds).toEqual([original?.command_id, original?.command_id]);
    expect(recovered.result.current.original).toEqual(original);
    expect(posts).toBe(1);
  });

  it("confirms the first canonical binding version from an unassigned review", async () => {
    installSubtaskReads({ assignment: null });
    let canonicalBinding: AgentBindingView | null = null;
    server.use(
      http.get(`${BASE}/assignments/subtask/${SUBTASK_ID}`, () => HttpResponse.json(canonicalBinding)),
      http.post("/api/v1/commands", async ({ request }) => {
        const command = await request.json() as CommandRequest;
        canonicalBinding = profileBinding(1);
        return HttpResponse.json({
          command_id: command.command_id,
          aggregate_id: SUBTASK_ID,
          aggregate_version: 8,
          event_ids: [],
        } satisfies CommandResponse);
      }),
    );

    const view = appointment();
    await waitFor(() => expect(view.result.current.review?.bindingFingerprint).toBe("null"));
    act(() => view.result.current.onChoiceChange({
      kind: "profile",
      id: PROFILE_REF.profile_id,
      profileRef: PROFILE_REF,
    }));
    await waitFor(() => expect(view.result.current.canSave).toBe(true));
    await act(async () => view.result.current.save());
    await waitFor(() => expect(view.result.current.saved).toBe(true));
    expect(view.result.current.original).not.toBeNull();
    expect(view.result.current.binding?.version).toBe(1);
  });

  it("fails closed when an applied draft has a malformed frozen binding fingerprint", async () => {
    installSubtaskReads();
    let canonicalBinding: AgentBindingView | null = binding;
    server.use(
      http.get(`${BASE}/assignments/subtask/${SUBTASK_ID}`, () => HttpResponse.json(canonicalBinding)),
      http.post("/api/v1/commands", async ({ request }) => {
        const command = await request.json() as CommandRequest;
        canonicalBinding = profileBinding(3);
        return HttpResponse.json({
          command_id: command.command_id,
          aggregate_id: SUBTASK_ID,
          aggregate_version: 8,
          event_ids: [],
        } satisfies CommandResponse);
      }),
    );

    const first = appointment();
    await waitFor(() => expect(first.result.current.review).not.toBeNull());
    act(() => first.result.current.onChoiceChange({
      kind: "profile",
      id: PROFILE_REF.profile_id,
      profileRef: PROFILE_REF,
    }));
    await waitFor(() => expect(first.result.current.canSave).toBe(true));
    await act(async () => first.result.current.save());
    await waitFor(() => expect(first.result.current.saved).toBe(true));

    const loaded = loadAppointmentDraft(scope);
    if (!loaded.ok || loaded.draft === null || loaded.draft.review === null) {
      throw new Error("Expected the applied review draft to be persisted");
    }
    const corrupted = {
      ...loaded.draft,
      review: { ...loaded.draft.review, bindingFingerprint: "not-a-tuple" },
    };
    expect(saveAppointmentDraft(scope, corrupted).ok).toBe(true);
    const original = first.result.current.original;
    first.unmount();

    const recovered = appointment();
    await waitFor(() => expect(recovered.result.current.original).toEqual(original));
    expect(recovered.result.current.saved).toBe(false);
    expect(recovered.result.current.canLookupOriginal).toBe(true);
  });

  it("keeps a chosen R1 pin when the catalog head is R2", async () => {
    const profileR2 = profileAt(2);
    installSubtaskReads({ profiles: [profileR2], exactProfile: PROFILE });
    const posted: CommandRequest[] = [];
    server.use(http.post("/api/v1/commands", async ({ request }) => {
      const command = await request.json() as CommandRequest;
      posted.push(command);
      return HttpResponse.json({
        command_id: command.command_id,
        aggregate_id: SUBTASK_ID,
        aggregate_version: 8,
        event_ids: [],
        data: { binding: {
          ...binding,
          assignee: { kind: "profile", id: PROFILE_REF.profile_id },
          profile_ref: PROFILE_REF,
          version: 3,
        } satisfies AgentBindingView },
      } satisfies CommandResponse);
    }));

    const view = appointment();
    await waitFor(() => expect(view.result.current.review).not.toBeNull());
    act(() => view.result.current.onChoiceChange({
      kind: "profile",
      id: PROFILE_REF.profile_id,
      profileRef: PROFILE_REF,
    }));
    await waitFor(() => expect(view.result.current.canSave).toBe(true));
    await act(async () => view.result.current.save());

    expect(posted[0]?.payload.profile_revision).toBe(1);
  });

  it("recovers only the original command after a lost POST reply", async () => {
    installSubtaskReads();
    let posts = 0;
    const statusIds: string[] = [];
    server.use(
      http.post("/api/v1/commands", () => {
        posts += 1;
        return HttpResponse.error();
      }),
      http.get(/\/api\/v1\/workspaces\/.*\/commands\/.*/, ({ request }) => {
        statusIds.push(new URL(request.url).pathname.split("/").at(-1) ?? "");
        return HttpResponse.json({
          command_id: statusIds.at(-1),
          command_type: "SetSubtaskAssignee",
          target_type: "task",
          target_id: SUBTASK_ID,
          expected_version: 7,
          status: "pending",
          result: null,
        });
      }),
    );
    const randomUUID = vi.spyOn(crypto, "randomUUID");

    const first = appointment();
    await waitFor(() => expect(first.result.current.review).not.toBeNull());
    act(() => first.result.current.onChoiceChange({
      kind: "profile",
      id: PROFILE_REF.profile_id,
      profileRef: PROFILE_REF,
    }));
    await waitFor(() => expect(first.result.current.canSave).toBe(true));
    await act(async () => first.result.current.save());
    expect(first.result.current.status).toBe("unknown");
    expect(first.result.current.canEditChoice).toBe(false);
    expect(first.result.current.canLookupOriginal).toBe(true);
    const original = first.result.current.original;
    expect(original).not.toBeNull();
    const identityCalls = randomUUID.mock.calls.length;
    first.unmount();

    const second = appointment();
    await waitFor(() => expect(second.result.current.original).toEqual(original));
    expect(posts).toBe(1);
    expect(statusIds).toEqual([]);
    await act(async () => second.result.current.lookupOriginal());

    expect(posts).toBe(1);
    expect(statusIds).toEqual([original?.command_id]);
    expect(second.result.current.status).toBe("unknown");
    expect(randomUUID).toHaveBeenCalledTimes(identityCalls);
  });

  it.each(["missing", "unknown", "other"] as const)(
    "keeps the original unresolved for %s status and accepts a later explicit applied read",
    async (invalidStatus) => {
      installSubtaskReads();
      let posts = 0;
      const originalCommands: CommandRequest[] = [];
      let statusMode: "missing" | "unknown" | "other" | "applied" = invalidStatus;
      let canonicalBinding: AgentBindingView | null = binding;
      let currentTask = task;
      const statusIds: string[] = [];
      server.use(
        http.get(`${BASE}/assignments/subtask/${SUBTASK_ID}`, () => HttpResponse.json(canonicalBinding)),
        http.get(`/api/v1/tasks/${SUBTASK_ID}`, () => HttpResponse.json(currentTask)),
        http.post("/api/v1/commands", async ({ request }) => {
          posts += 1;
          originalCommands.push(await request.json() as CommandRequest);
          return HttpResponse.error();
        }),
        http.get(/\/api\/v1\/workspaces\/.*\/commands\/.*/, ({ request }) => {
          const commandId = new URL(request.url).pathname.split("/").at(-1) ?? "";
          statusIds.push(commandId);
          const response: Record<string, unknown> = {
            command_id: commandId,
            command_type: "SetSubtaskAssignee",
            target_type: "task",
            target_id: SUBTASK_ID,
            expected_version: 7,
            result: null,
          };
          if (statusMode !== "missing") response.status = statusMode === "other" ? "unknown_state" : statusMode;
          const originalCommand = originalCommands[0];
          if (statusMode === "applied" && originalCommand !== undefined) {
            canonicalBinding = profileBinding(3);
            currentTask = { ...task, version: 8 };
            response.result = {
              command_id: originalCommand.command_id,
              aggregate_id: SUBTASK_ID,
              aggregate_version: 8,
              event_ids: [],
            } satisfies CommandResponse;
          }
          return HttpResponse.json(response);
        }),
      );

      const randomUUID = vi.spyOn(crypto, "randomUUID");
      const view = appointment();
      await waitFor(() => expect(view.result.current.review).not.toBeNull());
      const selected = {
        kind: "profile",
        id: PROFILE_REF.profile_id,
        profileRef: PROFILE_REF,
      } satisfies AssigneeValue;
      act(() => view.result.current.onChoiceChange(selected));
      await waitFor(() => expect(view.result.current.canSave).toBe(true));
      await act(async () => view.result.current.save());

      const original = view.result.current.original;
      expect(view.result.current.status).toBe("unknown");
      expect(view.result.current.choice).toEqual(selected);
      expect(view.result.current.canSave).toBe(false);
      expect(view.result.current.canLookupOriginal).toBe(true);
      expect(original).not.toBeNull();
      expect(posts).toBe(1);
      const identityCalls = randomUUID.mock.calls.length;

      await act(async () => view.result.current.lookupOriginal());
      expect(view.result.current.status).toBe("unknown");
      expect(view.result.current.original).toEqual(original);
      expect(view.result.current.canSave).toBe(false);
      expect(view.result.current.canLookupOriginal).toBe(true);
      expect(statusIds).toEqual([original?.command_id]);
      expect(posts).toBe(1);

      statusMode = "applied";
      await act(async () => view.result.current.lookupOriginal());
      await waitFor(() => expect(view.result.current.saved).toBe(true));
      expect(view.result.current.status).toBe("applied");
      expect(view.result.current.original).toEqual(original);
      expect(statusIds).toEqual([original?.command_id, original?.command_id]);
      expect(posts).toBe(1);
      expect(randomUUID).toHaveBeenCalledTimes(identityCalls);
    },
  );

  it("retains the exact selected owner and requires a fresh review after known rejection", async () => {
    installSubtaskReads();
    let original: CommandRequest | null = null;
    let posts = 0;
    server.use(
      http.post("/api/v1/commands", async ({ request }) => {
        posts += 1;
        original = await request.json() as CommandRequest;
        return HttpResponse.error();
      }),
      http.get(/\/api\/v1\/workspaces\/.*\/commands\/.*/, ({ request }) => HttpResponse.json({
        command_id: new URL(request.url).pathname.split("/").at(-1),
        command_type: "SetSubtaskAssignee",
        target_type: "task",
        target_id: SUBTASK_ID,
        expected_version: 7,
        status: "rejected",
        error_code: "version_conflict",
        result: null,
      })),
    );
    const view = appointment();
    await waitFor(() => expect(view.result.current.review).not.toBeNull());
    const selected = { kind: "profile", id: PROFILE_REF.profile_id, profileRef: PROFILE_REF } as const;
    act(() => view.result.current.onChoiceChange(selected));
    await waitFor(() => expect(view.result.current.canSave).toBe(true));
    await act(async () => view.result.current.save());
    await act(async () => view.result.current.lookupOriginal());

    expect(view.result.current.status).toBe("rejected");
    expect(view.result.current.choice).toEqual(selected);
    expect(view.result.current.error).not.toContain("PRIVATE");
    expect(view.result.current.canSave).toBe(false);
    expect(original).not.toBeNull();
    expect(posts).toBe(1);
    const freshTask = { ...task, version: 8 };
    server.use(http.get(`/api/v1/tasks/${SUBTASK_ID}`, () => HttpResponse.json(freshTask)));
    await act(async () => view.result.current.revisit());
    await waitFor(() => expect(view.result.current.review?.aggregateVersion).toBe(8));
    expect(view.result.current.choice).toEqual(selected);
    expect(view.result.current.original).toBeNull();
  });

  it("does not post when local storage cannot persist the frozen original", async () => {
    installSubtaskReads();
    const nativeSetItem = Storage.prototype.setItem;
    let writes = 0;
    const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (this: Storage, key, value) {
      writes += 1;
      if (writes >= 3) throw new DOMException("quota", "QuotaExceededError");
      nativeSetItem.call(this, key, value);
    });
    let posts = 0;
    server.use(http.post("/api/v1/commands", () => {
      posts += 1;
      return HttpResponse.json({});
    }));
    const view = appointment();
    await waitFor(() => expect(view.result.current.review).not.toBeNull());
    act(() => view.result.current.onChoiceChange({
      kind: "profile",
      id: PROFILE_REF.profile_id,
      profileRef: PROFILE_REF,
    }));
    await waitFor(() => expect(view.result.current.canSave).toBe(true));
    await act(async () => view.result.current.save());
    await waitFor(() => expect(view.result.current.storageError).not.toBeNull());
    expect(view.result.current.canSave).toBe(false);
    expect(view.result.current.canEditChoice).toBe(false);
    expect(posts).toBe(0);
    expect(writes).toBeGreaterThanOrEqual(3);
    expect(view.result.current.error).not.toContain("quota");
    setItem.mockRestore();
  });

  it("does not clear another target's busy state when the first route resolves late", async () => {
    const secondTask: TaskView = { ...task, id: OTHER_TASK_ID, version: 4 };
    const secondBinding: AgentBindingView = { ...binding, target_id: OTHER_TASK_ID };
    installSubtaskReads();
    server.use(
      http.get(`${BASE}/assignments/subtask/${OTHER_TASK_ID}`, () => HttpResponse.json(secondBinding)),
      http.get(`/api/v1/tasks/${OTHER_TASK_ID}`, () => HttpResponse.json(secondTask)),
    );
    let releaseA: (() => void) | undefined;
    let firstPosts = 0;
    const refreshA = vi.fn(async () => Promise.resolve());
    const refreshB = vi.fn(async () => Promise.resolve());
    server.use(http.post("/api/v1/commands", async ({ request }) => {
      const command = await request.json() as CommandRequest;
      const aggregateId = command.target_id;
      if (aggregateId === null) throw new Error("Expected an appointment target");
      if (command.target_id === SUBTASK_ID) {
        firstPosts += 1;
        await new Promise<void>((resolve) => { releaseA = resolve; });
      }
      return HttpResponse.json({
        command_id: command.command_id,
        aggregate_id: aggregateId,
        aggregate_version: (command.expected_version ?? 0) + 1,
        event_ids: [],
      } satisfies CommandResponse);
    }));
    const query = wrapper();
    const view = renderHook(({ targetId, onRefresh }) => useAssigneeAppointment({
      workspaceId: WORKSPACE_ID,
      targetKind: "subtask",
      targetId,
      onRefresh,
    }), {
      wrapper: query.Provider,
      initialProps: { targetId: SUBTASK_ID, onRefresh: refreshA },
    });
    await waitFor(() => expect(view.result.current.review).not.toBeNull());
    act(() => view.result.current.onChoiceChange({
      kind: "profile",
      id: PROFILE_REF.profile_id,
      profileRef: PROFILE_REF,
    }));
    await waitFor(() => expect(view.result.current.canSave).toBe(true));
    let saveA: Promise<void> | undefined;
    act(() => { saveA = view.result.current.save(); });
    await waitFor(() => expect(firstPosts).toBe(1));
    expect(view.result.current.canEditChoice).toBe(false);

    view.rerender({ targetId: OTHER_TASK_ID, onRefresh: refreshB });
    await waitFor(() => expect(view.result.current.review?.aggregateVersion).toBe(4));
    expect(view.result.current.busy).toBe(false);
    releaseA?.();
    await act(async () => saveA);

    expect(refreshA).not.toHaveBeenCalled();
    expect(refreshB).not.toHaveBeenCalled();
    expect(view.result.current.busy).toBe(false);
  });

  it("does not permit choice editing under an expired local session", async () => {
    installSubtaskReads({ session: { ...SESSION, expires_at: "2000-01-01T00:00:00Z" } });
    const view = appointment();
    await waitFor(() => expect(view.result.current.review).not.toBeNull());
    expect(view.result.current.sessionAuthorized).toBe(false);
    expect(view.result.current.canEditChoice).toBe(false);
    expect(view.result.current.canSave).toBe(false);
  });
});
