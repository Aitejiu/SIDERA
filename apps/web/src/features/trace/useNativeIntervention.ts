import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import { readNativeWorkerContext, nativeWorkerContextQueryKey } from "@/features/assignments/api";
import {
  buildNativeCommand,
  lookupNativeCommand,
  submitNativeCommand,
} from "@/features/assignments/commands";
import {
  type CommandRequest,
  type CommandStatusResponse,
  type NativeWorkerContextView,
} from "@/features/assignments/types";
import { readLocalSession, type LocalSession } from "@/features/local-session/api";
import { sessionObservationMatches } from "@/shared/sessionObservation";
import {
  executionQueryKey,
  executionSessionQueryKey,
  getExecution,
  getExecutionSession,
} from "./api";
import {
  loadNativeInterventionDraft,
  nativeInterventionDraftKey,
  nativeOriginalStatusMatches,
  reviewNativeInputResult,
  reviewNativeInterventionFacts,
  reviewNativeStopResult,
  saveNativeInterventionDraft,
  type NativeInterventionAction,
  type NativeInterventionDraft,
  type NativeInterventionReview,
  type NativeInterventionScope,
  type NativeInputStatus,
} from "./nativeIntervention";
import { readNativeExecutionInputPage } from "./nativeInputApi";
import { workerWorkspaceQueryKey } from "@/features/worker-items/api";
import type { WorkerWorkspace } from "@/features/worker-items/types";
import type { Execution, ExecutionSession } from "./types";

type ActionKind = "input" | "stop";
type DraftEntry = {
  loaded: boolean;
  draft: NativeInterventionDraft | null;
  storageError: string | null;
  storageFailure: "read" | "write" | null;
  stagedDraft: NativeInterventionDraft | null;
};

interface InputObservationState {
  commandId: string;
  status: NativeInputStatus | null;
  inputId: string | null;
  version: number | null;
  nextCursor: string | null;
  canReadMore: boolean;
  unavailable: boolean;
  seenCursors: string[];
}

interface FreshFacts {
  localSession: LocalSession;
  workspace: WorkerWorkspace;
  nativeContext: NativeWorkerContextView;
  execution: Execution;
  session: ExecutionSession;
  review: ReturnType<typeof reviewNativeInterventionFacts>;
}

const EMPTY_DRAFT = (scope: NativeInterventionScope): NativeInterventionDraft => ({
  schema_version: 1,
  scope,
  text: "",
  client_idempotency_key: null,
  review: null,
  input: emptyAction(),
  stop: emptyAction(),
});

const STORAGE_MESSAGE = "本地介入记录暂不可用；保存成功前不会提交。";
const SOURCE_MESSAGE = "当前 native 执行事实暂不可用，请重新读取。";
const REVIEW_MESSAGE = "执行上下文已变化，请重新读取并复核。";
const STATUS_MESSAGE = "原始操作状态尚未确认，请查询原始操作。";
const SAFE_REJECTION_CODES = new Set([
  "assignment_forbidden",
  "execution_context_changed",
  "input_closed",
  "permission_denied",
  "session_not_active",
  "stop_not_supported",
  "validation_error",
  "version_conflict",
]);
const NATIVE_INPUT_STATUSES = new Set<NativeInputStatus>([
  "accepted",
  "queued",
  "dispatching",
  "delivered",
  "blocked",
  "failed",
  "cancelled",
  "superseded",
]);
const TERMINAL_NATIVE_INPUT_STATUSES = new Set<NativeInputStatus>([
  "delivered",
  "failed",
  "cancelled",
  "superseded",
]);

export interface UseNativeInterventionOptions {
  workspaceId: string;
  workerItemId: string;
  workspace: WorkerWorkspace;
  execution: Execution | undefined;
  nativeContext: NativeWorkerContextView | undefined;
  onRefresh: () => Promise<void>;
}

