import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import { listProfiles, readProfile } from "@/features/agents/api";
import type { ProfileView } from "@/features/agents/types";
import { readLocalSession, type LocalSession } from "@/features/local-session/api";
import { getTask, taskQueryKey } from "@/features/tasks/api";
import type { TaskView } from "@/features/tasks/types";
import { workerWorkspaceQueryKey } from "@/features/worker-items/api";
import type { WorkerWorkspace } from "@/features/worker-items/types";
import {
  assignmentQueryKey,
  nativeWorkerContextQueryKey,
  readAssignment,
  readNativeWorkerContext,
} from "./api";
import { buildNativeCommand, lookupNativeCommand, submitNativeCommand } from "./commands";
import {
  appointmentDraftKey,
  loadAppointmentDraft,
  reviewAppointmentFacts,
  saveAppointmentDraft,
  type AppointmentDraft,
  type AppointmentReview,
  type AppointmentReviewResult,
  type AppointmentScope,
} from "./assigneeAppointmentState";
import type {
  AgentBindingView,
  AssigneeValue,
  CommandRequest,
  CommandResponse,
  NativeWorkerContextView,
} from "./types";

export interface UseAssigneeAppointmentOptions {
  workspaceId: string;
  targetKind: "subtask" | "worker_item";
  targetId: string | null;
  workerWorkspace?: WorkerWorkspace;
  onRefresh: () => Promise<void>;
}

type DraftEntry = {
  loaded: boolean;
  draft: AppointmentDraft | null;
  storageError: string | null;
  storageFailure: "read" | "write" | null;
};

type AppointmentStatus = AppointmentDraft["status"] | "unavailable";

const EMPTY_DRAFT = (scope: AppointmentScope): AppointmentDraft => ({
  schema_version: 1,
  scope,
  review: null,
  choice: null,
  original: null,
  status: "idle",
  rejectionCode: null,
});

const UNKNOWN_REJECTION_MESSAGE = "归属设置被拒绝，请重新读取事实";
const REJECTION_MESSAGES: Record<string, string> = {
  version_conflict: "归属事实已变化，请重新读取并复核",
  handoff_required: "执行仍在运行，需要先完成执行切换",
  assignment_forbidden: "当前会话不能设置此归属",
  validation_error: "归属设置未通过验证",
  unknown: UNKNOWN_REJECTION_MESSAGE,
};

export function useAssigneeAppointment({
  workspaceId,
  targetKind,
  targetId,
  workerWorkspace,
  onRefresh,
}: UseAssigneeAppointmentOptions) {
  const queryClient = useQueryClient();
  const normalizedTargetId = targetId?.trim() || null;
  const sessionQueryKey = [
    "appointment-session",
    workspaceId,
    targetKind,
    normalizedTargetId ?? "",
  ] as const;
  const sessionQuery = useQuery({
    queryKey: sessionQueryKey,
    queryFn: ({ signal }) => readLocalSession(signal),
    enabled: workspaceId.length > 0 && normalizedTargetId !== null,
    retry: false,
  });
  const session = sessionQuery.data ?? null;
  const sessionHasIdentity = session !== null
    && session.workspace_id === workspaceId
    && session.session_id.trim().length > 0
    && session.principal_id.trim().length > 0;
  const scope: AppointmentScope | null = sessionHasIdentity && normalizedTargetId !== null
    ? {
      workspace_id: workspaceId,
      principal_id: session.principal_id,
      target_kind: targetKind,
      target_id: normalizedTargetId,
    }
    : null;
  const scopeKey = scope === null ? null : appointmentDraftKey(scope);
  const activeScopeKeyRef = useRef<string | null>(scopeKey);
  useLayoutEffect(() => {
    activeScopeKeyRef.current = scopeKey;
  }, [scopeKey]);
  const authorized = scope !== null && isAuthorizedSession(session, workspaceId);

  const [draftEntries, setDraftEntries] = useState<Record<string, DraftEntry>>({});
  const draftEntriesRef = useRef<Record<string, DraftEntry>>({});
  const [busyKeys, setBusyKeys] = useState<Record<string, true>>({});
  const busyKeysRef = useRef(new Set<string>());
  const [messages, setMessages] = useState<Record<string, string>>({});
  const busy = scopeKey !== null && busyKeys[scopeKey] === true;
  const draftEntry = scopeKey === null ? undefined : draftEntries[scopeKey];
  const draft = draftEntry?.draft ?? null;

  const scopePrincipal = scope?.principal_id ?? "";
  const assignmentKey = scope === null
    ? ["agent-assignment", workspaceId, targetKind, normalizedTargetId ?? "", ""] as const
    : [...assignmentQueryKey(workspaceId, targetKind, scope.target_id), scope.principal_id] as const;
  const assignmentQuery = useQuery({
    queryKey: assignmentKey,
    queryFn: ({ signal }) => readAssignment(workspaceId, targetKind, normalizedTargetId ?? "", signal),
    enabled: scope !== null,
    retry: false,
  });
  const taskKey = scope?.target_kind === "subtask"
    ? [...taskQueryKey(workspaceId, scope.target_id), scopePrincipal] as const
    : ["task", workspaceId, normalizedTargetId ?? "", ""] as const;
  const taskQuery = useQuery({
    queryKey: taskKey,
    queryFn: ({ signal }) => getTask(workspaceId, normalizedTargetId ?? "", signal),
    enabled: scope?.target_kind === "subtask",
    retry: false,
  });
  const nativeContextKey = scope?.target_kind === "worker_item"
    ? [...nativeWorkerContextQueryKey(workspaceId, scope.target_id), scopePrincipal] as const
    : ["native-worker-context", workspaceId, normalizedTargetId ?? "", ""] as const;
  const nativeContextQuery = useQuery({
    queryKey: nativeContextKey,
    queryFn: ({ signal }) => readNativeWorkerContext(workspaceId, normalizedTargetId ?? "", signal),
    enabled: scope?.target_kind === "worker_item",
    retry: false,
  });
  const profilesKey = scope === null
    ? ["appointment-profiles", workspaceId, targetKind, normalizedTargetId ?? "", ""] as const
    : ["appointment-profiles", scope.workspace_id, scope.principal_id, scope.target_kind, scope.target_id] as const;
  const profilesQuery = useQuery({
    queryKey: profilesKey,
    queryFn: ({ signal }) => listProfiles(workspaceId, signal),
    enabled: scope !== null && authorized,
    retry: false,
  });

  const selectedProfileRef = draft?.choice?.kind === "profile" ? draft.choice.profileRef : null;
  const selectedProfileKey = scope !== null && selectedProfileRef !== null
    ? ["appointment-profile", scope.workspace_id, scope.principal_id, scope.target_kind,
      scope.target_id, selectedProfileRef.profile_id, selectedProfileRef.revision] as const
    : ["appointment-profile", workspaceId, scopePrincipal, targetKind, normalizedTargetId ?? "", "", 0] as const;
  const selectedProfileQuery = useQuery({
    queryKey: selectedProfileKey,
    queryFn: ({ signal }) => {
      if (selectedProfileRef === null) throw new Error("Selected Profile is unavailable.");
      return readProfile(workspaceId, selectedProfileRef.profile_id, selectedProfileRef.revision, signal);
    },
    enabled: scope !== null && authorized && selectedProfileRef !== null,
    retry: false,
  });

  const factsReady = scope !== null && assignmentQuery.isSuccess
    && (scope.target_kind === "subtask" ? taskQuery.isSuccess : nativeContextQuery.isSuccess);
  const currentReviewResult: AppointmentReviewResult | null = factsReady && scope !== null
    ? reviewAppointmentFacts(scope.target_kind === "subtask"
      ? {
        scope,
        binding: assignmentQuery.data,
        ...(taskQuery.data === undefined ? {} : { task: taskQuery.data }),
      }
      : {
        scope,
        binding: assignmentQuery.data,
        ...(workerWorkspace === undefined ? {} : { workerWorkspace }),
        ...(nativeContextQuery.data === undefined ? {} : { nativeContext: nativeContextQuery.data }),
      })
    : null;
  const canEditChoice = scope !== null && authorized
    && draftEntry?.loaded === true && draftEntry.storageError === null
    && draft !== null && draft.status === "idle" && draft.original === null
    && draft.review !== null && currentReviewResult?.ok === true && !busy;

  function putDraftEntry(key: string, entry: DraftEntry) {
    const next = { ...draftEntriesRef.current, [key]: entry };
    draftEntriesRef.current = next;
    setDraftEntries(next);
  }

  function putBusy(key: string, value: boolean) {
    if (value) busyKeysRef.current.add(key);
    else busyKeysRef.current.delete(key);
    setBusyKeys((previous) => {
      const next = { ...previous };
      if (value) next[key] = true;
      else delete next[key];
      return next;
    });
  }

  function setMessage(key: string, message: string | null) {
    setMessages((previous) => {
      if (previous[key] === message) return previous;
      const next = { ...previous };
      if (message === null) delete next[key];
      else next[key] = message;
      return next;
    });
  }

  function writeDraft(
    targetScope: AppointmentScope,
    key: string,
    nextDraft: AppointmentDraft,
    updateOnStorageFailure: boolean,
  ): boolean {
    const saved = saveAppointmentDraft(targetScope, nextDraft);
    if (!saved.ok) {
      const previous = draftEntriesRef.current[key];
      putDraftEntry(key, {
        loaded: true,
        draft: updateOnStorageFailure ? nextDraft : previous?.draft ?? null,
        storageError: saved.reason,
        storageFailure: "write",
      });
      return false;
    }
    putDraftEntry(key, {
      loaded: true,
      draft: nextDraft,
      storageError: null,
      storageFailure: null,
    });
    return true;
  }

  useEffect(() => {
    if (scope === null || scopeKey === null) return;
    if (draftEntriesRef.current[scopeKey]?.loaded === true) return;
    const loaded = loadAppointmentDraft(scope);
    if (!loaded.ok) {
      putDraftEntry(scopeKey, {
        loaded: true,
        draft: null,
        storageError: loaded.reason,
        storageFailure: "read",
      });
      return;
    }
    putDraftEntry(scopeKey, {
      loaded: true,
      draft: loaded.draft ?? EMPTY_DRAFT(scope),
      storageError: null,
      storageFailure: null,
    });
  }, [scope, scopeKey]);

  useEffect(() => {
    if (scope === null || scopeKey === null || draftEntry?.loaded !== true || draft === null
      || draftEntry.storageError !== null || draft.review !== null || draft.original !== null || draft.status !== "idle"
      || currentReviewResult?.ok !== true || assignmentQuery.isSuccess !== true) {
      return;
    }
    const next: AppointmentDraft = {
      ...draft,
      review: currentReviewResult.review,
      choice: draft.choice ?? choiceFromBinding(assignmentQuery.data),
    };
    writeDraft(scope, scopeKey, next, false);
  }, [
    scope,
    scopeKey,
    draftEntry?.loaded,
    draftEntry?.storageError,
    draft,
    currentReviewResult,
    assignmentQuery.isSuccess,
    assignmentQuery.data,
    writeDraft,
  ]);

  function retryDraftStorage() {
    if (scope === null || scopeKey === null || draftEntry === undefined) return;
    if (draftEntry.storageFailure === "write" && draftEntry.draft !== null) {
      writeDraft(scope, scopeKey, draftEntry.draft, true);
      return;
    }
    const loaded = loadAppointmentDraft(scope);
    if (!loaded.ok) {
      putDraftEntry(scopeKey, {
        loaded: true,
        draft: null,
        storageError: loaded.reason,
        storageFailure: "read",
      });
      return;
    }
    putDraftEntry(scopeKey, {
      loaded: true,
      draft: loaded.draft ?? EMPTY_DRAFT(scope),
      storageError: null,
      storageFailure: null,
    });
  }

  function onChoiceChange(choice: AssigneeValue | null) {
    if (!canEditChoice || scope === null || scopeKey === null || draft === null
      || busyKeysRef.current.has(scopeKey) || !validUserChoice(choice, scope)) return;
    const nextChoice = cloneChoice(choice);
    const next = { ...draft, choice: nextChoice };
    writeDraft(scope, scopeKey, next, true);
    setMessage(scopeKey, null);
  }

  const onSessionAuthorized = (value: LocalSession) => {
    if (value.workspace_id !== workspaceId || value.session_id.trim().length === 0
      || value.principal_id.trim().length === 0) return;
    queryClient.setQueryData(sessionQueryKey, value);
  };

  function isCurrentScope(key: string): boolean {
    return activeScopeKeyRef.current === key;
  }

  async function invalidateFacts(targetScope: AppointmentScope, key: string) {
    const principal = targetScope.principal_id;
    const keys: Array<readonly unknown[]> = [
      [...assignmentQueryKey(targetScope.workspace_id, targetScope.target_kind, targetScope.target_id), principal],
    ];
    if (targetScope.target_kind === "subtask") {
      keys.push([...taskQueryKey(targetScope.workspace_id, targetScope.target_id), principal]);
    } else {
      keys.push([...nativeWorkerContextQueryKey(targetScope.workspace_id, targetScope.target_id), principal]);
    }
    await Promise.all(keys.map((queryKey) => queryClient.invalidateQueries({ queryKey, exact: true })));
    if (isCurrentScope(key)) await onRefresh();
  }

  async function refreshFactsForReview(targetScope: AppointmentScope, key: string) {
    if (!isCurrentScope(key)) return null;
    if (isAuthorizedSession(queryClient.getQueryData<LocalSession>(sessionQueryKey), workspaceId) !== true) {
      return null;
    }
    await onRefresh();
    if (!isCurrentScope(key)) return null;
    const assignmentResult = await assignmentQuery.refetch({ throwOnError: true });
    if (assignmentResult.status !== "success") return null;
    if (targetScope.target_kind === "subtask") {
      const taskResult = await taskQuery.refetch({ throwOnError: true });
      if (taskResult.status !== "success") return null;
      return reviewAppointmentFacts({
        scope: targetScope,
        binding: assignmentResult.data,
        task: taskResult.data,
      });
    }
    const nativeResult = await nativeContextQuery.refetch({ throwOnError: true });
    if (nativeResult.status !== "success") return null;
    const refreshedWorkspace = queryClient.getQueryData<WorkerWorkspace>(
      workerWorkspaceQueryKey(targetScope.workspace_id, targetScope.target_id),
    ) ?? workerWorkspace;
    if (refreshedWorkspace === undefined) return null;
    return reviewAppointmentFacts({
      scope: targetScope,
      binding: assignmentResult.data,
      workerWorkspace: refreshedWorkspace,
      nativeContext: nativeResult.data,
    });
  }

  async function revisit() {
    if (scope === null || scopeKey === null || !authorized || busyKeysRef.current.has(scopeKey)) return;
    const capturedScope = scope;
    const capturedKey = scopeKey;
    const existing = draftEntriesRef.current[capturedKey]?.draft;
    if (existing === null || existing === undefined) return;
    if (existing.status === "pending" || existing.status === "unknown") {
      setMessage(capturedKey, "请先查询这条原始操作");
      return;
    }
    putBusy(capturedKey, true);
    setMessage(capturedKey, null);
    try {
      const reviewed = await refreshFactsForReview(capturedScope, capturedKey);
      if (!isCurrentScope(capturedKey)) return;
      if (reviewed === null || !reviewed.ok) {
        setMessage(capturedKey, reviewed?.reason ?? "归属事实暂不可用，请重试读取");
        return;
      }
      const latest = draftEntriesRef.current[capturedKey]?.draft;
      if (latest === null || latest === undefined) return;
      if (latest.status === "applied" && latest.original !== null
        && !bindingConfirmsPayload(
          latest.original.payload,
          queryClient.getQueryData<AgentBindingView | null>(assignmentKey),
          capturedScope,
          latest.review,
        )) {
        setMessage(capturedKey, "原始操作已处理，但当前归属尚未确认");
        return;
      }
      const next: AppointmentDraft = {
        ...latest,
        review: reviewed.review,
        original: null,
        status: "idle",
        rejectionCode: null,
      };
      writeDraft(capturedScope, capturedKey, next, true);
    } catch {
      if (isCurrentScope(capturedKey)) setMessage(capturedKey, "归属事实暂不可用，请重试读取");
    } finally {
      putBusy(capturedKey, false);
    }
  }

  async function save() {
    if (scope === null || scopeKey === null || draft === null || !authorized
      || busyKeysRef.current.has(scopeKey)) return;
    const capturedScope = scope;
    const capturedKey = scopeKey;
    const currentDraft = draftEntriesRef.current[capturedKey]?.draft;
    if (currentDraft === null || currentDraft === undefined || currentDraft.status !== "idle"
      || currentDraft.original !== null || currentDraft.review === null
      || currentDraft.choice === null || currentReviewResult?.ok !== true
      || currentReviewResult.action !== "appointment"
      || !sameReview(currentDraft.review, currentReviewResult.review)
      || assignmentQuery.isSuccess !== true) return;
    const choice = currentDraft.choice;
    if (!validUserChoice(choice, capturedScope)
      || choiceMatchesBinding(choice, assignmentQuery.data)) return;
    putBusy(capturedKey, true);
    setMessage(capturedKey, null);
    try {
      if (choice.kind === "profile") {
        const exactProfile = await readProfile(
          capturedScope.workspace_id,
          choice.id,
          choice.profileRef.revision,
        );
        if (!isCurrentScope(capturedKey)) return;
        if (!profileIsExactAndEnabled(exactProfile, choice, capturedScope)) {
          setMessage(capturedKey, "所选 Profile 版本不可用");
          return;
        }
      }

      const latestReview = currentFactsFromCache({
        scope: capturedScope,
        assignmentKey,
        taskKey,
        nativeContextKey,
        workerWorkspace,
        queryClient,
      });
      if (latestReview === null || !latestReview.ok || latestReview.action !== "appointment"
        || !sameReview(currentDraft.review, latestReview.review)) {
        if (isCurrentScope(capturedKey)) setMessage(capturedKey, "归属事实已变化，请重新读取并复核");
        return;
      }
      const latestDraft = draftEntriesRef.current[capturedKey]?.draft;
      if (latestDraft === null || latestDraft === undefined || latestDraft.status !== "idle"
        || latestDraft.choice === null || !sameChoice(latestDraft.choice, choice)) return;
      const payload = {
        assignee_kind: choice.kind,
        assignee_id: choice.id,
        profile_revision: choice.kind === "profile" ? choice.profileRef.revision : null,
      };
      const original = buildNativeCommand({
        session: session as LocalSession,
        workspaceId: capturedScope.workspace_id,
        commandType: capturedScope.target_kind === "subtask"
          ? "SetSubtaskAssignee"
          : "SetWorkerItemAssignee",
        targetType: capturedScope.target_kind === "subtask" ? "task" : "worker_item",
        targetId: capturedScope.target_id,
        expectedVersion: currentDraft.review.aggregateVersion,
        payload,
      });
      const pending: AppointmentDraft = {
        ...latestDraft,
        original,
        status: "pending",
        rejectionCode: null,
      };
      if (!writeDraft(capturedScope, capturedKey, pending, false)) {
        setMessage(capturedKey, "原始操作未能保存，因此没有提交");
        return;
      }
      if (!isCurrentScope(capturedKey)) return;
      try {
        const response = await submitNativeCommand(original);
        if (!isCurrentScope(capturedKey)) return;
        if (!commandResultMatches(response, original, capturedScope)) {
          setMessage(capturedKey, "提交结果尚未确认，请查询原始操作");
          writeDraft(capturedScope, capturedKey, { ...pending, status: "unknown" }, true);
          return;
        }
        writeDraft(capturedScope, capturedKey, { ...pending, status: "applied" }, true);
      } catch {
        if (!isCurrentScope(capturedKey)) return;
        const unknown: AppointmentDraft = { ...pending, status: "unknown" };
        writeDraft(capturedScope, capturedKey, unknown, true);
        setMessage(capturedKey, "提交结果尚未确认，可查询原始操作");
        return;
      }
      try {
        await invalidateFacts(capturedScope, capturedKey);
      } catch {
        if (isCurrentScope(capturedKey)) setMessage(capturedKey, "归属已处理，当前事实刷新暂不可用");
      }
    } catch {
      if (isCurrentScope(capturedKey)) setMessage(capturedKey, "所选 Profile 版本暂不可用");
    } finally {
      putBusy(capturedKey, false);
    }
  }

  async function lookupOriginal() {
    if (scope === null || scopeKey === null || !authorized || busyKeysRef.current.has(scopeKey)) return;
    const capturedScope = scope;
    const capturedKey = scopeKey;
    const currentDraft = draftEntriesRef.current[capturedKey]?.draft;
    const original = currentDraft?.original;
    if (currentDraft === null || currentDraft === undefined || original === null || original === undefined) return;
    putBusy(capturedKey, true);
    setMessage(capturedKey, null);
    try {
      const status = await lookupNativeCommand(original);
      if (!isCurrentScope(capturedKey)) return;
      if (!statusMatchesOriginal(status, original, capturedScope)) {
        writeDraft(capturedScope, capturedKey, { ...currentDraft, status: "unknown", rejectionCode: null }, true);
        setMessage(capturedKey, "原始操作状态尚未确认");
        return;
      }
      if (status.status === "pending") {
        const unresolved: AppointmentDraft = {
          ...currentDraft,
          status: currentDraft.status === "pending" ? "pending" : "unknown",
          rejectionCode: null,
        };
        writeDraft(capturedScope, capturedKey, unresolved, true);
        setMessage(capturedKey, "原始操作仍在处理中");
        return;
      }
      if (status.status === "rejected") {
        const errorCode = status.error_code ?? "";
        const rejectionCode = Object.prototype.hasOwnProperty.call(REJECTION_MESSAGES, errorCode)
          ? errorCode
          : "unknown";
        const rejected: AppointmentDraft = { ...currentDraft, status: "rejected", rejectionCode };
        writeDraft(capturedScope, capturedKey, rejected, true);
        setMessage(capturedKey, REJECTION_MESSAGES[rejectionCode] ?? UNKNOWN_REJECTION_MESSAGE);
        return;
      }
      if (status.result !== null && status.result !== undefined
        && !commandResultMatches(status.result, original, capturedScope)) {
        writeDraft(capturedScope, capturedKey, { ...currentDraft, status: "unknown", rejectionCode: null }, true);
        setMessage(capturedKey, "原始操作结果尚未确认");
        return;
      }
      const applied: AppointmentDraft = { ...currentDraft, status: "applied", rejectionCode: null };
      writeDraft(capturedScope, capturedKey, applied, true);
      try {
        await invalidateFacts(capturedScope, capturedKey);
      } catch {
        if (isCurrentScope(capturedKey)) setMessage(capturedKey, "归属已处理，当前事实刷新暂不可用");
      }
    } catch {
      if (!isCurrentScope(capturedKey)) return;
      const latest = draftEntriesRef.current[capturedKey]?.draft ?? currentDraft;
      writeDraft(capturedScope, capturedKey, { ...latest, status: "unknown", rejectionCode: null }, true);
      setMessage(capturedKey, "原始操作状态尚未确认，请重试读取");
    } finally {
      putBusy(capturedKey, false);
    }
  }

  const currentBinding = assignmentQuery.isSuccess ? assignmentQuery.data : undefined;
  const selectedProfile = selectedProfileQuery.data;
  const profiles = useMemo(() => {
    const items = profilesQuery.data?.items ?? [];
    if (selectedProfile === undefined || !validProfileViewIdentity(selectedProfile, workspaceId)) return items;
    if (items.some((profile) => sameProfileRef(profile.profile.ref, selectedProfile.profile.ref))) return items;
    return [...items, selectedProfile];
  }, [profilesQuery.data?.items, selectedProfile, workspaceId]);
  const currentReview = draft?.review ?? null;
  const isStale = currentReview !== null && currentReviewResult?.ok === true
    && !sameReview(currentReview, currentReviewResult.review);
  const mode = currentReviewResult === null
    ? "loading"
    : currentReviewResult.ok
      ? "native"
      : currentReviewResult.action === "legacy" ? "legacy" : "unavailable";
  const action = currentReviewResult === null
    ? null
    : currentReviewResult.ok ? currentReviewResult.action : currentReviewResult.action;
  const original = draft?.original ?? null;
  const selectedChoice = draft?.choice ?? null;
  const storageError = draftEntry?.storageError ?? null;
  const error = scopeKey === null ? null : messages[scopeKey] ?? storageError;
  const saved = draft?.status === "applied" && draft.original !== null
    && bindingConfirmsPayload(draft.original.payload, currentBinding ?? null, scope, draft.review);
  const disabledReason = appointmentDisabledReason({
    workspaceId,
    targetKind,
    targetId: normalizedTargetId,
    scope,
    session,
    sessionLoading: sessionQuery.isPending,
    authorized,
    draftEntry,
    draft,
    assignmentLoading: assignmentQuery.isPending,
    assignmentError: assignmentQuery.isError,
    taskLoading: taskQuery.isPending,
    taskError: taskQuery.isError,
    contextLoading: nativeContextQuery.isPending,
    contextError: nativeContextQuery.isError,
    reviewResult: currentReviewResult,
    isStale,
    busy,
    choice: selectedChoice,
    binding: currentBinding,
    profilesError: profilesQuery.isError,
    selectedProfileLoading: selectedProfileQuery.isPending,
    selectedProfileError: selectedProfileQuery.isError,
    selectedProfile,
  });
  const canSave = disabledReason === null;
  const status: AppointmentStatus = draftEntry?.loaded === true
    ? draft?.status ?? "unavailable"
    : "unavailable";

  return {
    scope,
    session,
    sessionAuthorized: authorized,
    binding: currentBinding,
    execution: nativeContextQuery.data?.execution ?? null,
    nativeContext: nativeContextQuery.data,
    profiles,
    choice: selectedChoice,
    onChoiceChange,
    mode,
    action,
    review: currentReview,
    currentReviewResult,
    isStale,
    revisit,
    save,
    lookupOriginal,
    retryDraftStorage,
    original,
    status,
    busy,
    error,
    disabledReason,
    canEditChoice,
    canSave,
    saved,
    canLookupOriginal: original !== null && authorized && !busy,
    storageError,
    onSessionAuthorized,
  };
}