export function useNativeIntervention({
  workspaceId,
  workerItemId,
  workspace,
  execution,
  nativeContext,
  onRefresh,
}: UseNativeInterventionOptions) {
  const queryClient = useQueryClient();
  const normalizedWorkspaceId = workspaceId.trim();
  const normalizedWorkerItemId = workerItemId.trim();
  const localSessionKey = useMemo(
    () => ["appointment-session", normalizedWorkspaceId, "worker_item", normalizedWorkerItemId] as const,
    [normalizedWorkerItemId, normalizedWorkspaceId],
  );
  const localSessionQuery = useQuery({
    queryKey: localSessionKey,
    queryFn: ({ signal }) => readLocalSession(signal),
    enabled: normalizedWorkspaceId.length > 0 && normalizedWorkerItemId.length > 0,
    retry: false,
  });
  const localSession = localSessionQuery.data;
  const localSessionSampleTime = localSessionQuery.dataUpdatedAt;
  const scope = useMemo<NativeInterventionScope | null>(() => {
    if (!isAuthorizedSession(localSession, normalizedWorkspaceId, localSessionSampleTime)
      || normalizedWorkerItemId.length === 0) return null;
    return {
      workspace_id: normalizedWorkspaceId,
      principal_id: localSession.principal_id,
      worker_item_id: normalizedWorkerItemId,
    };
  }, [localSession, localSessionSampleTime, normalizedWorkerItemId, normalizedWorkspaceId]);
  const scopeKey = scope === null ? null : nativeInterventionDraftKey(scope);
  const activeScopeKeyRef = useRef<string | null>(scopeKey);
  const mountedRef = useRef(false);
  useLayoutEffect(() => {
    activeScopeKeyRef.current = scopeKey;
    return () => {
      if (activeScopeKeyRef.current === scopeKey) activeScopeKeyRef.current = null;
    };
  }, [scopeKey]);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      activeScopeKeyRef.current = null;
    };
  }, []);

  const contextSessionId = nativeContext?.context?.session_id ?? "";
  const canonicalSessionQuery = useQuery({
    queryKey: executionSessionQueryKey(normalizedWorkspaceId, contextSessionId),
    queryFn: ({ signal }) => getExecutionSession(normalizedWorkspaceId, contextSessionId, signal),
    enabled: normalizedWorkspaceId.length > 0 && contextSessionId.length > 0,
    retry: false,
  });
  const factsReview = scope !== null && execution !== undefined && nativeContext !== undefined
    && canonicalSessionQuery.data !== undefined
    ? reviewNativeInterventionFacts({
      scope,
      workspace,
      nativeContext,
      execution,
      session: canonicalSessionQuery.data,
    })
    : null;

  const [draftEntries, setDraftEntries] = useState<Record<string, DraftEntry>>({});
  const draftEntriesRef = useRef<Record<string, DraftEntry>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [busyKeys, setBusyKeys] = useState<Record<string, true>>({});
  const busyKeysRef = useRef(new Set<string>());
  const [inputObservations, setInputObservations] = useState<Record<string, InputObservationState>>({});
  const inputObservationsRef = useRef<Record<string, InputObservationState>>({});
  const draftEntry = scopeKey === null ? undefined : draftEntries[scopeKey];
  const draft = draftEntry?.draft ?? null;
  const error = scopeKey === null ? null : errors[scopeKey] ?? null;
  const storageError = draftEntry?.storageError ?? null;
  const busy = scopeKey !== null && Object.keys(busyKeys).some((key) => key.startsWith(`${scopeKey}:`));
  const sessionAuthorized = scope !== null;

  useEffect(() => {
    if (scope === null || scopeKey === null || draftEntriesRef.current[scopeKey]?.loaded === true) return;
    const loaded = loadNativeInterventionDraft(scope);
    if (!loaded.ok) {
      putEntry(scopeKey, {
        loaded: true,
        draft: null,
        storageError: STORAGE_MESSAGE,
        storageFailure: "read",
        stagedDraft: null,
      });
      return;
    }
    putEntry(scopeKey, {
      loaded: true,
      draft: loaded.draft ?? EMPTY_DRAFT(scope),
      storageError: null,
      storageFailure: null,
      stagedDraft: null,
    });
  }, [scope, scopeKey]);

  function isCurrentScope(key: string): boolean {
    return mountedRef.current && activeScopeKeyRef.current === key;
  }

  function putEntry(key: string, entry: DraftEntry) {
    if (!mountedRef.current || activeScopeKeyRef.current !== key) return;
    draftEntriesRef.current = { ...draftEntriesRef.current, [key]: entry };
    setDraftEntries((previous) => ({ ...previous, [key]: entry }));
  }

  function setError(key: string, message: string | null) {
    if (!isCurrentScope(key)) return;
    setErrors((previous) => {
      if ((previous[key] ?? null) === message) return previous;
      const next = { ...previous };
      if (message === null) delete next[key];
      else next[key] = message;
      return next;
    });
  }

  function persistDraft(
    targetScope: NativeInterventionScope,
    key: string,
    nextDraft: NativeInterventionDraft,
    failedSaveDraft: NativeInterventionDraft = nextDraft,
  ): boolean {
    const saved = saveNativeInterventionDraft(targetScope, nextDraft);
    const previous = draftEntriesRef.current[key];
    if (!saved.ok) {
      putEntry(key, {
        loaded: true,
        draft: previous?.draft ?? null,
        storageError: STORAGE_MESSAGE,
        storageFailure: "write",
        stagedDraft: failedSaveDraft,
      });
      return false;
    }
    putEntry(key, {
      loaded: true,
      draft: nextDraft,
      storageError: null,
      storageFailure: null,
      stagedDraft: null,
    });
    return true;
  }

  function setBusy(key: string, action: string, value: boolean) {
    const busyKey = `${key}:${action}`;
    if (value) busyKeysRef.current.add(busyKey);
    else busyKeysRef.current.delete(busyKey);
    if (!isCurrentScope(key)) return;
    setBusyKeys((previous) => {
      const next = { ...previous };
      if (value) next[busyKey] = true;
      else delete next[busyKey];
      return next;
    });
  }

  function acquire(key: string, action: string): boolean {
    const busyKey = `${key}:${action}`;
    if (busyKeysRef.current.has(busyKey)) return false;
    setBusy(key, action, true);
    return true;
  }

  function currentDraft(key: string): NativeInterventionDraft | null {
    return draftEntriesRef.current[key]?.draft ?? null;
  }

  function currentObservation(key: string): InputObservationState | null {
    return inputObservationsRef.current[key] ?? null;
  }

  function putObservation(key: string, observation: InputObservationState) {
    if (!isCurrentScope(key)) return;
    inputObservationsRef.current = { ...inputObservationsRef.current, [key]: observation };
    setInputObservations((previous) => ({ ...previous, [key]: observation }));
  }

  async function readFreshFacts(
    targetScope: NativeInterventionScope,
    key: string,
  ): Promise<FreshFacts | null> {
    const currentLocalSession = await readLocalSession();
    if (!isCurrentScope(key)) return null;
    const sessionReadTime = Date.now();
    if (!isAuthorizedSession(currentLocalSession, targetScope.workspace_id, sessionReadTime)
      || currentLocalSession.principal_id !== targetScope.principal_id) {
      return null;
    }
    await onRefresh();
    if (!isCurrentScope(key)) return null;
    const freshContext = await readNativeWorkerContext(targetScope.workspace_id, targetScope.worker_item_id);
    if (!isCurrentScope(key)) return null;
    const freshExecutionId = freshContext.context?.execution_id;
    const freshSessionId = freshContext.context?.session_id;
    if (freshExecutionId === undefined || freshSessionId === undefined) return null;
    const [freshExecution, freshSession] = await Promise.all([
      getExecution(targetScope.workspace_id, freshExecutionId),
      getExecutionSession(targetScope.workspace_id, freshSessionId),
    ]);
    if (!isCurrentScope(key)) return null;
    const freshWorkspace = queryClient.getQueryData<WorkerWorkspace>(
      workerWorkspaceQueryKey(targetScope.workspace_id, targetScope.worker_item_id),
    ) ?? workspace;
    const reviewResult = reviewNativeInterventionFacts({
      scope: targetScope,
      workspace: freshWorkspace,
      nativeContext: freshContext,
      execution: freshExecution,
      session: freshSession,
    });
    queryClient.setQueryData(localSessionKey, currentLocalSession);
    queryClient.setQueryData(
      [...nativeWorkerContextQueryKey(targetScope.workspace_id, targetScope.worker_item_id), targetScope.principal_id],
      freshContext,
    );
    queryClient.setQueryData(executionQueryKey(targetScope.workspace_id, freshExecutionId), freshExecution);
    queryClient.setQueryData(executionSessionQueryKey(targetScope.workspace_id, freshSessionId), freshSession);
    return {
      localSession: currentLocalSession,
      workspace: freshWorkspace,
      nativeContext: freshContext,
      execution: freshExecution,
      session: freshSession,
      review: reviewResult,
    };
  }

  async function reviewCurrentContext(): Promise<void> {
    if (scope === null || scopeKey === null || !acquire(scopeKey, "review")) return;
    const capturedScope = scope;
    const capturedKey = scopeKey;
    setError(capturedKey, null);
    try {
      const fresh = await readFreshFacts(capturedScope, capturedKey);
      if (!isCurrentScope(capturedKey)) return;
      if (fresh === null || !fresh.review.ok) {
        setError(capturedKey, SOURCE_MESSAGE);
        return;
      }
      const latest = currentDraft(capturedKey);
      if (latest === null) return;
      persistDraft(capturedScope, capturedKey, { ...latest, review: fresh.review.review });
    } catch {
      if (isCurrentScope(capturedKey)) setError(capturedKey, SOURCE_MESSAGE);
    } finally {
      setBusy(capturedKey, "review", false);
    }
  }

  function onTextChange(text: string) {
    if (scope === null || scopeKey === null || draft === null || storageError !== null) return;
    persistDraft(scope, scopeKey, { ...draft, text });
    setError(scopeKey, null);
  }

  function onSessionAuthorized(value: LocalSession) {
    if (value.workspace_id !== normalizedWorkspaceId || value.session_id.trim().length === 0
      || value.principal_id.trim().length === 0) return;
    queryClient.setQueryData(localSessionKey, value);
  }

  async function send(): Promise<void> {
    if (scope === null || scopeKey === null || !acquire(scopeKey, "input")) return;
    const capturedScope = scope;
    const capturedKey = scopeKey;
    setError(capturedKey, null);
    try {
      const existing = currentDraft(capturedKey);
      if (existing === null || existing.review === null || existing.text.trim().length === 0
        || !canStartInput(existing, inputObservationStatus(inputObservations[capturedKey]), factsReview)) {
        setError(capturedKey, REVIEW_MESSAGE);
        return;
      }
      const fresh = await readFreshFacts(capturedScope, capturedKey);
      if (!isCurrentScope(capturedKey)) return;
      const latest = currentDraft(capturedKey);
      if (fresh === null || !fresh.review.ok || latest === null
        || !reviewStillCurrent(latest.review, fresh.review.review) || !fresh.review.canInput
        || !canStartInput(latest, inputObservationStatus(inputObservationsRef.current[capturedKey]), fresh.review)) {
        setError(capturedKey, REVIEW_MESSAGE);
        return;
      }
      const normalizedText = existing.text.trim();
      const clientKey = crypto.randomUUID();
      const original = buildNativeCommand({
        session: fresh.localSession,
        workspaceId: capturedScope.workspace_id,
        commandType: "SendExecutionInput",
        targetType: "execution_session",
        targetId: fresh.review.review.context.session_id,
        expectedVersion: fresh.review.review.context.session_version,
        payload: {
          context: fresh.review.review.context,
          client_idempotency_key: clientKey,
          text: normalizedText,
          attachment_refs: [],
        },
      });
      const pending: NativeInterventionDraft = {
        ...latest,
        client_idempotency_key: clientKey,
        input: pendingAction(original, fresh.review.review),
      };
      if (!persistDraft(capturedScope, capturedKey, pending, latest)) return;
      if (!isCurrentScope(capturedKey)) return;
      const nextObservations = { ...inputObservationsRef.current };
      delete nextObservations[capturedKey];
      inputObservationsRef.current = nextObservations;
      setInputObservations((previous) => {
        const nextState = { ...previous };
        delete nextState[capturedKey];
        return nextState;
      });
      await postOriginal(capturedScope, capturedKey, "input", original);
    } catch {
      if (isCurrentScope(capturedKey)) setError(capturedKey, SOURCE_MESSAGE);
    } finally {
      setBusy(capturedKey, "input", false);
    }
  }

  async function requestStop(): Promise<void> {
    const clickedFactsReview = factsReview;
    if (scope === null || scopeKey === null || !acquire(scopeKey, "stop")) return;
    const capturedScope = scope;
    const capturedKey = scopeKey;
    setError(capturedKey, null);
    try {
      const existing = currentDraft(capturedKey);
      const stopAction = existing?.stop;
      if (existing === null || existing === undefined || clickedFactsReview?.ok !== true
        || !clickedFactsReview.canStop || stopAction === undefined
        || !canStartStop(stopAction, clickedFactsReview)) {
        setError(capturedKey, REVIEW_MESSAGE);
        return;
      }
      const clickedReview = clickedFactsReview.review;
      const fresh = await readFreshFacts(capturedScope, capturedKey);
      if (!isCurrentScope(capturedKey)) return;
      const latest = currentDraft(capturedKey);
      if (fresh === null || !fresh.review.ok || latest === null
        || !reviewStillCurrent(clickedReview, fresh.review.review) || !fresh.review.canStop
        || !canStartStop(latest.stop, fresh.review)) {
        setError(capturedKey, REVIEW_MESSAGE);
        return;
      }
      const original = buildNativeCommand({
        session: fresh.localSession,
        workspaceId: capturedScope.workspace_id,
        commandType: "StopExecution",
        targetType: "execution",
        targetId: fresh.review.review.context.execution_id,
        expectedVersion: fresh.review.review.execution_version,
        payload: {},
      });
      const pending: NativeInterventionDraft = {
        ...latest,
        stop: pendingAction(original, fresh.review.review),
      };
      if (!persistDraft(capturedScope, capturedKey, pending, latest)) return;
      if (!isCurrentScope(capturedKey)) return;
      await postOriginal(capturedScope, capturedKey, "stop", original);
    } catch {
      if (isCurrentScope(capturedKey)) setError(capturedKey, SOURCE_MESSAGE);
    } finally {
      setBusy(capturedKey, "stop", false);
    }
  }

  async function postOriginal(
    capturedScope: NativeInterventionScope,
    capturedKey: string,
    kind: ActionKind,
    original: CommandRequest,
  ) {
    let response;
    try {
      response = await submitNativeCommand(original);
    } catch {
      if (!isCurrentScope(capturedKey)) return;
      const latest = currentDraft(capturedKey);
      if (latest !== null) {
        const action = actionFor(latest, kind);
        persistDraft(capturedScope, capturedKey, {
          ...latest,
          ...replaceAction(latest, kind, { ...action, status: "unknown" }),
        });
      }
      setError(capturedKey, STATUS_MESSAGE);
      return;
    }
    if (!isCurrentScope(capturedKey)) return;
    const latest = currentDraft(capturedKey);
    if (latest === null) return;
    const action = actionFor(latest, kind);
    const review = action.review;
    if (review === null || action.original?.command_id !== original.command_id) return;
    const result = kind === "input"
      ? reviewNativeInputResult(original, response)
      : reviewNativeStopResult(original, review, response);
    if (result.status === "unknown") {
      persistDraft(capturedScope, capturedKey, replaceAction(latest, kind, { ...action, status: "unknown" }));
      setError(capturedKey, STATUS_MESSAGE);
    } else {
      persistDraft(capturedScope, capturedKey, replaceAction(latest, kind, {
        ...action,
        status: "applied",
        aggregate_id: result.aggregate_id,
        aggregate_version: result.aggregate_version,
      }));
      setError(capturedKey, null);
    }
    if (!isCurrentScope(capturedKey)) return;
    try {
      await onRefresh();
    } catch {
      if (isCurrentScope(capturedKey)) setError(capturedKey, "操作已记录，但工作区刷新暂不可用。可查询原始操作。");
    }
  }

  async function lookupOriginal(kind: ActionKind): Promise<void> {
    if (scope === null || scopeKey === null || !acquire(scopeKey, `${kind}-lookup`)) return;
    const capturedScope = scope;
    const capturedKey = scopeKey;
    setError(capturedKey, null);
    try {
      const existing = currentDraft(capturedKey);
      const action = existing === null ? null : actionFor(existing, kind);
      const original = action?.original;
      if (existing === null || action === null || original === null || original === undefined) return;
      const status = await lookupNativeCommand(original);
      if (!isCurrentScope(capturedKey)) return;
      const latest = currentDraft(capturedKey) ?? existing;
      const latestAction = actionFor(latest, kind);
      if (latestAction.original?.command_id !== original.command_id
        || !nativeOriginalStatusMatches(original, status)) {
        setError(capturedKey, STATUS_MESSAGE);
        return;
      }
      const nextAction = actionFromJournal(kind, latestAction, status);
      if (nextAction === null) {
        setError(capturedKey, STATUS_MESSAGE);
        return;
      }
      if (latestAction.aggregate_id !== null && nextAction.aggregate_id !== null
        && latestAction.aggregate_id !== nextAction.aggregate_id) {
        setError(capturedKey, STATUS_MESSAGE);
        return;
      }
      const updatedDraft = replaceAction(latest, kind, {
        ...nextAction,
        aggregate_id: latestAction.aggregate_id ?? nextAction.aggregate_id,
        aggregate_version: latestAction.aggregate_id === null
          ? nextAction.aggregate_version
          : latestAction.aggregate_version ?? nextAction.aggregate_version,
      });
      persistDraft(
        capturedScope,
        capturedKey,
        kind === "input" && nextAction.status === "rejected"
          ? { ...updatedDraft, review: null }
          : updatedDraft,
      );
      if (nextAction.status === "applied") {
        try {
          await onRefresh();
        } catch {
          if (isCurrentScope(capturedKey)) setError(capturedKey, "操作已处理，但工作区刷新暂不可用。可继续查询原始操作。");
        }
      }
    } catch {
      if (isCurrentScope(capturedKey)) setError(capturedKey, STATUS_MESSAGE);
    } finally {
      setBusy(capturedKey, `${kind}-lookup`, false);
    }
  }

  async function readInputPage(mode: "first" | "more"): Promise<void> {
    if (scope === null || scopeKey === null || !acquire(scopeKey, "input-read")) return;
    const capturedScope = scope;
    const capturedKey = scopeKey;
    try {
      const existing = currentDraft(capturedKey);
      const original = existing?.input.original;
      const actionReview = existing?.input.review;
      if (existing === null || existing === undefined || original === null || original === undefined
        || actionReview === null || actionReview === undefined) return;
      const previous = currentObservation(capturedKey);
      const sameOriginal = previous?.commandId === original.command_id;
      if (mode === "more" && (!sameOriginal || previous?.canReadMore !== true
        || previous.nextCursor === null || previous.nextCursor === undefined)) return;
      const requestCursor = mode === "more" ? previous?.nextCursor ?? undefined : undefined;
      if (mode === "more" && requestCursor !== undefined
        && requestCursor !== null
        && previous?.seenCursors.includes(requestCursor)) {
        putObservation(capturedKey, { ...previous, canReadMore: false, unavailable: true });
        setError(capturedKey, STATUS_MESSAGE);
        return;
      }
      const page = await readNativeExecutionInputPage(
        capturedScope.workspace_id,
        actionReview.context.execution_id,
        original.target_id ?? "",
        requestCursor === undefined || requestCursor === null ? {} : { cursor: requestCursor },
      );
      if (!isCurrentScope(capturedKey)) return;
      const latest = currentDraft(capturedKey);
      if (latest?.input.original?.command_id !== original.command_id) return;

      const matching = page.items.filter(
        (item) => item.client_idempotency_key === original.payload.client_idempotency_key,
      );
      const unique = new Map<string, typeof matching[number]>();
      for (const item of matching) {
        const duplicate = unique.get(item.id);
        if (duplicate !== undefined && (duplicate.version !== item.version || duplicate.status !== item.status)) {
          putObservation(capturedKey, observationUnavailable(original.command_id, previous));
          setError(capturedKey, STATUS_MESSAGE);
          return;
        }
        unique.set(item.id, item);
      }
      if (unique.size > 1) {
        putObservation(capturedKey, observationUnavailable(original.command_id, previous));
        setError(capturedKey, STATUS_MESSAGE);
        return;
      }
      const item = unique.values().next().value as typeof matching[number] | undefined;
      const itemStatus = item === undefined ? null : nativeInputStatus(item.status);
      if (item !== undefined && itemStatus === null) {
        putObservation(capturedKey, observationUnavailable(original.command_id, previous));
        setError(capturedKey, STATUS_MESSAGE);
        return;
      }
      const inputAction = latest.input;
      if (item !== undefined && inputAction.aggregate_id !== null && inputAction.aggregate_id !== item.id) {
        putObservation(capturedKey, observationUnavailable(original.command_id, previous));
        setError(capturedKey, STATUS_MESSAGE);
        return;
      }
      if (item !== undefined && sameOriginal && previous?.inputId !== null && previous?.inputId !== undefined
        && previous.inputId !== item.id) {
        putObservation(capturedKey, observationUnavailable(original.command_id, previous));
        setError(capturedKey, STATUS_MESSAGE);
        return;
      }
      if (item !== undefined && sameOriginal && previous?.version !== null && previous?.version !== undefined
        && item.version < previous.version) {
        putObservation(capturedKey, observationUnavailable(original.command_id, previous));
        setError(capturedKey, STATUS_MESSAGE);
        return;
      }
      if (item !== undefined && inputAction.aggregate_version !== null
        && item.version < inputAction.aggregate_version) {
        putObservation(capturedKey, observationUnavailable(original.command_id, previous));
        setError(capturedKey, STATUS_MESSAGE);
        return;
      }

      const seenCursors = mode === "first" || !sameOriginal
        ? []
        : [...(previous?.seenCursors ?? []), ...(requestCursor === undefined || requestCursor === null ? [] : [requestCursor])];
      const repeatedCursor = page.next_cursor !== null
        && (page.next_cursor === requestCursor || seenCursors.includes(page.next_cursor));
      const observation: InputObservationState = {
        commandId: original.command_id,
        status: itemStatus,
        inputId: item?.id ?? (sameOriginal ? previous?.inputId ?? null : null),
        version: item?.version ?? (sameOriginal ? previous?.version ?? null : null),
        nextCursor: repeatedCursor ? null : page.next_cursor,
        canReadMore: page.next_cursor !== null && !repeatedCursor,
        unavailable: repeatedCursor,
        seenCursors,
      };
      putObservation(capturedKey, observation);
      if (repeatedCursor) setError(capturedKey, STATUS_MESSAGE);
      else setError(capturedKey, null);

      if (item !== undefined && latest.input.status === "applied") {
        const mappedAction: NativeInterventionAction = {
          ...latest.input,
          aggregate_id: item.id,
          aggregate_version: item.version,
        };
        const terminalDelivery = itemStatus === "delivered" || itemStatus === "failed"
          || itemStatus === "cancelled" || itemStatus === "superseded";
        persistDraft(capturedScope, capturedKey, {
          ...latest,
          review: terminalDelivery ? null : latest.review,
          input: mappedAction,
        });
      }
    } catch {
      if (isCurrentScope(capturedKey)) {
        const latest = currentDraft(capturedKey);
        const originalId = latest?.input.original?.command_id;
        if (originalId !== undefined) {
          putObservation(capturedKey, observationUnavailable(originalId, currentObservation(capturedKey)));
        }
        setError(capturedKey, STATUS_MESSAGE);
      }
    } finally {
      setBusy(capturedKey, "input-read", false);
    }
  }

  function explicitResendSuperseded(selectedBody: string) {
    if (scope === null || scopeKey === null || !canLoadHistoricalInput || selectedBody.trim().length === 0) return;
    const existing = currentDraft(scopeKey);
    if (existing === null || !canSelectHistoricalInput(existing.input, currentObservation(scopeKey))
      || stopBlocksCurrentExecution(existing.stop, factsReview)) {
      return;
    }
    const next: NativeInterventionDraft = {
      ...existing,
      text: selectedBody,
      review: null,
    };
    if (persistDraft(scope, scopeKey, next)) {
      setError(scopeKey, null);
    }
  }

  function retryDraftStorage() {
    if (scope === null || scopeKey === null) return;
    const entry = draftEntriesRef.current[scopeKey];
    if (entry === undefined) return;
    if (entry.storageFailure === "write" && entry.stagedDraft !== null) {
      const saved = saveNativeInterventionDraft(scope, entry.stagedDraft);
      if (!saved.ok) {
        putEntry(scopeKey, { ...entry, storageError: STORAGE_MESSAGE });
        return;
      }
      putEntry(scopeKey, {
        loaded: true,
        draft: entry.stagedDraft,
        storageError: null,
        storageFailure: null,
        stagedDraft: null,
      });
      return;
    }
    const loaded = loadNativeInterventionDraft(scope);
    if (!loaded.ok) {
      putEntry(scopeKey, { ...entry, storageError: STORAGE_MESSAGE, storageFailure: "read" });
      return;
    }
    putEntry(scopeKey, {
      loaded: true,
      draft: loaded.draft ?? EMPTY_DRAFT(scope),
      storageError: null,
      storageFailure: null,
      stagedDraft: null,
    });
  }

  const currentDraftReview = draft?.review ?? null;
  const canLoadHistoricalInput = scope !== null && scopeKey !== null && sessionAuthorized
    && draftEntry?.loaded === true && draft !== null && storageError === null
    && canSelectHistoricalInput(draft.input, inputObservations[scopeKey] ?? null)
    && !stopBlocksCurrentExecution(draft.stop, factsReview);
  const canSend = scope !== null && scopeKey !== null && draft !== null && factsReview?.ok === true
    && sessionAuthorized && reviewStillCurrent(currentDraftReview, factsReview.review)
    && factsReview.canInput && canStartInput(draft, inputObservationStatus(inputObservations[scopeKey]), factsReview)
    && !busyKeys[`${scopeKey}:input`];
  const canStop = scope !== null && scopeKey !== null && draft !== null && factsReview?.ok === true
    && sessionAuthorized
    && factsReview.canStop && canStartStop(draft.stop, factsReview)
    && !busyKeys[`${scopeKey}:stop`];

  return {
    text: draft?.text ?? "",
    onTextChange,
    reviewCurrentContext,
    send,
    requestStop,
    lookupOriginalInput: () => lookupOriginal("input"),
    lookupOriginalStop: () => lookupOriginal("stop"),
    readMoreInputStatus: () => readInputPage("more"),
    refreshInputStatus: () => readInputPage("first"),
    explicitResendSuperseded,
    canLoadHistoricalInput,
    sessionAuthorized,
    onSessionAuthorized,
    inputAction: draft?.input ?? null,
    stopAction: draft?.stop ?? null,
    inputObservation: scopeKey === null || inputObservations[scopeKey] === undefined
      ? null
      : {
        status: inputObservations[scopeKey].status,
        inputId: inputObservations[scopeKey].inputId,
        canReadMore: inputObservations[scopeKey].canReadMore,
        unavailable: inputObservations[scopeKey].unavailable,
      },
    canSend,
    canStop,
    busy,
    error,
    storageError,
    retryDraftStorage,
    nativeFactsAvailable: factsReview?.ok === true,
    stopRequested: execution?.stop_requested === true,
  };
}