function appointmentDisabledReason(input: {
  workspaceId: string;
  targetKind: "subtask" | "worker_item";
  targetId: string | null;
  scope: AppointmentScope | null;
  session: LocalSession | null;
  sessionLoading: boolean;
  authorized: boolean;
  draftEntry: DraftEntry | undefined;
  draft: AppointmentDraft | null;
  assignmentLoading: boolean;
  assignmentError: boolean;
  taskLoading: boolean;
  taskError: boolean;
  contextLoading: boolean;
  contextError: boolean;
  reviewResult: AppointmentReviewResult | null;
  isStale: boolean;
  busy: boolean;
  choice: AssigneeValue | null;
  binding: AgentBindingView | null | undefined;
  profilesError: boolean;
  selectedProfileLoading: boolean;
  selectedProfileError: boolean;
  selectedProfile: ProfileView | undefined;
}): string | null {
  if (input.targetId === null) return "没有可设置归属的实体子任务";
  if (input.sessionLoading) return "正在读取当前用户会话";
  if (input.scope === null || input.session === null) return "需要当前用户会话";
  if (!input.authorized) return "当前用户会话已失效或不属于此工作区";
  if (input.draftEntry?.loaded !== true) return "正在读取本地归属记录";
  if (input.draftEntry.storageError !== null) return input.draftEntry.storageError;
  if (input.assignmentLoading || input.targetKind === "subtask" && input.taskLoading
    || input.targetKind === "worker_item" && input.contextLoading) return "正在读取归属事实";
  if (input.assignmentError || input.targetKind === "subtask" && input.taskError
    || input.targetKind === "worker_item" && input.contextError) return "归属事实暂不可用";
  if (input.reviewResult === null) return "归属事实暂不可用";
  if (!input.reviewResult.ok) return input.reviewResult.reason;
  if (input.reviewResult.action === "handoff_required") return "执行仍在运行，需要先完成执行切换";
  if (input.busy) return "归属操作处理中";
  if (input.draft === null || input.draft.review === null) return "尚未复核归属事实";
  if (input.draft.status === "pending" || input.draft.status === "unknown") return "请先查询这条原始操作";
  if (input.draft.status === "applied") return "原始操作已处理，请重新读取归属事实";
  if (input.draft.status === "rejected") return "请重新读取并复核后再提交";
  if (input.isStale) return "归属事实已变化，请重新读取并复核";
  if (!sameReview(input.draft.review, input.reviewResult.review)) return "归属事实已变化，请重新读取并复核";
  if (input.choice === null) return "请选择当前用户或可用 Profile";
  if (input.choice.kind === "human" && input.choice.id !== input.scope.principal_id) {
    return "已保存的用户归属不可直接提交";
  }
  if (input.choice.kind === "profile") {
    if (input.choice.profileRef.workspace_id !== input.workspaceId
      || input.choice.profileRef.profile_id !== input.choice.id
      || !Number.isSafeInteger(input.choice.profileRef.revision) || input.choice.profileRef.revision <= 0) {
      return "所选 Profile 版本不可用";
    }
    if (input.profilesError || input.selectedProfileError) return "无法确认所选 Profile 版本";
    if (input.selectedProfileLoading || !profileIsExactAndEnabled(input.selectedProfile, input.choice, input.scope)) {
      return "请等待所选 Profile 版本确认";
    }
  }
  if (input.binding === undefined) return "当前归属尚未确认";
  if (choiceMatchesBinding(input.choice, input.binding)) return "所选归属与当前归属相同";
  return null;
}

function currentFactsFromCache(input: {
  scope: AppointmentScope;
  assignmentKey: readonly unknown[];
  taskKey: readonly unknown[];
  nativeContextKey: readonly unknown[];
  workerWorkspace: WorkerWorkspace | undefined;
  queryClient: ReturnType<typeof useQueryClient>;
}): AppointmentReviewResult | null {
  const assignmentState = input.queryClient.getQueryState<AgentBindingView | null>(input.assignmentKey);
  if (assignmentState?.status !== "success" || assignmentState.data === undefined) return null;
  if (input.scope.target_kind === "subtask") {
    const taskState = input.queryClient.getQueryState<TaskView>(input.taskKey);
    if (taskState?.status !== "success" || taskState.data === undefined) return null;
    return reviewAppointmentFacts({ scope: input.scope, binding: assignmentState.data, task: taskState.data });
  }
  const contextState = input.queryClient.getQueryState<NativeWorkerContextView>(input.nativeContextKey);
  const workspace = input.queryClient.getQueryData<WorkerWorkspace>(
    workerWorkspaceQueryKey(input.scope.workspace_id, input.scope.target_id),
  ) ?? input.workerWorkspace;
  if (contextState?.status !== "success" || contextState.data === undefined || workspace === undefined) return null;
  return reviewAppointmentFacts({
    scope: input.scope,
    binding: assignmentState.data,
    workerWorkspace: workspace,
    nativeContext: contextState.data,
  });
}