function emptyAction(): NativeInterventionAction {
  return {
    original: null,
    review: null,
    status: "idle",
    aggregate_id: null,
    aggregate_version: null,
    rejection_code: null,
  };
}

function pendingAction(
  original: CommandRequest,
  review: NativeInterventionReview,
): NativeInterventionAction {
  return {
    original,
    review,
    status: "pending",
    aggregate_id: null,
    aggregate_version: null,
    rejection_code: null,
  };
}

function actionFor(draft: NativeInterventionDraft, kind: ActionKind): NativeInterventionAction {
  return kind === "input" ? draft.input : draft.stop;
}

function replaceAction(
  draft: NativeInterventionDraft,
  kind: ActionKind,
  action: NativeInterventionAction,
): NativeInterventionDraft {
  return kind === "input" ? { ...draft, input: action } : { ...draft, stop: action };
}

function reviewStillCurrent(
  approved: NativeInterventionReview | null | undefined,
  current: NativeInterventionReview | null | undefined,
): boolean {
  return approved != null && current != null
    && sessionObservationMatches(
      approved.context.session_version,
      approved.context.session_last_sequence,
      current.context.session_version,
      current.context.session_last_sequence,
    )
    && approved.context.worker_item_id === current.context.worker_item_id
    && approved.context.plan_generation === current.context.plan_generation
    && approved.context.context_version === current.context.context_version
    && approved.context.execution_id === current.context.execution_id
    && approved.context.session_id === current.context.session_id
    && approved.context.worker_id === current.context.worker_id
    && approved.context.execution_mode === current.context.execution_mode
    && approved.context.binding_version === current.context.binding_version
    && approved.context.run_id === current.context.run_id
    && approved.execution_version === current.execution_version
    && approved.binding_version === current.binding_version
    && approved.profile_ref.workspace_id === current.profile_ref.workspace_id
    && approved.profile_ref.profile_id === current.profile_ref.profile_id
    && approved.profile_ref.revision === current.profile_ref.revision;
}

function canStartInput(
  draft: NativeInterventionDraft,
  observation: string | null,
  facts: ReturnType<typeof reviewNativeInterventionFacts> | null,
): boolean {
  if (facts?.ok !== true || !facts.canInput) return false;
  if (stopBlocksCurrentExecution(draft.stop, facts)) return false;
  if (draft.input.status === "idle" || draft.input.status === "rejected") return true;
  return draft.input.status === "applied"
    && (observation === "delivered" || observation === "failed"
      || observation === "cancelled" || observation === "superseded");
}

function canSelectHistoricalInput(
  action: NativeInterventionAction,
  observation: InputObservationState | null,
): boolean {
  if (action.status === "idle" || action.status === "rejected") return true;
  if (action.status !== "applied" || action.original === null || observation === null
    || observation.commandId !== action.original.command_id || observation.unavailable
    || observation.status === null || observation.inputId === null) return false;
  return TERMINAL_NATIVE_INPUT_STATUSES.has(observation.status)
    && (action.aggregate_id === null || action.aggregate_id === observation.inputId);
}

function canStartStop(
  action: NativeInterventionAction,
  facts: ReturnType<typeof reviewNativeInterventionFacts> | null,
): boolean {
  return facts?.ok === true && facts.canStop && !stopBlocksCurrentExecution(action, facts);
}