function commandResultMatches(
  response: CommandResponse,
  original: CommandRequest,
  scope: AppointmentScope,
): boolean {
  return isRecord(response) && response.command_id === original.command_id
    && response.aggregate_id === scope.target_id
    && response.aggregate_version === (original.expected_version ?? 0) + 1;
}

function statusMatchesOriginal(
  status: Awaited<ReturnType<typeof lookupNativeCommand>>,
  original: CommandRequest,
  scope: AppointmentScope,
): boolean {
  return isRecord(status)
    && (status.status === "pending" || status.status === "applied" || status.status === "rejected")
    && status.command_id === original.command_id
    && status.command_type === original.command_type
    && status.target_type === original.target_type
    && status.target_id === scope.target_id
    && status.expected_version === original.expected_version;
}

function bindingMatchesPayload(
  payload: Record<string, unknown>,
  binding: AgentBindingView | null | undefined,
  scope: AppointmentScope | null,
): boolean {
  if (scope === null || binding === null || binding === undefined
    || binding.workspace_id !== scope.workspace_id
    || binding.target_type !== scope.target_kind || binding.target_id !== scope.target_id
    || !Number.isSafeInteger(binding.version) || binding.version <= 0) return false;
  const kind = payload.assignee_kind;
  const id = payload.assignee_id;
  const revision = payload.profile_revision;
  if (binding.assignee.kind !== kind || binding.assignee.id !== id) return false;
  if (kind === "human") return id === scope.principal_id && revision === null && binding.profile_ref === null;
  if (kind !== "profile" || typeof revision !== "number" || !Number.isSafeInteger(revision)
    || binding.profile_ref === null) return false;
  return binding.profile_ref.workspace_id === scope.workspace_id
    && binding.profile_ref.profile_id === id
    && binding.profile_ref.revision === revision;
}