function stopBlocksCurrentExecution(
  action: NativeInterventionAction,
  facts: ReturnType<typeof reviewNativeInterventionFacts> | null,
): boolean {
  if (action.status === "pending" || action.status === "unknown") return true;
  if (action.status === "idle" || action.status === "rejected") return false;

  const original = action.original;
  const savedReview = action.review;
  if (facts?.ok !== true || original === null || savedReview === null
    || original.command_type !== "StopExecution" || original.target_type !== "execution"
    || original.workspace_id !== savedReview.profile_ref.workspace_id
    || original.target_id !== savedReview.context.execution_id
    || savedReview.context.worker_item_id !== facts.review.context.worker_item_id
    || savedReview.profile_ref.workspace_id !== facts.review.profile_ref.workspace_id) {
    return true;
  }
  return facts.review.context.execution_id === original.target_id;
}

function inputObservationStatus(entry: InputObservationState | undefined): NativeInputStatus | null {
  return entry?.status ?? null;
}

function nativeInputStatus(value: string): NativeInputStatus | null {
  return NATIVE_INPUT_STATUSES.has(value as NativeInputStatus) ? value as NativeInputStatus : null;
}

function observationUnavailable(
  commandId: string,
  previous: InputObservationState | null,
): InputObservationState {
  return {
    commandId,
    status: null,
    inputId: previous?.inputId ?? null,
    version: previous?.version ?? null,
    nextCursor: null,
    canReadMore: false,
    unavailable: true,
    seenCursors: previous?.seenCursors ?? [],
  };
}