function bindingConfirmsPayload(
  payload: Record<string, unknown>,
  binding: AgentBindingView | null | undefined,
  scope: AppointmentScope | null,
  review: AppointmentReview | null,
): boolean {
  const previousVersion = reviewedBindingVersion(review, scope);
  return previousVersion !== null
    && bindingMatchesPayload(payload, binding, scope)
    && binding !== null && binding !== undefined
    && binding.version > previousVersion;
}

function reviewedBindingVersion(
  review: AppointmentReview | null,
  scope: AppointmentScope | null,
): number | null {
  if (review === null || scope === null || !isPositiveSafeInteger(review.aggregateVersion)
    || typeof review.bindingFingerprint !== "string") return null;
  if (review.bindingFingerprint === "null") return 0;

  let parsed: unknown;
  try {
    parsed = JSON.parse(review.bindingFingerprint);
  } catch {
    return null;
  }
  if (!Array.isArray(parsed)) return null;
  const fields: unknown[] = parsed;
  if (fields.length !== 7 || JSON.stringify(fields) !== review.bindingFingerprint
    || fields[0] !== scope.workspace_id || fields[1] !== scope.target_kind
    || fields[2] !== scope.target_id || !isPositiveSafeInteger(fields[3])
    || (fields[4] !== "human" && fields[4] !== "profile")
    || typeof fields[5] !== "string" || fields[5].trim().length === 0) return null;

  const profileRef = fields[6];
  if (fields[4] === "human") return profileRef === null ? fields[3] : null;
  if (!Array.isArray(profileRef)) return null;
  const profileFields: unknown[] = profileRef;
  if (profileFields.length !== 3 || profileFields[0] !== scope.workspace_id
    || profileFields[1] !== fields[5] || !isPositiveSafeInteger(profileFields[2])) return null;
  return fields[3];
}

function isPositiveSafeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0;
}

function choiceMatchesBinding(
  choice: AssigneeValue,
  binding: AgentBindingView | null | undefined,
): boolean {
  if (binding === null || binding === undefined || choice.kind !== binding.assignee.kind
    || choice.id !== binding.assignee.id) return false;
  if (choice.kind === "human") return binding.profile_ref === null;
  return binding.profile_ref !== null && sameProfileRef(choice.profileRef, binding.profile_ref);
}

function choiceFromBinding(binding: AgentBindingView | null | undefined): AssigneeValue | null {
  if (binding === null || binding === undefined) return null;
  if (binding.assignee.kind === "human") {
    return { kind: "human", id: binding.assignee.id, profileRef: null };
  }
  return binding.profile_ref === null ? null : {
    kind: "profile",
    id: binding.assignee.id,
    profileRef: { ...binding.profile_ref },
  };
}

function validUserChoice(choice: AssigneeValue | null, scope: AppointmentScope): boolean {
  if (choice === null) return true;
  if (choice.kind === "human") return choice.id === scope.principal_id && choice.profileRef === null;
  return choice.kind === "profile" && choice.id.trim().length > 0
    && choice.profileRef.workspace_id === scope.workspace_id
    && choice.profileRef.profile_id === choice.id
    && Number.isSafeInteger(choice.profileRef.revision) && choice.profileRef.revision > 0;
}