function actionFromJournal(
  kind: ActionKind,
  current: NativeInterventionAction,
  status: CommandStatusResponse,
): NativeInterventionAction | null {
  if (status.status === "pending") {
    if (current.status === "applied" || current.status === "rejected") return null;
    return { ...current, status: current.status === "pending" ? "pending" : "unknown" };
  }
  if (status.status === "rejected") {
    if (current.status === "applied") return null;
    const code = typeof status.error_code === "string" && SAFE_REJECTION_CODES.has(status.error_code)
      ? status.error_code
      : "unknown";
    return { ...current, status: "rejected", rejection_code: code, aggregate_id: null, aggregate_version: null };
  }
  if (current.review === null || current.original === null) return null;
  const mapped = status.result === null || status.result === undefined
    ? { status: "applied" as const, aggregate_id: null, aggregate_version: null }
    : kind === "input"
      ? reviewNativeInputResult(current.original, status.result)
      : reviewNativeStopResult(current.original, current.review, status.result);
  if (mapped.status !== "applied") return null;
  return {
    ...current,
    status: "applied",
    aggregate_id: mapped.aggregate_id,
    aggregate_version: mapped.aggregate_version,
    rejection_code: null,
  };
}

function isAuthorizedSession(value: LocalSession | undefined, workspaceId: string, now: number): value is LocalSession {
  if (value === undefined || value.workspace_id !== workspaceId
    || value.session_id.trim().length === 0 || value.principal_id.trim().length === 0
    || !Number.isFinite(now) || now <= 0) return false;
  const expiry = Date.parse(value.expires_at);
  return Number.isFinite(expiry) && expiry > now;
}