function cloneChoice(choice: AssigneeValue | null): AssigneeValue | null {
  if (choice === null) return null;
  return choice.kind === "human"
    ? { kind: "human", id: choice.id, profileRef: null }
    : { kind: "profile", id: choice.id, profileRef: { ...choice.profileRef } };
}

function sameChoice(left: AssigneeValue, right: AssigneeValue): boolean {
  return left.kind === right.kind && left.id === right.id
    && (left.kind === "human" && right.kind === "human"
      || left.kind === "profile" && right.kind === "profile"
        && sameProfileRef(left.profileRef, right.profileRef));
}

function sameReview(left: AppointmentReview, right: AppointmentReview): boolean {
  return left.aggregateVersion === right.aggregateVersion
    && left.bindingFingerprint === right.bindingFingerprint
    && left.executionFingerprint === right.executionFingerprint;
}

function profileIsExactAndEnabled(
  profile: ProfileView | undefined,
  choice: Extract<AssigneeValue, { kind: "profile" }>,
  scope: AppointmentScope | null,
): boolean {
  return profile !== undefined && scope !== null
    && profile.status.enabled === true
    && profile.status.profile_id === choice.id
    && profile.profile.ref.workspace_id === scope.workspace_id
    && profile.profile.ref.profile_id === choice.id
    && profile.profile.ref.revision === choice.profileRef.revision
    && sameProfileRef(profile.profile.ref, choice.profileRef);
}

function validProfileViewIdentity(profile: ProfileView, workspaceId: string): boolean {
  return profile.profile.ref.workspace_id === workspaceId
    && profile.status.profile_id === profile.profile.ref.profile_id
    && Number.isSafeInteger(profile.profile.ref.revision)
    && profile.profile.ref.revision > 0;
}

function sameProfileRef(
  left: { workspace_id: string; profile_id: string; revision: number },
  right: { workspace_id: string; profile_id: string; revision: number },
): boolean {
  return left.workspace_id === right.workspace_id && left.profile_id === right.profile_id
    && left.revision === right.revision;
}

function isAuthorizedSession(session: LocalSession | null | undefined, workspaceId: string): boolean {
  if (session === null || session === undefined || session.workspace_id !== workspaceId
    || session.session_id.trim().length === 0 || session.principal_id.trim().length === 0) return false;
  const expiresAt = Date.parse(session.expires_at);
  return Number.isFinite(expiresAt) && expiresAt > Date.now();
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
